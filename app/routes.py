import csv
from datetime import date, datetime
from io import StringIO

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from sqlalchemy import func, select

from .extensions import db
from .models import (
    Aircraft,
    AircraftType,
    AdsbFlight,
    Airline,
    Airport,
    GroundTruthMatch,
    MatchResult,
    Photo,
    SpottingEvent,
    TrackPoint,
)
from .reference_data import BA_A380_REGISTRATIONS
from .services.evaluation import build_date_comparison, build_evaluation_report
from .services.photo_upload import InvalidPhotoError, save_photo_upload
from .services.track_visualization import build_track_visualization


main_bp = Blueprint("main", __name__)


def _evaluation_scope():
    date_value = request.args.get("date", "2025-11-05").strip()
    try:
        selected_date = date.fromisoformat(date_value) if date_value else None
    except ValueError:
        abort(400, description="Date must use YYYY-MM-DD.")

    requested_location = request.args.get("location")
    if requested_location is None and selected_date is not None:
        location = db.session.scalar(
            select(SpottingEvent.spotting_location_raw)
            .where(SpottingEvent.spotting_date == selected_date)
            .where(SpottingEvent.spotting_location_raw.is_not(None))
            .group_by(SpottingEvent.spotting_location_raw)
            .order_by(func.count(SpottingEvent.spotting_id).desc())
            .limit(1)
        ) or ""
    else:
        location = (requested_location or "").strip()
    return date_value, selected_date, location


def _is_async_request():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _ground_truth_error(message, status_code=400):
    if _is_async_request():
        return jsonify({"ok": False, "message": message}), status_code
    flash(message, "warning")
    return redirect(
        url_for(
            "main.evaluation",
            date=request.form.get("return_date", "2025-11-05"),
            location=request.form.get("return_location", "London Heathrow"),
            review_status=request.form.get("return_review_status", "all"),
            _anchor=f"event-{request.view_args['spotting_id']}",
        )
    )


def _archive_event_sort_key(event):
    observed_seconds = (
        event.spotting_time.hour * 3600
        + event.spotting_time.minute * 60
        + event.spotting_time.second
        if event.spotting_time
        else -1
    )
    return (
        -event.spotting_date.toordinal(),
        -observed_seconds,
        event.aircraft.registration,
        event.spotting_id,
    )


def _group_archive_rows(rows):
    grouped = {}
    for row in rows:
        airline_name = (
            row["event"].observed_airline.name
            if row["event"].observed_airline
            else "Unknown airline"
        )
        grouped.setdefault(airline_name, []).append(row)
    return [
        {
            "airline": airline_name,
            "rows": sorted(
                group_rows,
                key=lambda row: _archive_event_sort_key(row["event"]),
            ),
        }
        for airline_name, group_rows in sorted(grouped.items())
    ]


def _archive_navigation_tiles():
    specifications = [
        {
            "label": "British Airways",
            "eyebrow": "Airline",
            "href": url_for("main.archive", airline="British Airways"),
            "condition": Airline.name == "British Airways",
        },
        {
            "label": "Boeing 787",
            "eyebrow": "Aircraft family",
            "href": url_for("main.archive", aircraft_type="Boeing 787"),
            "condition": AircraftType.model.ilike("Boeing 787%"),
        },
        {
            "label": "London Heathrow",
            "eyebrow": "Location",
            "href": url_for("main.archive", airport="London Heathrow"),
            "condition": SpottingEvent.spotting_location_raw == "London Heathrow",
        },
        {
            "label": "Airbus A350",
            "eyebrow": "Aircraft family",
            "href": url_for("main.archive", aircraft_type="Airbus A350"),
            "condition": AircraftType.model.ilike("Airbus A350%"),
        },
    ]
    photo_query = (
        select(Photo)
        .join(SpottingEvent, Photo.spotting_id == SpottingEvent.spotting_id)
        .join(Aircraft, SpottingEvent.aircraft_id == Aircraft.aircraft_id)
        .outerjoin(Airline, SpottingEvent.observed_airline_id == Airline.airline_id)
        .outerjoin(
            AircraftType,
            Aircraft.aircraft_type_id == AircraftType.aircraft_type_id,
        )
    )
    tiles = []
    for specification in specifications:
        photos = db.session.scalars(
            photo_query.where(specification["condition"])
            .order_by(
                Photo.is_primary.desc(),
                SpottingEvent.spotting_date.desc(),
                Photo.photo_id.desc(),
            )
            .limit(12)
        ).all()
        tiles.append(
            {
                **{
                    key: value
                    for key, value in specification.items()
                    if key != "condition"
                },
                "photos": [
                    url_for("main.uploaded_photo", filename=photo.file_path)
                    for photo in photos
                ],
            }
        )
    return tiles


