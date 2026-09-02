from datetime import date, datetime, timedelta

import pytest

from app import create_app
from app.extensions import db
from app.models import (
    AdsbFlight,
    Aircraft,
    AircraftType,
    Airline,
    Airport,
    GroundTruthMatch,
    MatchResult,
    SpottingEvent,
)
from app.services.evaluation import Assignment, build_ablation_report, calculate_metrics


class TestConfig:
    TESTING = True
    SECRET_KEY = "test"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_TRACK_MODIFICATIONS = False


@pytest.fixture()
def web_app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        airline = Airline(name="United Airlines", iata_code="UA", icao_code="UAL")
        airport = Airport(
            name="London Heathrow Airport", iata_code="LHR", icao_code="EGLL"
        )
        aircraft_type = AircraftType(model="Boeing 777-222ER")
        evaluated_aircraft = Aircraft(
            registration="G-EVAL",
            icao24="400001",
            airline=airline,
            aircraft_type=aircraft_type,
        )
        open_aircraft = Aircraft(
            registration="G-OPEN",
            icao24="400002",
            airline=airline,
            aircraft_type=aircraft_type,
        )
        evaluated_event = SpottingEvent(
            aircraft=evaluated_aircraft,
            observed_airline=airline,
            spotting_date=date(2025, 11, 5),
            spotting_location_raw="London Heathrow",
            airport=airport,
            flight_number="UA4",
            route_text_original="London Heathrow -- Houston",
            source_row=1,
            source_event_group=1,
        )
        open_event = SpottingEvent(
            aircraft=open_aircraft,
            observed_airline=airline,
            spotting_date=date(2025, 11, 5),
            spotting_location_raw="London Heathrow",
            airport=airport,
            flight_number="UA17",
            route_text_original="London Heathrow -- Newark Liberty",
            source_row=2,
            source_event_group=1,
        )
        first_seen = datetime(2025, 11, 5, 10, 0)
        evaluated_flight = AdsbFlight(
            source_name="test",
            source_flight_id="evaluated",
            icao24="400001",
            registration="G-EVAL",
            callsign="UAL4",
            first_seen=first_seen,
            last_seen=first_seen + timedelta(hours=8),
        )
        open_flight = AdsbFlight(
            source_name="test",
            source_flight_id="open",
            icao24="400002",
            registration="G-OPEN",
            callsign="UAL17",
            first_seen=first_seen,
            last_seen=first_seen + timedelta(hours=8),
        )
        db.session.add_all(
            [evaluated_event, open_event, evaluated_flight, open_flight]
        )
        db.session.flush()
        db.session.add_all(
            [
                MatchResult(
                    spotting_id=evaluated_event.spotting_id,
                    adsb_flight_id=evaluated_flight.adsb_flight_id,
                    algorithm_version="weighted-v1",
                    registration_score=40,
                    time_score=10,
                    airport_score=15,
                    callsign_score=15,
                    route_score=2,
                    total_score=82,
                    match_status="matched",
                ),
                MatchResult(
                    spotting_id=open_event.spotting_id,
                    adsb_flight_id=open_flight.adsb_flight_id,
                    algorithm_version="weighted-v1",
                    registration_score=40,
                    time_score=10,
                    airport_score=15,
                    callsign_score=0,
                    route_score=2,
                    total_score=67,
                    match_status="review",
                ),
                GroundTruthMatch(
                    spotting_id=evaluated_event.spotting_id,
                    expected_adsb_flight_id=evaluated_flight.adsb_flight_id,
                    verification_method="manual_fr24",
                    notes="Independent playback checked",
                ),
            ]
        )
        db.session.commit()
        app.config["EVALUATED_EVENT_ID"] = evaluated_event.spotting_id
        app.config["OPEN_EVENT_ID"] = open_event.spotting_id
        app.config["OPEN_FLIGHT_ID"] = open_flight.adsb_flight_id
        yield app
        db.session.remove()
        db.drop_all()


def test_evaluation_metrics_include_wrong_link_as_fp_and_fn():
    metrics = calculate_metrics(
        [
            Assignment(1, 1, True),
            Assignment(2, 3, True),
            Assignment(4, None, False),
            Assignment(None, None, False),
        ]
    )

    assert metrics["true_positive"] == 1
    assert metrics["false_positive"] == 1
    assert metrics["false_negative"] == 2
    assert metrics["true_negative"] == 1
    assert metrics["precision"] == 0.5
    assert metrics["recall"] == 1 / 3
    assert metrics["f1"] == 0.4


