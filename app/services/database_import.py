from collections import Counter, defaultdict

from sqlalchemy import delete, func, select

from ..extensions import db
from ..models import (
    AdsbFlight,
    Aircraft,
    AircraftType,
    Airline,
    Airport,
    GroundTruthMatch,
    MatchResult,
    Photo,
    SpottingEvent,
)
from ..reference_data import AIRLINE_CODES, AIRPORT_ALIASES, AIRPORTS
from .excel_importer import NormalizedSpottingEvent


def _get_or_create(model_class, defaults=None, **lookup):
    instance = db.session.scalar(select(model_class).filter_by(**lookup))
    if instance:
        return instance
    instance = model_class(**lookup, **(defaults or {}))
    db.session.add(instance)
    db.session.flush()
    return instance


def seed_airports():
    for data in AIRPORTS.values():
        _get_or_create(
            Airport,
            iata_code=data["iata_code"],
            defaults={
                "icao_code": data["icao_code"],
                "name": data["name"],
                "latitude": data["latitude"],
                "longitude": data["longitude"],
            },
        )
    db.session.commit()


def seed_airline_codes():
    for name, (iata_code, icao_code) in AIRLINE_CODES.items():
        airline = db.session.scalar(select(Airline).where(Airline.name == name))
        if airline is None:
            continue
        airline.iata_code = iata_code
        airline.icao_code = icao_code
    db.session.commit()


def _airport_for_raw_place(raw_place):
    if not raw_place:
        return None
    normalized = str(raw_place).strip().casefold()
    canonical_name = AIRPORT_ALIASES.get(normalized)
    if canonical_name is None:
        canonical_name = next(
            (
                name
                for name, data in AIRPORTS.items()
                if normalized
                in {
                    name.casefold(),
                    data["iata_code"].casefold(),
                    data["icao_code"].casefold(),
                    data["name"].casefold(),
                }
            ),
            None,
        )
    if canonical_name is None:
        return None
    return db.session.scalar(
        select(Airport).where(
            Airport.iata_code == AIRPORTS[canonical_name]["iata_code"]
        )
    )


def import_normalized_events(
    events: list[NormalizedSpottingEvent], *, commit: bool = True
) -> dict:
    inserted = 0
    skipped_existing = 0
    skipped_invalid = 0

    for event in events:
        if event.spotting_date is None:
            skipped_invalid += 1
            continue
        existing = db.session.scalar(
            select(SpottingEvent).where(
                SpottingEvent.source_row == event.source_row,
                SpottingEvent.source_event_group == event.source_event_group,
            )
        )
        if existing:
            skipped_existing += 1
            continue

        airline = None
        if event.airline_raw:
            airline = _get_or_create(Airline, name=event.airline_raw)

        aircraft_type = None
        if event.aircraft_type:
            aircraft_type = _get_or_create(AircraftType, model=event.aircraft_type)

        aircraft = db.session.scalar(
            select(Aircraft).where(Aircraft.registration == event.registration)
        )
        if aircraft is None:
            aircraft = Aircraft(
                registration=event.registration,
                airline=airline,
                aircraft_type=aircraft_type,
                notes=event.aircraft_notes,
            )
            db.session.add(aircraft)
            db.session.flush()
        else:
            if airline is not None:
                aircraft.airline = airline
            if aircraft_type is not None:
                aircraft.aircraft_type = aircraft_type
            if event.aircraft_notes:
                aircraft.notes = event.aircraft_notes

        db.session.add(
            SpottingEvent(
                aircraft=aircraft,
                observed_airline=airline,
                spotting_date=event.spotting_date,
                time_precision="unknown",
                airport=_airport_for_raw_place(event.spotting_location_raw),
                spotting_location_raw=event.spotting_location_raw,
                flight_number=event.flight_number,
                route_text_original=event.route_text_original,
                declared_departure_airport=_airport_for_raw_place(
                    event.route_departure_raw
                ),
                declared_arrival_airport=_airport_for_raw_place(
                    event.route_arrival_raw
                ),
                source_row=event.source_row,
                source_event_group=event.source_event_group,
                quality_status=event.quality_status,
                quality_reasons=list(event.quality_reasons),
            )
        )
        inserted += 1

    if commit:
        db.session.commit()
    return {
        "inserted": inserted,
        "skipped_existing": skipped_existing,
        "skipped_invalid": skipped_invalid,
    }