@main_bp.get("/")
def archive():
    query = (
        select(SpottingEvent)
        .join(SpottingEvent.aircraft)
        .outerjoin(SpottingEvent.observed_airline)
        .outerjoin(Aircraft.aircraft_type)
    )

    registration = request.args.get("registration", "").strip()
    airline = request.args.get("airline", "").strip()
    aircraft_type = request.args.get("aircraft_type", "").strip()
    airport = request.args.get("airport", "").strip()

    if registration:
        query = query.where(Aircraft.registration.ilike(f"%{registration}%"))
    if airline:
        query = query.where(Airline.name == airline)
    if aircraft_type:
        if aircraft_type in {"Boeing 787", "Airbus A350"}:
            query = query.where(AircraftType.model.ilike(f"{aircraft_type}%"))
        else:
            query = query.where(AircraftType.model == aircraft_type)
    if airport:
        query = query.where(SpottingEvent.spotting_location_raw == airport)

    events = db.session.scalars(query.limit(200)).all()
    event_ids = [event.spotting_id for event in events]
    photos_by_spotting_id = {}
    reviewed_spotting_ids = set()
    if event_ids:
        photos = db.session.scalars(
            select(Photo)
            .where(Photo.spotting_id.in_(event_ids))
            .order_by(Photo.is_primary.desc(), Photo.photo_id)
        ).all()
        for photo in photos:
            photos_by_spotting_id.setdefault(photo.spotting_id, photo)
        reviewed_spotting_ids = set(
            db.session.scalars(
                select(GroundTruthMatch.spotting_id).where(
                    GroundTruthMatch.spotting_id.in_(event_ids)
                )
            ).all()
        )

    archive_rows = [
        {
            "event": event,
            "photo": photos_by_spotting_id.get(event.spotting_id),
            "reviewed": event.spotting_id in reviewed_spotting_ids,
        }
        for event in events
    ]
    missing_image_groups = _group_archive_rows(
        [row for row in archive_rows if row["photo"] is None]
    )
    uploaded_image_groups = _group_archive_rows(
        [row for row in archive_rows if row["photo"] is not None]
    )
    airlines = db.session.scalars(select(Airline).order_by(Airline.name)).all()
    aircraft_types = db.session.scalars(
        select(AircraftType)
        .join(Aircraft, Aircraft.aircraft_type_id == AircraftType.aircraft_type_id)
        .join(SpottingEvent, SpottingEvent.aircraft_id == Aircraft.aircraft_id)
        .distinct()
        .order_by(AircraftType.model)
    ).all()
    airport_rows = db.session.execute(
        select(
            SpottingEvent.spotting_location_raw,
            Airport.iata_code,
            Airport.icao_code,
        )
        .outerjoin(Airport, SpottingEvent.airport_id == Airport.airport_id)
        .where(SpottingEvent.spotting_location_raw.is_not(None))
        .distinct()
        .order_by(SpottingEvent.spotting_location_raw)
    ).all()
    airport_options = [
        {
            "value": raw,
            "label": (
                f"{raw} ({iata}/{icao})" if iata and icao else raw
            ),
        }
        for raw, iata, icao in airport_rows
    ]

    return render_template(
        "archive.html",
        event_count=len(events),
        missing_image_count=sum(len(group["rows"]) for group in missing_image_groups),
        uploaded_image_count=sum(len(group["rows"]) for group in uploaded_image_groups),
        missing_image_groups=missing_image_groups,
        uploaded_image_groups=uploaded_image_groups,
        navigation_tiles=_archive_navigation_tiles(),
        airlines=airlines,
        aircraft_types=aircraft_types,
        airports=airport_options,
        filters=request.args,
    )


@main_bp.get("/fleet/british-airways-a380")
def british_airways_a380():
    spotted_rows = db.session.execute(
        select(Aircraft.registration, func.count(SpottingEvent.spotting_id))
        .join(SpottingEvent, SpottingEvent.aircraft_id == Aircraft.aircraft_id)
        .where(Aircraft.registration.in_(BA_A380_REGISTRATIONS))
        .group_by(Aircraft.registration)
    ).all()
    spotted = {registration: count for registration, count in spotted_rows}
    fleet = [
        {
            "registration": registration,
            "spotted": registration in spotted,
            "event_count": spotted.get(registration, 0),
        }
        for registration in BA_A380_REGISTRATIONS
    ]
    return render_template(
        "fleet.html",
        fleet=fleet,
        spotted_count=sum(1 for aircraft in fleet if aircraft["spotted"]),
    )


