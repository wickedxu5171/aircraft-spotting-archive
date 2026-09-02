from datetime import date, datetime
from io import BytesIO

import pytest

from app import create_app
from app.extensions import db
from app.models import Aircraft, AircraftType, Airline, Airport, Photo, SpottingEvent
from app.services.photo_upload import InvalidPhotoError, save_photo_upload


class TestConfig:
    TESTING = True
    SECRET_KEY = "test"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_TRACK_MODIFICATIONS = False


@pytest.fixture()
def app(tmp_path):
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
        aircraft_type = AircraftType(model="Airbus A380-841")
        aircraft = Aircraft(
            registration="G-XLEG", airline=airline, aircraft_type=aircraft_type
        )
        event = SpottingEvent(
            aircraft=aircraft,
            observed_airline=airline,
            spotting_date=date(2026, 5, 16),
            spotting_location_raw="London Heathrow",
            airport=airport,
            flight_number="BA285",
            route_text_original="LHR -- SFO",
            source_row=8,
            source_event_group=1,
        )
        db.session.add(event)
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()


def test_rejects_non_image_content(tmp_path):
    class FakeUpload:
        filename = "not-really.jpg"
        stream = BytesIO(b"plain text")

    with pytest.raises(InvalidPhotoError):
        save_photo_upload(FakeUpload(), tmp_path)


def test_sighting_page_accepts_and_serves_local_photo(app):
    client = app.test_client()
    response = client.post(
        "/sightings/1/photos",
        data={
            "photo": (BytesIO(b"\xff\xd8\xff\xe0test-photo"), "heathrow.jpg"),
            "captured_at": "2026-05-16T14:35",
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302

    with app.app_context():
        photo = db.session.query(Photo).one()
        assert photo.file_path.endswith(".jpg")
        assert photo.is_primary is True
        assert photo.captured_at == datetime(2026, 5, 16, 14, 35)
        stored_name = photo.file_path

    image_response = client.get(f"/uploads/{stored_name}")
    assert image_response.status_code == 200
    assert image_response.data.startswith(b"\xff\xd8\xff")


def test_sighting_note_is_editable(app):
    client = app.test_client()
    response = client.post(
        "/sightings/1/notes",
        data={"notes": "Special livery · first Heathrow visit"},
    )
    assert response.status_code == 302

    with app.app_context():
        assert db.session.get(SpottingEvent, 1).notes == (
            "Special livery · first Heathrow visit"
        )

    page = client.get("/sightings/1")
    assert page.status_code == 200
    assert b"Special livery" in page.data
    assert b'id="special-note"' in page.data
    assert b'rows="3"' in page.data
    assert b"record-tools" not in page.data


def test_sighting_detail_has_four_column_ready_observation_cards(app):
    with app.app_context():
        original = db.session.get(SpottingEvent, 1)
        db.session.add(
            SpottingEvent(
                aircraft_id=original.aircraft_id,
                observed_airline_id=original.observed_airline_id,
                spotting_date=date(2025, 11, 5),
                spotting_location_raw="London Heathrow",
                flight_number="BA285",
                route_text_original="LHR -- SFO",
                source_row=8,
                source_event_group=2,
            )
        )
        db.session.commit()

    page = app.test_client().get("/sightings/1")

    assert page.status_code == 200
    assert b"Observation records" in page.data
    assert b"2 records for G-XLEG" in page.data
    assert page.data.count(b'<article class="observation-card') == 2
    assert page.data.count(b'class="observation-thumb observation-upload-trigger"') == 2
    assert b'id="photo-upload-dialog"' in page.data
    assert b"Time not recorded" in page.data
    assert b"London Heathrow (LHR/EGLL)" in page.data
    assert b'<label for="special-note">Special note</label>' in page.data

    archive = app.test_client().get("/")
    assert archive.status_code == 200
    assert b"London Heathrow (LHR/EGLL)" in archive.data
