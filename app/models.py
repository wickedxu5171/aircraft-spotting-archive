from datetime import datetime, timezone

from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME

from .extensions import db


BIGINT = db.BigInteger().with_variant(db.Integer(), "sqlite")
PRECISE_DATETIME = db.DateTime().with_variant(MYSQL_DATETIME(fsp=6), "mysql")


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Airline(db.Model):
    __tablename__ = "airlines"

    airline_id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    iata_code = db.Column(db.String(2))
    icao_code = db.Column(db.String(3))
    country = db.Column(db.String(80))


class AircraftType(db.Model):
    __tablename__ = "aircraft_types"

    aircraft_type_id = db.Column(db.Integer, primary_key=True)
    manufacturer = db.Column(db.String(80))
    model = db.Column(db.String(100), nullable=False, unique=True)
    icao_type_code = db.Column(db.String(4))


class Aircraft(db.Model):
    __tablename__ = "aircraft"

    aircraft_id = db.Column(db.Integer, primary_key=True)
    registration = db.Column(db.String(16), nullable=False, unique=True, index=True)
    icao24 = db.Column(db.String(6), unique=True, index=True)
    airline_id = db.Column(db.Integer, db.ForeignKey("airlines.airline_id"))
    aircraft_type_id = db.Column(
        db.Integer, db.ForeignKey("aircraft_types.aircraft_type_id")
    )
    fleet_status = db.Column(db.String(30))
    notes = db.Column(db.Text)

    airline = db.relationship("Airline")
    aircraft_type = db.relationship("AircraftType")


class Airport(db.Model):
    __tablename__ = "airports"

    airport_id = db.Column(db.Integer, primary_key=True)
    iata_code = db.Column(db.String(3), unique=True, index=True)
    icao_code = db.Column(db.String(4), unique=True, index=True)
    name = db.Column(db.String(150), nullable=False)
    latitude = db.Column(db.Numeric(9, 6))
    longitude = db.Column(db.Numeric(9, 6))


class SpottingEvent(db.Model):
    __tablename__ = "spotting_events"
    __table_args__ = (
        db.UniqueConstraint(
            "source_row", "source_event_group", name="uq_spotting_source_event"
        ),
    )

    spotting_id = db.Column(BIGINT, primary_key=True, autoincrement=True)
    aircraft_id = db.Column(
        db.Integer, db.ForeignKey("aircraft.aircraft_id"), nullable=False, index=True
    )
    observed_airline_id = db.Column(db.Integer, db.ForeignKey("airlines.airline_id"))
    spotting_date = db.Column(db.Date, nullable=False, index=True)
    spotting_time = db.Column(db.Time)
    time_precision = db.Column(db.String(12), nullable=False, default="unknown")
    airport_id = db.Column(db.Integer, db.ForeignKey("airports.airport_id"))
    spotting_location_raw = db.Column(db.String(200))
    flight_number = db.Column(db.String(12), index=True)
    route_text_original = db.Column(db.String(200))
    declared_departure_airport_id = db.Column(
        db.Integer, db.ForeignKey("airports.airport_id")
    )
    declared_arrival_airport_id = db.Column(
        db.Integer, db.ForeignKey("airports.airport_id")
    )
    notes = db.Column(db.Text)
    source_row = db.Column(db.Integer)
    source_event_group = db.Column(db.Integer)
    quality_status = db.Column(db.String(12), nullable=False, default="ready")
    quality_reasons = db.Column(db.JSON)

    aircraft = db.relationship("Aircraft")
    observed_airline = db.relationship("Airline")
    airport = db.relationship("Airport", foreign_keys=[airport_id])
    declared_departure_airport = db.relationship(
        "Airport", foreign_keys=[declared_departure_airport_id]
    )
    declared_arrival_airport = db.relationship(
        "Airport", foreign_keys=[declared_arrival_airport_id]
    )


class Photo(db.Model):
    __tablename__ = "photos"

    photo_id = db.Column(BIGINT, primary_key=True, autoincrement=True)
    spotting_id = db.Column(
        BIGINT,
        db.ForeignKey("spotting_events.spotting_id"),
        nullable=False,
        index=True,
    )
    file_path = db.Column(db.String(500), nullable=False)
    captured_at = db.Column(PRECISE_DATETIME)
    is_primary = db.Column(db.Boolean, nullable=False, default=False)

    spotting_event = db.relationship("SpottingEvent")