@main_bp.get("/sightings/<int:spotting_id>")
def sighting_detail(spotting_id):
    event = db.session.get(SpottingEvent, spotting_id)
    if event is None:
        abort(404)
    photos = db.session.scalars(
        select(Photo)
        .where(Photo.spotting_id == spotting_id)
        .order_by(Photo.is_primary.desc(), Photo.photo_id)
    ).all()
    observation_history = db.session.scalars(
        select(SpottingEvent)
        .where(SpottingEvent.aircraft_id == event.aircraft_id)
        .order_by(SpottingEvent.spotting_date.desc(), SpottingEvent.spotting_id.desc())
    ).all()
    history_ids = [item.spotting_id for item in observation_history]
    history_photos = db.session.scalars(
        select(Photo)
        .where(Photo.spotting_id.in_(history_ids))
        .order_by(Photo.is_primary.desc(), Photo.photo_id)
    ).all()
    photos_by_spotting_id = {}
    for photo in history_photos:
        photos_by_spotting_id.setdefault(photo.spotting_id, photo)
    best_match = db.session.scalar(
        select(MatchResult)
        .where(MatchResult.spotting_id == spotting_id)
        .order_by(MatchResult.total_score.desc(), MatchResult.match_result_id)
        .limit(1)
    )
    return render_template(
        "sighting_detail.html",
        event=event,
        photos=photos,
        observation_history=observation_history,
        photos_by_spotting_id=photos_by_spotting_id,
        best_match=best_match,
    )


@main_bp.post("/sightings/<int:spotting_id>/photos")
def upload_sighting_photo(spotting_id):
    event = db.session.get(SpottingEvent, spotting_id)
    if event is None:
        abort(404)

    try:
        stored_name = save_photo_upload(
            request.files.get("photo"), current_app.config["UPLOAD_FOLDER"]
        )
    except InvalidPhotoError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.sighting_detail", spotting_id=spotting_id))

    captured_at = None
    captured_value = request.form.get("captured_at", "").strip()
    if captured_value:
        try:
            captured_at = datetime.fromisoformat(captured_value)
        except ValueError:
            flash("The photo was uploaded, but its capture time was invalid.", "warning")

    has_photo = db.session.scalar(
        select(Photo.photo_id).where(Photo.spotting_id == spotting_id).limit(1)
    )
    db.session.add(
        Photo(
            spotting_id=spotting_id,
            file_path=stored_name,
            captured_at=captured_at,
            is_primary=has_photo is None,
        )
    )
    db.session.commit()
    flash("Photo added to this sighting.", "success")
    return redirect(url_for("main.sighting_detail", spotting_id=spotting_id))


@main_bp.post("/sightings/<int:spotting_id>/notes")
def update_sighting_notes(spotting_id):
    event = db.session.get(SpottingEvent, spotting_id)
    if event is None:
        abort(404)
    event.notes = request.form.get("notes", "").strip()[:2000] or None
    db.session.commit()
    flash("Sighting note updated.", "success")
    return redirect(url_for("main.sighting_detail", spotting_id=spotting_id))


@main_bp.get("/uploads/<path:filename>")
def uploaded_photo(filename):
    photo_exists = db.session.scalar(
        select(Photo.photo_id).where(Photo.file_path == filename).limit(1)
    )
    if photo_exists is None:
        abort(404)
    return send_from_directory(current_app.config["UPLOAD_FOLDER"], filename)


@main_bp.get("/evaluation")
def evaluation():
    date_value, selected_date, location = _evaluation_scope()
    review_status = request.args.get("review_status", "all").strip().lower()
    if review_status not in {"all", "evaluated", "unevaluated"}:
        abort(400, description="Unknown review status filter.")

    report = build_evaluation_report(
        spotting_date=selected_date,
        spotting_location_raw=location or None,
    )
    comparison_dates = db.session.scalars(
        select(SpottingEvent.spotting_date)
        .distinct()
        .order_by(SpottingEvent.spotting_date)
    ).all()
    comparison = build_date_comparison(
        comparison_dates,
        spotting_location_raw=location or "London Heathrow",
    )
    all_rows = report["rows"]
    review_counts = {
        "all": len(all_rows),
        "evaluated": sum(row["truth"] is not None for row in all_rows),
        "unevaluated": sum(row["truth"] is None for row in all_rows),
    }
    rows = all_rows
    if review_status == "evaluated":
        rows = [row for row in all_rows if row["truth"] is not None]
    elif review_status == "unevaluated":
        rows = [row for row in all_rows if row["truth"] is None]

    return render_template(
        "evaluation.html",
        rows=rows,
        metrics=report["metrics"],
        selected_date=date_value,
        selected_location=location,
        review_status=review_status,
        review_counts=review_counts,
        comparison=comparison,
        ablation=report["ablation"],
    )


