from datetime import datetime, time, timedelta

from sqlalchemy import or_, select

from ..extensions import db
from ..models import AdsbFlight, MatchResult, Photo, SpottingEvent
from .matching import FlightCandidate, SpottingEvidence, callsign_aliases, score_candidate


def _matching_time(event: SpottingEvent, primary_photo: Photo | None = None):
    if event.spotting_time:
        return (
            datetime.combine(event.spotting_date, event.spotting_time),
            "Recorded observation time",
        )
    if (
        primary_photo is not None
        and primary_photo.captured_at is not None
        and primary_photo.captured_at.date() == event.spotting_date
    ):
        return primary_photo.captured_at, "Primary photo capture time"
    return None, None


def _observed_at(event: SpottingEvent):
    if event.spotting_time:
        return _matching_time(event)
    primary_photo = db.session.scalar(
        select(Photo)
        .where(Photo.spotting_id == event.spotting_id)
        .order_by(Photo.is_primary.desc(), Photo.photo_id)
        .limit(1)
    )
    return _matching_time(event, primary_photo)


def _airport_codes(event: SpottingEvent):
    if not event.airport:
        return ()
    return tuple(
        code
        for code in (event.airport.iata_code, event.airport.icao_code)
        if code
    )


def _candidate_airport_codes(flight: AdsbFlight):
    codes = []
    for airport in (flight.origin_airport, flight.destination_airport):
        if airport:
            codes.extend(code for code in (airport.iata_code, airport.icao_code) if code)
    return tuple(codes)


def find_candidates(event: SpottingEvent) -> list[AdsbFlight]:
    day_start = datetime.combine(event.spotting_date, time.min)
    day_end = day_start + timedelta(days=1)
    conditions = []
    if event.aircraft.icao24:
        conditions.append(AdsbFlight.icao24 == event.aircraft.icao24.lower())
    aliases = callsign_aliases(
        event.flight_number,
        event.observed_airline.iata_code if event.observed_airline else None,
        event.observed_airline.icao_code if event.observed_airline else None,
    )
    if aliases:
        conditions.append(AdsbFlight.callsign.in_(aliases))
    if not conditions:
        return []
    return db.session.scalars(
        select(AdsbFlight)
        .where(
            AdsbFlight.first_seen < day_end,
            AdsbFlight.last_seen >= day_start,
            or_(*conditions),
        )
        .order_by(AdsbFlight.first_seen)
    ).all()


def run_match_for_event(event: SpottingEvent) -> dict:
    observed_at, observed_at_source = _observed_at(event)
    evidence = SpottingEvidence(
        registration=event.aircraft.registration,
        observed_at=observed_at,
        airport_code=(event.airport.iata_code if event.airport else None),
        flight_number=event.flight_number,
        route_origin=(
            event.declared_departure_airport.iata_code
            if event.declared_departure_airport
            else None
        ),
        route_destination=(
            event.declared_arrival_airport.iata_code
            if event.declared_arrival_airport
            else None
        ),
        spotting_date=event.spotting_date,
        airline_iata=(event.observed_airline.iata_code if event.observed_airline else None),
        airline_icao=(event.observed_airline.icao_code if event.observed_airline else None),
        observed_at_source=observed_at_source,
    )
    candidates = find_candidates(event)
    results = []
    for flight in candidates:
        candidate = FlightCandidate(
            registration=flight.registration,
            first_seen=flight.first_seen,
            last_seen=flight.last_seen,
            callsign=flight.callsign,
            airport_codes=_candidate_airport_codes(flight),
            route_origin=(flight.origin_airport.iata_code if flight.origin_airport else None),
            route_destination=(
                flight.destination_airport.iata_code if flight.destination_airport else None
            ),
        )
        breakdown = score_candidate(evidence, candidate)
        stored = db.session.scalar(
            select(MatchResult).where(
                MatchResult.spotting_id == event.spotting_id,
                MatchResult.adsb_flight_id == flight.adsb_flight_id,
                MatchResult.algorithm_version == breakdown.algorithm_version,
            )
        )
        if stored is None:
            stored = MatchResult(
                spotting_event=event,
                adsb_flight=flight,
                algorithm_version=breakdown.algorithm_version,
            )
            db.session.add(stored)
        stored.registration_score = breakdown.registration_score
        stored.time_score = breakdown.time_score
        stored.airport_score = breakdown.airport_score
        stored.callsign_score = breakdown.callsign_score
        stored.route_score = breakdown.route_score
        stored.total_score = breakdown.total_score
        stored.match_status = breakdown.status
        stored.explanation = breakdown.explanation
        results.append(breakdown)
    return {
        "candidate_count": len(results),
        "matched": sum(result.status == "matched" for result in results),
        "review": sum(result.status == "review" for result in results),
        "unmatched": sum(result.status == "unmatched" for result in results),
    }


def run_matching(*, spotting_date, spotting_location_raw) -> dict:
    events = db.session.scalars(
        select(SpottingEvent).where(
            SpottingEvent.spotting_date == spotting_date,
            SpottingEvent.spotting_location_raw == spotting_location_raw,
            SpottingEvent.quality_status == "ready",
            SpottingEvent.flight_number.is_not(None),
            SpottingEvent.route_text_original.is_not(None),
        )
    ).all()
    totals = {
        "events": len(events),
        "events_with_candidates": 0,
        "candidate_count": 0,
        "matched": 0,
        "review": 0,
        "unmatched": 0,
    }
    for event in events:
        report = run_match_for_event(event)
        if report["candidate_count"]:
            totals["events_with_candidates"] += 1
        for key in ("candidate_count", "matched", "review", "unmatched"):
            totals[key] += report[key]
    db.session.commit()
    return totals