def restore_aircraft_types_from_events(
    events: list[NormalizedSpottingEvent], *, commit: bool = True
) -> dict:
    """Restore website aircraft models from the authoritative workbook values.

    This is intentionally in-place: spotting identifiers, photographs, ADS-B
    flights, match results and ground-truth rows remain untouched.
    """
    workbook_by_source = {
        (event.source_row, event.source_event_group): event for event in events
    }
    stored_events = db.session.scalars(select(SpottingEvent)).all()
    stored_by_aircraft = defaultdict(list)
    desired_registrations = defaultdict(set)
    for stored_event in stored_events:
        stored_by_aircraft[stored_event.aircraft_id].append(stored_event)
        workbook_event = workbook_by_source.get(
            (stored_event.source_row, stored_event.source_event_group)
        )
        if workbook_event is not None:
            desired_registrations[stored_event.aircraft_id].add(
                workbook_event.registration
            )

    registration_changes = []
    registration_conflicts = []
    for aircraft_id, registrations in sorted(desired_registrations.items()):
        aircraft = db.session.get(Aircraft, aircraft_id)
        if aircraft is None or registrations == {aircraft.registration}:
            continue
        mapped_events = [
            item
            for item in stored_by_aircraft[aircraft_id]
            if (item.source_row, item.source_event_group) in workbook_by_source
        ]
        if len(registrations) != 1 or len(mapped_events) != len(stored_by_aircraft[aircraft_id]):
            registration_conflicts.append(
                f"{aircraft.registration} -> {', '.join(sorted(registrations))}"
            )
            continue
        desired_registration = next(iter(registrations))
        existing = db.session.scalar(
            select(Aircraft).where(Aircraft.registration == desired_registration)
        )
        if existing is not None and existing.aircraft_id != aircraft_id:
            registration_conflicts.append(
                f"{aircraft.registration} -> {desired_registration} (already exists)"
            )
            continue
        previous_registration = aircraft.registration
        aircraft.registration = desired_registration
        registration_changes.append(
            {
                "aircraft_id": aircraft_id,
                "previous_registration": previous_registration,
                "workbook_registration": desired_registration,
                "preserved_spotting_ids": [item.spotting_id for item in mapped_events],
            }
        )

    if registration_conflicts:
        raise ValueError(
            "Workbook registration corrections are ambiguous: "
            + "; ".join(registration_conflicts)
        )

    models_by_registration = defaultdict(set)
    for event in events:
        if event.aircraft_type:
            models_by_registration[event.registration].add(event.aircraft_type)

    conflicts = {
        registration: sorted(models)
        for registration, models in models_by_registration.items()
        if len(models) > 1
    }
    if conflicts:
        raise ValueError(
            "Workbook contains conflicting aircraft models for the same registration: "
            + "; ".join(
                f"{registration}={', '.join(models)}"
                for registration, models in sorted(conflicts.items())
            )
        )

    target_models = {
        registration: next(iter(models))
        for registration, models in models_by_registration.items()
    }
    aircraft_by_registration = {
        aircraft.registration: aircraft
        for aircraft in db.session.scalars(
            select(Aircraft).where(
                Aircraft.registration.in_(target_models)
            )
        ).all()
    }

    changed = []
    unchanged = 0
    missing_aircraft = []
    for registration, workbook_model in sorted(target_models.items()):
        aircraft = aircraft_by_registration.get(registration)
        if aircraft is None:
            missing_aircraft.append(registration)
            continue
        previous_model = aircraft.aircraft_type.model if aircraft.aircraft_type else None
        if previous_model == workbook_model:
            unchanged += 1
            continue
        aircraft.aircraft_type = _get_or_create(AircraftType, model=workbook_model)
        changed.append(
            {
                "registration": registration,
                "previous_model": previous_model,
                "workbook_model": workbook_model,
            }
        )

    if commit:
        db.session.commit()
    return {
        "workbook_registrations": len(target_models),
        "registration_changes": registration_changes,
        "changed": len(changed),
        "unchanged": unchanged,
        "missing_aircraft": missing_aircraft,
        "changes": changed,
    }


def _text_key(value):
    return str(value or "").strip().casefold()


def _event_identity(event):
    """Build a row-independent identity for safely retaining local additions."""
    return (
        _text_key(event.aircraft.registration),
        event.spotting_date,
        _text_key(event.flight_number),
        _text_key(event.spotting_location_raw),
        _text_key(event.route_text_original),
    )


