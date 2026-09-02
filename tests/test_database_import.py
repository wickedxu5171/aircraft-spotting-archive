from datetime import date

import pytest

from app import create_app
from app.extensions import db
from app.models import Aircraft, AircraftType, GroundTruthMatch, Photo, SpottingEvent
from app.services.database_import import (
    import_normalized_events,
    replace_normalized_events,
    restore_aircraft_types_from_events,
    seed_airports,
)
from app.services.aircraft_metadata import apply_typecode_metadata
from app.services.excel_importer import NormalizedSpottingEvent


class TestConfig:
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_TRACK_MODIFICATIONS = False


def event(row, registration, location="London Heathrow", route="LHR -- SFO"):
    return NormalizedSpottingEvent(
        source_row=row,
        source_event_group=1,
        airline_raw="British Airways",
        registration=registration,
        aircraft_type_raw="Airbus A380-841",
        aircraft_type="Airbus A380-841",
        aircraft_notes=None,
        spotting_location_raw=location,
        spotting_date=date(2025, 11, 5),
        flight_number="BA285",
        route_text_original=route,
        route_departure_raw="LHR",
        route_arrival_raw="SFO",
        quality_status="ready",
        quality_reasons=(),
    )


@pytest.fixture()
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        seed_airports()
        yield app
        db.session.remove()
        db.drop_all()


def test_english_airport_aliases_populate_route_relationships(app):
    with app.app_context():
        import_normalized_events([event(8, "G-XLEG")])
        stored = db.session.query(SpottingEvent).one()

        assert stored.airport.iata_code == "LHR"
        assert stored.declared_departure_airport.iata_code == "LHR"
        assert stored.declared_arrival_airport.iata_code == "SFO"


def test_replace_removes_old_archive_without_touching_new_rows(app):
    with app.app_context():
        import_normalized_events([event(8, "G-XLEG")])
        report = replace_normalized_events([event(8, "G-XLEK")])

        assert report["removed_events"] == 1
        assert db.session.query(SpottingEvent).count() == 1
        assert {item.registration for item in db.session.query(Aircraft)} == {"G-XLEK"}


def test_replace_rejects_empty_workbook(app):
    with app.app_context():
        import_normalized_events([event(8, "G-XLEG")])

        with pytest.raises(ValueError):
            replace_normalized_events([])

        assert db.session.query(SpottingEvent).count() == 1


def test_replace_restores_photo_and_note_when_event_row_moves(app):
    with app.app_context():
        import_normalized_events([event(8, "G-XLEG")])
        original = db.session.query(SpottingEvent).one()
        original.notes = "Special livery"
        db.session.add(
            Photo(
                spotting_id=original.spotting_id,
                file_path="local-photo.jpg",
                is_primary=True,
            )
        )
        db.session.commit()

        report = replace_normalized_events([event(28, "G-XLEG")])

        replacement = db.session.query(SpottingEvent).one()
        photo = db.session.query(Photo).one()
        assert replacement.source_row == 28
        assert replacement.notes == "Special livery"
        assert photo.spotting_id == replacement.spotting_id
        assert photo.file_path == "local-photo.jpg"
        assert report["restored_photos"] == 1
        assert report["restored_notes"] == 1


def test_replace_restores_ground_truth_when_event_row_moves(app):
    with app.app_context():
        import_normalized_events([event(8, "G-XLEG")])
        original = db.session.query(SpottingEvent).one()
        db.session.add(
            GroundTruthMatch(
                spotting_id=original.spotting_id,
                expected_adsb_flight_id=None,
                verification_method="photo_metadata",
                notes="Verified no matching trace",
            )
        )
        db.session.commit()

        report = replace_normalized_events([event(28, "G-XLEG")])

        replacement = db.session.query(SpottingEvent).one()
        truth = db.session.query(GroundTruthMatch).one()
        assert truth.spotting_id == replacement.spotting_id
        assert truth.verification_method == "photo_metadata"
        assert truth.notes == "Verified no matching trace"
        assert report["restored_ground_truths"] == 1
        assert report["skipped_ground_truths"] == 0


def test_workbook_restore_replaces_generic_metadata_model_in_place(app):
    with app.app_context():
        workbook_event = event(8, "G-XLEG")
        import_normalized_events([workbook_event])
        aircraft = db.session.query(Aircraft).one()
        aircraft.aircraft_type = AircraftType(model="Airbus A380")
        db.session.commit()

        report = restore_aircraft_types_from_events([workbook_event])

        assert aircraft.aircraft_type.model == "Airbus A380-841"
        assert report["changed"] == 1


def test_workbook_restore_corrects_registration_in_place_by_source_coordinates(app):
    with app.app_context():
        original_event = event(8, "G-XIIW")
        import_normalized_events([original_event])
        stored = db.session.query(SpottingEvent).one()
        original_spotting_id = stored.spotting_id

        corrected_event = event(8, "G-VIIW")
        report = restore_aircraft_types_from_events([corrected_event])

        aircraft = db.session.query(Aircraft).one()
        assert aircraft.registration == "G-VIIW"
        assert db.session.query(SpottingEvent).one().spotting_id == original_spotting_id
        assert report["registration_changes"] == [
            {
                "aircraft_id": aircraft.aircraft_id,
                "previous_registration": "G-XIIW",
                "workbook_registration": "G-VIIW",
                "preserved_spotting_ids": [original_spotting_id],
            }
        ]


def test_public_typecode_does_not_override_workbook_model(app):
    with app.app_context():
        import_normalized_events([event(8, "G-XLEG")])
        aircraft = db.session.query(Aircraft).one()

        result = apply_typecode_metadata(aircraft, "A388")

        assert aircraft.aircraft_type.model == "Airbus A380-841"
        assert result["action"] == "preserved_workbook_type"
        assert result["metadata_type"] == "Airbus A380"
