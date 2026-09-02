from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select

from ..extensions import db
from ..models import GroundTruthMatch, MatchResult, Photo, SpottingEvent


ABLATION_VARIANTS = (
    ("baseline", "All evidence", None, 100),
    ("without_time", "Without time", "time_score", 75),
    ("without_callsign", "Without flight number / callsign", "callsign_score", 85),
    ("without_route", "Without route", "route_score", 95),
)


@dataclass(frozen=True)
class Assignment:
    expected_flight_id: int | None
    predicted_flight_id: int | None
    predicted_positive: bool


def calculate_metrics(assignments: list[Assignment]) -> dict:
    """Calculate event-level linkage metrics from verified assignments.

    A wrong positive prediction contributes one false positive and, when a
    correct flight exists, one false negative. This is the standard treatment
    for single-label record linkage.
    """
    true_positive = false_positive = false_negative = true_negative = 0

    for item in assignments:
        actual_positive = item.expected_flight_id is not None
        correct_positive = (
            item.predicted_positive
            and actual_positive
            and item.predicted_flight_id == item.expected_flight_id
        )
        if correct_positive:
            true_positive += 1
            continue
        if item.predicted_positive:
            false_positive += 1
        if actual_positive:
            false_negative += 1
        elif not item.predicted_positive:
            true_negative += 1

    def ratio(numerator: int, denominator: int):
        return numerator / denominator if denominator else None

    precision = ratio(true_positive, true_positive + false_positive)
    recall = ratio(true_positive, true_positive + false_negative)
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    return {
        "verified": len(assignments),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "true_negative": true_negative,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def build_ablation_report(rows: list[dict]) -> list[dict]:
    """Re-rank verified candidates after removing one evidence component.

    Scores are normalized to the remaining theoretical maximum so the existing
    80-point automatic-match threshold stays comparable across variants.
    """
    verified_rows = [row for row in rows if row["truth"] is not None]
    variants = []
    for key, label, omitted_field, remaining_maximum in ABLATION_VARIANTS:
        assignments = []
        top_scores = []
        correct_top = 0
        for row in verified_rows:
            rescored = []
            for candidate in row["candidates"]:
                omitted_score = (
                    float(getattr(candidate, omitted_field))
                    if omitted_field is not None
                    else 0.0
                )
                normalized_score = (
                    (float(candidate.total_score) - omitted_score)
                    / remaining_maximum
                    * 100
                )
                rescored.append((normalized_score, candidate))
            rescored.sort(
                key=lambda item: (
                    -item[0],
                    -float(item[1].total_score),
                    item[1].match_result_id,
                )
            )
            top_score, top = rescored[0] if rescored else (None, None)
            truth = row["truth"]
            if top_score is not None:
                top_scores.append(top_score)
            if top and top.adsb_flight_id == truth.expected_adsb_flight_id:
                correct_top += 1
            assignments.append(
                Assignment(
                    expected_flight_id=truth.expected_adsb_flight_id,
                    predicted_flight_id=top.adsb_flight_id if top else None,
                    predicted_positive=bool(top and top_score >= 80),
                )
            )
        metrics = calculate_metrics(assignments)
        variants.append(
            {
                "key": key,
                "label": label,
                "remaining_maximum": remaining_maximum,
                "average_top_score": (
                    sum(top_scores) / len(top_scores) if top_scores else None
                ),
                "correct_top": correct_top,
                **metrics,
            }
        )
    return variants


def build_evaluation_report(
    *, spotting_date: date | None = None, spotting_location_raw: str | None = None
) -> dict:
    event_query = select(SpottingEvent).order_by(
        SpottingEvent.spotting_date, SpottingEvent.spotting_id
    )
    if spotting_date is not None:
        event_query = event_query.where(SpottingEvent.spotting_date == spotting_date)
    if spotting_location_raw:
        event_query = event_query.where(
            SpottingEvent.spotting_location_raw == spotting_location_raw
        )
    events = db.session.scalars(event_query.limit(500)).all()
    event_ids = [event.spotting_id for event in events]

    results_by_event: dict[int, list[MatchResult]] = defaultdict(list)
    truths_by_event: dict[int, GroundTruthMatch] = {}
    photos_by_event: dict[int, Photo] = {}
    if event_ids:
        match_results = db.session.scalars(
            select(MatchResult)
            .where(MatchResult.spotting_id.in_(event_ids))
            .order_by(MatchResult.spotting_id, MatchResult.total_score.desc())
        ).all()
        for result in match_results:
            results_by_event[result.spotting_id].append(result)

        truths = db.session.scalars(
            select(GroundTruthMatch).where(
                GroundTruthMatch.spotting_id.in_(event_ids)
            )
        ).all()
        truths_by_event = {truth.spotting_id: truth for truth in truths}

        photos = db.session.scalars(
            select(Photo)
            .where(Photo.spotting_id.in_(event_ids))
            .order_by(Photo.is_primary.desc(), Photo.photo_id)
        ).all()
        for photo in photos:
            photos_by_event.setdefault(photo.spotting_id, photo)

    rows = []
    assignments = []
    matched_events = review_events = 0
    for event in events:
        candidates = results_by_event[event.spotting_id]
        top = candidates[0] if candidates else None
        truth = truths_by_event.get(event.spotting_id)
        photo = photos_by_event.get(event.spotting_id)
        if top and top.match_status == "matched":
            matched_events += 1
        elif top and top.match_status == "review":
            review_events += 1
        if truth is not None:
            assignments.append(
                Assignment(
                    expected_flight_id=truth.expected_adsb_flight_id,
                    predicted_flight_id=top.adsb_flight_id if top else None,
                    predicted_positive=bool(top and top.match_status == "matched"),
                )
            )
        rows.append(
            {
                "event": event,
                "candidates": candidates,
                "top": top,
                "truth": truth,
                "photo": photo,
                "photo_date_matches": bool(
                    photo
                    and photo.captured_at
                    and photo.captured_at.date() == event.spotting_date
                ),
            }
        )

    metrics = calculate_metrics(assignments)
    metrics.update(
        {
            "events": len(events),
            "events_with_candidates": sum(bool(row["candidates"]) for row in rows),
            "matched_events": matched_events,
            "review_events": review_events,
            "enrichment_rate": (
                matched_events / len(events) if events else None
            ),
        }
    )
    return {
        "rows": rows,
        "metrics": metrics,
        "ablation": build_ablation_report(rows),
    }


def build_date_comparison(dates: list[date], *, spotting_location_raw: str) -> list[dict]:
    comparison = []
    for selected_date in dates:
        report = build_evaluation_report(
            spotting_date=selected_date,
            spotting_location_raw=spotting_location_raw,
        )
        rows = report["rows"]
        events = [row["event"] for row in rows]
        top_candidates = [row["top"] for row in rows if row["top"] is not None]
        unique_aircraft = {event.aircraft_id: event.aircraft for event in events}
        matched = sum(item.match_status == "matched" for item in top_candidates)
        review = sum(item.match_status == "review" for item in top_candidates)
        unmatched = sum(item.match_status == "unmatched" for item in top_candidates)
        route_enriched = sum(
            bool(item.adsb_flight.origin_airport and item.adsb_flight.destination_airport)
            for item in top_candidates
        )
        comparison.append(
            {
                "date": selected_date,
                "events": len(events),
                "unique_aircraft": len(unique_aircraft),
                "metadata_mapped": sum(
                    aircraft.icao24 is not None for aircraft in unique_aircraft.values()
                ),
                "with_candidates": len(top_candidates),
                "without_candidates": len(events) - len(top_candidates),
                "candidate_rate": (
                    len(top_candidates) / len(events) if events else None
                ),
                "matched": matched,
                "review": review,
                "unmatched": unmatched,
                "route_enriched": route_enriched,
                "average_top_score": (
                    sum(float(item.total_score) for item in top_candidates)
                    / len(top_candidates)
                    if top_candidates
                    else None
                ),
            }
        )
    return comparison