def _capture_local_additions():
    events = db.session.scalars(select(SpottingEvent)).all()
    event_counts = Counter(_event_identity(event) for event in events)
    photos_by_event = defaultdict(list)
    for photo in db.session.scalars(select(Photo)).all():
        photos_by_event[photo.spotting_id].append(
            {
                "file_path": photo.file_path,
                "captured_at": photo.captured_at,
                "is_primary": photo.is_primary,
            }
        )
    truths_by_event = {
        truth.spotting_id: {
            "expected_adsb_flight_id": truth.expected_adsb_flight_id,
            "verification_method": truth.verification_method,
            "verified_at": truth.verified_at,
            "notes": truth.notes,
        }
        for truth in db.session.scalars(select(GroundTruthMatch)).all()
    }

    additions = {}
    for event in events:
        identity = _event_identity(event)
        if event_counts[identity] != 1:
            continue
        photos = photos_by_event.get(event.spotting_id, [])
        ground_truth = truths_by_event.get(event.spotting_id)
        if event.notes or photos or ground_truth:
            additions[identity] = {
                "notes": event.notes,
                "photos": photos,
                "ground_truth": ground_truth,
            }
    return additions


def _restore_local_additions(additions):
    new_events = db.session.scalars(select(SpottingEvent)).all()
    event_counts = Counter(_event_identity(event) for event in new_events)
    restored_photos = 0
    restored_notes = 0
    restored_ground_truths = 0
    skipped_ground_truths = 0
    for event in new_events:
        identity = _event_identity(event)
        addition = additions.get(identity)
        if addition is None or event_counts[identity] != 1:
            continue
        if addition["notes"]:
            event.notes = addition["notes"]
            restored_notes += 1
        for photo_data in addition["photos"]:
            db.session.add(Photo(spotting_id=event.spotting_id, **photo_data))
            restored_photos += 1
        truth_data = addition["ground_truth"]
        if truth_data:
            expected_flight_id = truth_data["expected_adsb_flight_id"]
            expected_flight_exists = (
                expected_flight_id is None
                or db.session.get(AdsbFlight, expected_flight_id) is not None
            )
            if expected_flight_exists:
                db.session.add(
                    GroundTruthMatch(spotting_id=event.spotting_id, **truth_data)
                )
                restored_ground_truths += 1
            else:
                skipped_ground_truths += 1
    return (
        restored_photos,
        restored_notes,
        restored_ground_truths,
        skipped_ground_truths,
    )


def replace_normalized_events(events: list[NormalizedSpottingEvent]) -> dict:
    """Replace the personal archive while preserving independent ADS-B data.

    The workbook is parsed before any deletion. An empty or entirely invalid
    workbook is rejected so a mistaken upload cannot erase the current archive.
    """
    events = list(events)
    valid_count = sum(event.spotting_date is not None for event in events)
    if not events or valid_count == 0:
        raise ValueError("Replacement workbook contains no valid spotting events.")

    local_additions = _capture_local_additions()
    removed_events = db.session.scalar(
        select(func.count(SpottingEvent.spotting_id))
    ) or 0
    try:
        event_ids = select(SpottingEvent.spotting_id)
        db.session.execute(
            delete(GroundTruthMatch).where(GroundTruthMatch.spotting_id.in_(event_ids))
        )
        db.session.execute(
            delete(MatchResult).where(MatchResult.spotting_id.in_(event_ids))
        )
        db.session.execute(delete(Photo).where(Photo.spotting_id.in_(event_ids)))
        db.session.execute(delete(SpottingEvent))
        db.session.flush()

        report = import_normalized_events(events, commit=False)
        db.session.flush()
        (
            restored_photos,
            restored_notes,
            restored_ground_truths,
            skipped_ground_truths,
        ) = _restore_local_additions(local_additions)

        referenced_aircraft = select(SpottingEvent.aircraft_id)
        stale_aircraft = db.session.scalars(
            select(Aircraft).where(Aircraft.aircraft_id.not_in(referenced_aircraft))
        ).all()
        for aircraft in stale_aircraft:
            db.session.delete(aircraft)
        db.session.flush()

        for aircraft_type in db.session.scalars(select(AircraftType)).all():
            still_used = db.session.scalar(
                select(Aircraft.aircraft_id)
                .where(Aircraft.aircraft_type_id == aircraft_type.aircraft_type_id)
                .limit(1)
            )
            if still_used is None:
                db.session.delete(aircraft_type)

        for airline in db.session.scalars(select(Airline)).all():
            used_by_aircraft = db.session.scalar(
                select(Aircraft.aircraft_id)
                .where(Aircraft.airline_id == airline.airline_id)
                .limit(1)
            )
            used_by_event = db.session.scalar(
                select(SpottingEvent.spotting_id)
                .where(SpottingEvent.observed_airline_id == airline.airline_id)
                .limit(1)
            )
            if used_by_aircraft is None and used_by_event is None:
                db.session.delete(airline)

        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    report["removed_events"] = removed_events
    report["restored_photos"] = restored_photos
    report["restored_notes"] = restored_notes
    report["restored_ground_truths"] = restored_ground_truths
    report["skipped_ground_truths"] = skipped_ground_truths
    report["mode"] = "replace"
    return report