class AdsbFlight(db.Model):
    __tablename__ = "adsb_flights"
    __table_args__ = (
        db.UniqueConstraint(
            "source_name", "source_flight_id", name="uq_adsb_source_flight"
        ),
    )

    adsb_flight_id = db.Column(BIGINT, primary_key=True, autoincrement=True)
    source_name = db.Column(db.String(40), nullable=False)
    source_flight_id = db.Column(db.String(100))
    icao24 = db.Column(db.String(6), nullable=False, index=True)
    registration = db.Column(db.String(16), index=True)
    callsign = db.Column(db.String(12), index=True)
    first_seen = db.Column(PRECISE_DATETIME, nullable=False, index=True)
    last_seen = db.Column(PRECISE_DATETIME, nullable=False)
    origin_airport_id = db.Column(db.Integer, db.ForeignKey("airports.airport_id"))
    destination_airport_id = db.Column(
        db.Integer, db.ForeignKey("airports.airport_id")
    )
    retrieved_at = db.Column(PRECISE_DATETIME, nullable=False, default=utc_now)

    origin_airport = db.relationship("Airport", foreign_keys=[origin_airport_id])
    destination_airport = db.relationship(
        "Airport", foreign_keys=[destination_airport_id]
    )


class TrackPoint(db.Model):
    __tablename__ = "track_points"
    __table_args__ = (
        db.UniqueConstraint(
            "adsb_flight_id", "observed_at", name="uq_track_flight_time"
        ),
    )

    track_point_id = db.Column(BIGINT, primary_key=True, autoincrement=True)
    adsb_flight_id = db.Column(
        BIGINT,
        db.ForeignKey("adsb_flights.adsb_flight_id"),
        nullable=False,
        index=True,
    )
    observed_at = db.Column(PRECISE_DATETIME, nullable=False, index=True)
    latitude = db.Column(db.Numeric(9, 6), nullable=False)
    longitude = db.Column(db.Numeric(9, 6), nullable=False)
    baro_altitude = db.Column(db.Numeric(10, 2))
    geo_altitude = db.Column(db.Numeric(10, 2))
    ground_speed = db.Column(db.Numeric(10, 2))
    track_heading = db.Column(db.Numeric(7, 2))
    vertical_rate = db.Column(db.Numeric(10, 2))
    on_ground = db.Column(db.Boolean)

    adsb_flight = db.relationship("AdsbFlight")


class MatchResult(db.Model):
    __tablename__ = "match_results"
    __table_args__ = (
        db.UniqueConstraint(
            "spotting_id",
            "adsb_flight_id",
            "algorithm_version",
            name="uq_match_candidate_version",
        ),
    )

    match_result_id = db.Column(BIGINT, primary_key=True, autoincrement=True)
    spotting_id = db.Column(
        BIGINT, db.ForeignKey("spotting_events.spotting_id"), nullable=False
    )
    adsb_flight_id = db.Column(
        BIGINT, db.ForeignKey("adsb_flights.adsb_flight_id"), nullable=False
    )
    algorithm_version = db.Column(db.String(30), nullable=False)
    registration_score = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    time_score = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    airport_score = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    callsign_score = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    route_score = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    total_score = db.Column(db.Numeric(5, 2), nullable=False)
    match_status = db.Column(db.String(12), nullable=False, index=True)
    explanation = db.Column(db.JSON)
    created_at = db.Column(PRECISE_DATETIME, nullable=False, default=utc_now)

    spotting_event = db.relationship("SpottingEvent")
    adsb_flight = db.relationship("AdsbFlight")


class GroundTruthMatch(db.Model):
    __tablename__ = "ground_truth_matches"

    ground_truth_id = db.Column(BIGINT, primary_key=True, autoincrement=True)
    spotting_id = db.Column(
        BIGINT,
        db.ForeignKey("spotting_events.spotting_id"),
        nullable=False,
        unique=True,
    )
    expected_adsb_flight_id = db.Column(
        BIGINT, db.ForeignKey("adsb_flights.adsb_flight_id")
    )
    verification_method = db.Column(db.String(80), nullable=False)
    verified_at = db.Column(PRECISE_DATETIME, nullable=False, default=utc_now)
    notes = db.Column(db.Text)

    spotting_event = db.relationship("SpottingEvent")
    expected_adsb_flight = db.relationship("AdsbFlight")
