from datetime import date

import pytest
from sqlalchemy import select

from app import create_app
from app.extensions import db
from app.models import (
    Aircraft,
    AircraftType,
    Airline,
    Airport,
    GroundTruthMatch,
    Photo,
    SpottingEvent,
)


class TestConfig:
    TESTING = True
    SECRET_KEY = "test"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_TRACK_MODIFICATIONS = False


@pytest.fixture()
def archive_app(tmp_path):
    app = create_app(TestConfig)
    app.config["UPLOAD_FOLDER"] = tmp_path / "uploads"
    with app.app_context():
        db.create_all()
        airline = Airline(name="British Airways")
        airport = Airport(
            name="London Heathrow Airport",
            iata_code="LHR",
            icao_code="EGLL",
        )
        a350 = AircraftType(model="Airbus A350-1041")
        b787 = AircraftType(model="Boeing 787-10")
        events = [
            SpottingEvent(
                aircraft=Aircraft(
                    registration="G-LATE", airline=airline, aircraft_type=a350
                ),
                observed_airline=airline,
                spotting_date=date(2026, 5, 16),
                spotting_location_raw="London Heathrow",
                airport=airport,
                source_row=1,
                source_event_group=1,
            ),
            SpottingEvent(
                aircraft=Aircraft(
                    registration="G-ALFA", airline=airline, aircraft_type=a350
                ),
                observed_airline=airline,
                spotting_date=date(2026, 5, 16),
                spotting_location_raw="London Heathrow",
                airport=airport,
                source_row=2,
                source_event_group=1,
            ),
            SpottingEvent(
                aircraft=Aircraft(
                    registration="G-PHOTO", airline=airline, aircraft_type=b787
                ),
                observed_airline=airline,
                spotting_date=date(2025, 11, 5),
                spotting_location_raw="London Heathrow",
                airport=airport,
                source_row=3,
                source_event_group=1,
            ),
        ]
        db.session.add_all(events)
        db.session.flush()
        db.session.add_all(
            [
                Photo(
                    spotting_id=events[2].spotting_id,
                    file_path="archive-photo.jpg",
                    is_primary=True,
                ),
                GroundTruthMatch(
                    spotting_id=events[1].spotting_id,
                    verification_method="Photo metadata",
                    notes="Independent review",
                ),
            ]
        )
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()


def test_archive_splits_photo_states_and_shows_compact_markers(archive_app):
    response = archive_app.test_client().get("/")
    assert response.status_code == 200
    page = response.get_data(as_text=True)

    assert "2</strong> awaiting images" in page
    assert "1</strong> with images" in page
    assert "Missing images" in page
    assert "Images uploaded" in page
    assert "IMG ?" in page
    assert "IMG ✓" in page
    assert "ADS-B ✓" in page
    assert "ADS-B ?" in page
    assert "Always keep longing for the blue sky" in page
    assert "Tianyang's Aircraft Spotting Log Website" in page
    assert page.index("G-ALFA") < page.index("G-LATE") < page.index("G-PHOTO")


def test_archive_navigation_family_filters_match_specific_variants(archive_app):
    client = archive_app.test_client()

    a350_page = client.get("/?aircraft_type=Airbus+A350").get_data(as_text=True)
    assert "2 sightings shown" in a350_page
    assert "G-ALFA" in a350_page
    assert "G-LATE" in a350_page
    assert "G-PHOTO" not in a350_page

    b787_page = client.get("/?aircraft_type=Boeing+787").get_data(as_text=True)
    assert "1 sightings shown" in b787_page
    assert "G-PHOTO" in b787_page
    assert "G-ALFA" not in b787_page


def test_new_photo_moves_record_to_uploaded_section(archive_app):
    with archive_app.app_context():
        event = db.session.scalar(
            select(SpottingEvent)
            .join(SpottingEvent.aircraft)
            .where(Aircraft.registration == "G-LATE")
        )
        db.session.add(
            Photo(
                spotting_id=event.spotting_id,
                file_path="new-photo.jpg",
                is_primary=True,
            )
        )
        db.session.commit()

    page = archive_app.test_client().get("/").get_data(as_text=True)
    assert "1</strong> awaiting images" in page
    assert "2</strong> with images" in page