def test_empty_evaluation_has_undefined_rates():
    metrics = calculate_metrics([])

    assert metrics["verified"] == 0
    assert metrics["precision"] is None
    assert metrics["recall"] is None
    assert metrics["f1"] is None


def test_ablation_rescales_remaining_evidence_and_reuses_match_threshold(web_app):
    with web_app.app_context():
        from app.services.evaluation import build_evaluation_report

        report = build_evaluation_report()
        variants = {item["key"]: item for item in build_ablation_report(report["rows"])}

        assert variants["baseline"]["f1"] == 1.0
        assert variants["without_route"]["f1"] == 1.0
        assert variants["without_callsign"]["recall"] == 0.0
        assert variants["without_callsign"]["average_top_score"] == pytest.approx(
            67 / 85 * 100
        )


def test_review_filters_and_verified_candidate_styling(web_app):
    client = web_app.test_client()
    evaluated = client.get(
        "/evaluation?date=2025-11-05&location=London+Heathrow&review_status=evaluated"
    )
    assert evaluated.status_code == 200
    assert b"G-EVAL" in evaluated.data
    assert b"G-OPEN" not in evaluated.data
    assert b"Verified match" in evaluated.data
    assert b"candidate-verified" in evaluated.data
    assert b'id="count-evaluated">1' in evaluated.data
    assert b"Ground-truth ablation" in evaluated.data
    assert b"Without flight number / callsign" in evaluated.data
    assert b"Operational-frequency ambiguity" in evaluated.data
    assert b"Registration-only candidate ambiguity" in evaluated.data
    assert b"G-VPRD" in evaluated.data
    assert b"#556/VIR103M" in evaluated.data
    assert b"#555 received 4 time points and 59 overall" in evaluated.data
    assert b"current snapshot contains 25 independently reviewed records" in evaluated.data.lower()
    assert b"single-date positive-only subset" in evaluated.data

    unevaluated = client.get(
        "/evaluation?date=2025-11-05&location=London+Heathrow&review_status=unevaluated"
    )
    assert unevaluated.status_code == 200
    assert b"G-OPEN" in unevaluated.data
    assert b"G-EVAL" not in unevaluated.data
    assert b"Unevaluated \xc2\xb7 needs review" in unevaluated.data


def test_ground_truth_can_be_saved_without_page_reload(web_app):
    client = web_app.test_client()
    response = client.post(
        f"/evaluation/ground-truth/{web_app.config['OPEN_EVENT_ID']}",
        data={
            "expected_adsb_flight_id": str(web_app.config["OPEN_FLIGHT_ID"]),
            "verification_method": "manual_review",
            "notes": "Independent source confirmed",
            "return_date": "2025-11-05",
            "return_location": "London Heathrow",
            "return_review_status": "unevaluated",
        },
        headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
    )
    assert response.status_code == 200
    assert response.json["ok"] is True
    assert response.json["created"] is True
    assert response.json["expected_adsb_flight_id"] == web_app.config["OPEN_FLIGHT_ID"]

    with web_app.app_context():
        truth = db.session.scalar(
            db.select(GroundTruthMatch).where(
                GroundTruthMatch.spotting_id == web_app.config["OPEN_EVENT_ID"]
            )
        )
        assert truth is not None
        assert truth.notes == "Independent source confirmed"


def test_evaluation_exports_csv_and_json(web_app):
    client = web_app.test_client()
    csv_response = client.get(
        "/evaluation/export.csv?date=2025-11-05&location=London+Heathrow"
    )
    assert csv_response.status_code == 200
    assert csv_response.mimetype == "text/csv"
    assert b"spotting_id,spotting_date,registration" in csv_response.data
    assert b"G-EVAL" in csv_response.data

    json_response = client.get(
        "/evaluation/export.json?date=2025-11-05&location=London+Heathrow"
    )
    assert json_response.status_code == 200
    assert json_response.json["metrics"]["events"] == 2
    assert len(json_response.json["records"]) == 2
    assert json_response.json["records"][0]["evaluated"] is True