@main_bp.get("/evaluation/export.<export_format>")
def export_evaluation(export_format):
    if export_format not in {"csv", "json"}:
        abort(404)
    date_value, selected_date, location = _evaluation_scope()
    report = build_evaluation_report(
        spotting_date=selected_date,
        spotting_location_raw=location or None,
    )
    records = []
    for row in report["rows"]:
        event = row["event"]
        top = row["top"]
        truth = row["truth"]
        records.append(
            {
                "spotting_id": event.spotting_id,
                "spotting_date": event.spotting_date.isoformat(),
                "registration": event.aircraft.registration,
                "observed_flight_number": event.flight_number or "",
                "observed_route": event.route_text_original or "",
                "candidate_count": len(row["candidates"]),
                "top_adsb_flight_id": top.adsb_flight_id if top else None,
                "top_callsign": top.adsb_flight.callsign if top else None,
                "top_score": float(top.total_score) if top else None,
                "automated_status": top.match_status if top else "missing",
                "evaluated": truth is not None,
                "verified_adsb_flight_id": (
                    truth.expected_adsb_flight_id if truth else None
                ),
                "verification_method": truth.verification_method if truth else None,
                "verified_at": truth.verified_at.isoformat() if truth else None,
                "notes": truth.notes if truth else None,
            }
        )

    filename_date = date_value or "all-dates"
    if export_format == "json":
        response = jsonify(
            {
                "filters": {"date": date_value, "location": location},
                "metrics": report["metrics"],
                "ablation": report["ablation"],
                "records": records,
            }
        )
    else:
        output = StringIO(newline="")
        fieldnames = list(records[0]) if records else [
            "spotting_id",
            "spotting_date",
            "registration",
            "observed_flight_number",
            "observed_route",
            "candidate_count",
            "top_adsb_flight_id",
            "top_callsign",
            "top_score",
            "automated_status",
            "evaluated",
            "verified_adsb_flight_id",
            "verification_method",
            "verified_at",
            "notes",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)
        response = make_response(output.getvalue())
        response.mimetype = "text/csv"
    response.headers["Content-Disposition"] = (
        f'attachment; filename="evaluation-{filename_date}.{export_format}"'
    )
    return response


@main_bp.post("/evaluation/ground-truth/<int:spotting_id>")
def save_ground_truth(spotting_id):
    event = db.session.get(SpottingEvent, spotting_id)
    if event is None:
        abort(404)

    expected_value = request.form.get("expected_adsb_flight_id", "").strip()
    if not expected_value:
        return _ground_truth_error(
            "Choose a verified candidate or explicitly confirm that no candidate matches."
        )
    expected_flight_id = None
    if expected_value and expected_value != "none":
        try:
            expected_flight_id = int(expected_value)
        except ValueError:
            return _ground_truth_error("Invalid ADS-B flight identifier.")
        is_candidate = db.session.scalar(
            select(MatchResult.match_result_id).where(
                MatchResult.spotting_id == spotting_id,
                MatchResult.adsb_flight_id == expected_flight_id,
            )
        )
        if is_candidate is None or db.session.get(AdsbFlight, expected_flight_id) is None:
            return _ground_truth_error(
                "The selected flight is not a candidate for this sighting."
            )

    allowed_methods = {"manual_fr24", "photo_metadata", "manual_review"}
    verification_method = request.form.get(
        "verification_method", "manual_review"
    ).strip()
    if verification_method not in allowed_methods:
        return _ground_truth_error("Invalid verification method.")

    truth = db.session.scalar(
        select(GroundTruthMatch).where(
            GroundTruthMatch.spotting_id == spotting_id
        )
    )
    created = truth is None
    if created:
        truth = GroundTruthMatch(spotting_id=spotting_id)
        db.session.add(truth)
    truth.expected_adsb_flight_id = expected_flight_id
    truth.verification_method = verification_method
    truth.notes = request.form.get("notes", "").strip()[:1000] or None
    db.session.commit()

    if _is_async_request():
        return jsonify(
            {
                "ok": True,
                "message": "Ground truth saved successfully.",
                "spotting_id": spotting_id,
                "created": created,
                "expected_adsb_flight_id": expected_flight_id,
                "verification_method": verification_method,
                "verified_at": truth.verified_at.isoformat(),
            }
        )

    return redirect(
        url_for(
            "main.evaluation",
            date=request.form.get("return_date", "2025-11-05"),
            location=request.form.get("return_location", "London Heathrow"),
            review_status=request.form.get("return_review_status", "all"),
            _anchor=f"event-{spotting_id}",
        )
    )


@main_bp.get("/flights/<int:adsb_flight_id>")
def flight_detail(adsb_flight_id):
    flight = db.session.get(AdsbFlight, adsb_flight_id)
    if flight is None:
        abort(404)
    points = db.session.scalars(
        select(TrackPoint)
        .where(TrackPoint.adsb_flight_id == adsb_flight_id)
        .order_by(TrackPoint.observed_at)
    ).all()
    visualization = build_track_visualization(points)
    return render_template(
        "flight_detail.html",
        flight=flight,
        points=points,
        visualization=visualization,
    )
