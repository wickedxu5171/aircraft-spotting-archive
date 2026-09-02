from datetime import date, datetime, time

from app.services.match_runner import _matching_time, _observed_at
from app.services.matching import FlightCandidate, SpottingEvidence, score_candidate


def test_complete_exact_evidence_is_matched():
    spotting = SpottingEvidence(
        registration="G-XLEG",
        observed_at=datetime(2025, 11, 5, 14, 20),
        airport_code="LHR",
        flight_number="BA285",
        route_origin="LHR",
        route_destination="SFO",
    )
    candidate = FlightCandidate(
        registration="G-XLEG",
        first_seen=datetime(2025, 11, 5, 14, 0),
        last_seen=datetime(2025, 11, 5, 23, 10),
        callsign="BA285",
        airport_codes=("LHR", "SFO"),
        route_origin="LHR",
        route_destination="SFO",
    )
    result = score_candidate(spotting, candidate)
    assert result.total_score == 100
    assert result.status == "matched"


def test_date_only_registration_match_is_unmatched():
    spotting = SpottingEvidence(
        registration="G-XLEG",
        observed_at=None,
        airport_code=None,
        flight_number=None,
        spotting_date=datetime(2025, 11, 5).date(),
    )
    candidate = FlightCandidate(
        registration="G-XLEG",
        first_seen=datetime(2025, 11, 5, 14, 0),
        last_seen=datetime(2025, 11, 5, 23, 10),
        callsign="BA285",
    )
    result = score_candidate(spotting, candidate)
    assert result.total_score == 50
    assert result.status == "unmatched"


def test_iata_flight_number_matches_icao_callsign_without_exact_time():
    spotting = SpottingEvidence(
        registration="G-XLEG",
        observed_at=None,
        airport_code="LHR",
        flight_number="BA285",
        route_origin="LHR",
        route_destination="SFO",
        spotting_date=datetime(2025, 11, 5).date(),
        airline_iata="BA",
        airline_icao="BAW",
    )
    candidate = FlightCandidate(
        registration="G-XLEG",
        first_seen=datetime(2025, 11, 5, 14, 0),
        last_seen=datetime(2025, 11, 5, 23, 10),
        callsign="BAW285",
        airport_codes=("LHR", "SFO"),
        route_origin="LHR",
        route_destination="SFO",
    )
    result = score_candidate(spotting, candidate)
    assert result.total_score == 85
    assert result.status == "matched"


def test_observation_time_takes_priority_without_querying_for_a_photo():
    event = type(
        "Event",
        (),
        {"spotting_date": date(2025, 11, 5), "spotting_time": time(14, 20)},
    )()
    observed_at, source = _observed_at(event)
    assert observed_at == datetime(2025, 11, 5, 14, 20)
    assert source == "Recorded observation time"


def test_matching_uses_only_date_consistent_photo_time():
    event = type(
        "Event",
        (),
        {"spotting_date": date(2025, 11, 5), "spotting_time": None},
    )()
    valid_photo = type(
        "Photo", (), {"captured_at": datetime(2025, 11, 5, 12, 48)}
    )()
    invalid_photo = type(
        "Photo", (), {"captured_at": datetime(2026, 11, 5, 12, 48)}
    )()

    assert _matching_time(event, valid_photo) == (
        datetime(2025, 11, 5, 12, 48),
        "Primary photo capture time",
    )
    assert _matching_time(event, invalid_photo) == (None, None)
