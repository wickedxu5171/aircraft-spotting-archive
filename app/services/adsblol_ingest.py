from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from math import asin, cos, radians, sin, sqrt

from sqlalchemy import delete, func, select

from ..extensions import db
from ..models import (
    AdsbFlight,
    Airport,
    GroundTruthMatch,
    MatchResult,
    TrackPoint,
)


FEET_TO_METRES = 0.3048
KNOTS_TO_METRES_PER_SECOND = 0.514444
FEET_PER_MINUTE_TO_METRES_PER_SECOND = 0.00508


@dataclass(frozen=True)
class AdsbLolPoint:
    observed_at: datetime
    latitude: float
    longitude: float
    baro_altitude: float | None
    geo_altitude: float | None
    ground_speed: float | None
    track_heading: float | None
    vertical_rate: float | None
    on_ground: bool
    callsign: str | None
    starts_new_leg: bool


def _number(value):
    return float(value) if isinstance(value, (int, float)) else None


def parse_trace(payload: dict) -> list[AdsbLolPoint]:
    base_timestamp = _number(payload.get("timestamp"))
    if base_timestamp is None:
        return []
    points = []
    last_callsign = None
    for row in payload.get("trace") or []:
        if not isinstance(row, list) or len(row) < 8:
            continue
        latitude = _number(row[1])
        longitude = _number(row[2])
        if latitude is None or longitude is None:
            continue
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            continue

        details = row[8] if len(row) > 8 and isinstance(row[8], dict) else {}
        source_type = row[9] if len(row) > 9 else details.get("type")
        if source_type and not str(source_type).startswith("adsb"):
            continue
        raw_callsign = details.get("flight")
        if raw_callsign:
            last_callsign = str(raw_callsign).strip().upper() or None

        raw_altitude = row[3]
        on_ground = raw_altitude == "ground"
        baro_altitude_ft = None if on_ground else _number(raw_altitude)
        geo_altitude_ft = _number(row[10]) if len(row) > 10 else None
        flags = int(row[6] or 0)
        observed_at = datetime.fromtimestamp(
            base_timestamp + float(row[0]), tz=timezone.utc
        ).replace(tzinfo=None)
        points.append(
            AdsbLolPoint(
                observed_at=observed_at,
                latitude=latitude,
                longitude=longitude,
                baro_altitude=(
                    baro_altitude_ft * FEET_TO_METRES
                    if baro_altitude_ft is not None
                    else None
                ),
                geo_altitude=(
                    geo_altitude_ft * FEET_TO_METRES
                    if geo_altitude_ft is not None
                    else None
                ),
                ground_speed=(
                    _number(row[4]) * KNOTS_TO_METRES_PER_SECOND
                    if _number(row[4]) is not None
                    else None
                ),
                track_heading=_number(row[5]),
                vertical_rate=(
                    _number(row[7]) * FEET_PER_MINUTE_TO_METRES_PER_SECOND
                    if _number(row[7]) is not None
                    else None
                ),
                on_ground=on_ground,
                callsign=last_callsign,
                starts_new_leg=bool(flags & 2),
            )
        )
    return points


def split_legs(points: list[AdsbLolPoint], *, max_gap_seconds=2700):
    legs = []
    current = []
    for point in points:
        gap = (
            (point.observed_at - current[-1].observed_at).total_seconds()
            if current
            else 0
        )
        same_callsign = bool(
            current
            and point.callsign
            and current[-1].callsign
            and point.callsign == current[-1].callsign
        )
        callsign_changed = bool(
            current
            and point.callsign
            and current[-1].callsign
            and point.callsign != current[-1].callsign
        )
        hard_gap = gap > 12 * 60 * 60
        if current and (
            hard_gap
            or callsign_changed
            or (point.starts_new_leg and not same_callsign)
            or (gap > max_gap_seconds and not same_callsign)
        ):
            legs.append(current)
            current = []
        current.append(point)
    if current:
        legs.append(current)
    return [leg for leg in legs if len(leg) >= 2]


def delete_adsblol_day(archive_date: date) -> dict:
    """Delete one previously imported ADSB.lol day before a clean re-import."""
    day_start = datetime.combine(archive_date, time.min)
    day_end = day_start + timedelta(days=1)
    flight_ids = select(AdsbFlight.adsb_flight_id).where(
        AdsbFlight.source_name == "ADSB.lol",
        AdsbFlight.first_seen < day_end,
        AdsbFlight.last_seen >= day_start,
    )
    removed_flights = db.session.scalar(
        select(func.count(AdsbFlight.adsb_flight_id)).where(
            AdsbFlight.adsb_flight_id.in_(flight_ids)
        )
    ) or 0
    db.session.execute(
        delete(GroundTruthMatch).where(
            GroundTruthMatch.expected_adsb_flight_id.in_(flight_ids)
        )
    )
    db.session.execute(
        delete(MatchResult).where(MatchResult.adsb_flight_id.in_(flight_ids))
    )
    db.session.execute(
        delete(TrackPoint).where(TrackPoint.adsb_flight_id.in_(flight_ids))
    )
    db.session.execute(
        delete(AdsbFlight).where(AdsbFlight.adsb_flight_id.in_(flight_ids))
    )
    db.session.commit()
    return {"removed_source_flights": removed_flights}


def sample_leg(points: list[AdsbLolPoint], sample_seconds: int):
    if sample_seconds <= 0 or len(points) <= 2:
        return points
    sampled = [points[0]]
    for point in points[1:-1]:
        if (point.observed_at - sampled[-1].observed_at).total_seconds() >= sample_seconds:
            sampled.append(point)
    if points[-1].observed_at != sampled[-1].observed_at:
        sampled.append(points[-1])
    return sampled


def _haversine_km(latitude_1, longitude_1, latitude_2, longitude_2):
    radius = 6371.0088
    lat_1, lon_1, lat_2, lon_2 = map(
        radians, (latitude_1, longitude_1, latitude_2, longitude_2)
    )
    delta_latitude = lat_2 - lat_1
    delta_longitude = lon_2 - lon_1
    value = (
        sin(delta_latitude / 2) ** 2
        + cos(lat_1) * cos(lat_2) * sin(delta_longitude / 2) ** 2
    )
    return 2 * radius * asin(sqrt(value))


def _nearest_airport(point: AdsbLolPoint, airports, *, maximum_km=40):
    candidates = []
    for airport in airports:
        if airport.latitude is None or airport.longitude is None:
            continue
        distance = _haversine_km(
            point.latitude,
            point.longitude,
            float(airport.latitude),
            float(airport.longitude),
        )
        if distance <= maximum_km:
            candidates.append((distance, airport))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def import_adsblol_traces(payloads: dict[str, dict], *, sample_seconds=60) -> dict:
    airports = db.session.scalars(select(Airport)).all()
    report = {
        "aircraft": len(payloads),
        "legs_seen": 0,
        "flights_inserted": 0,
        "flights_existing": 0,
        "track_points_inserted": 0,
        "short_legs_skipped": 0,
    }
    for icao24, payload in payloads.items():
        registration = str(payload.get("r") or "").strip().upper() or None
        for leg in split_legs(parse_trace(payload)):
            report["legs_seen"] += 1
            if (leg[-1].observed_at - leg[0].observed_at).total_seconds() < 120:
                report["short_legs_skipped"] += 1
                continue
            callsigns = Counter(point.callsign for point in leg if point.callsign)
            callsign = callsigns.most_common(1)[0][0] if callsigns else None
            source_flight_id = (
                f"{icao24}:{int(leg[0].observed_at.timestamp())}:"
                f"{int(leg[-1].observed_at.timestamp())}"
            )
            flight = db.session.scalar(
                select(AdsbFlight).where(
                    AdsbFlight.source_name == "ADSB.lol",
                    AdsbFlight.source_flight_id == source_flight_id,
                )
            )
            if flight is None:
                flight = AdsbFlight(
                    source_name="ADSB.lol",
                    source_flight_id=source_flight_id,
                    icao24=icao24,
                    registration=registration,
                    callsign=callsign,
                    first_seen=leg[0].observed_at,
                    last_seen=leg[-1].observed_at,
                    origin_airport=_nearest_airport(leg[0], airports),
                    destination_airport=_nearest_airport(leg[-1], airports),
                )
                db.session.add(flight)
                db.session.flush()
                report["flights_inserted"] += 1
            else:
                report["flights_existing"] += 1
                if flight.origin_airport is None:
                    flight.origin_airport = _nearest_airport(leg[0], airports)
                if flight.destination_airport is None:
                    flight.destination_airport = _nearest_airport(leg[-1], airports)
                if flight.callsign is None:
                    flight.callsign = callsign

            existing_points = db.session.scalar(
                select(func.count(TrackPoint.track_point_id)).where(
                    TrackPoint.adsb_flight_id == flight.adsb_flight_id
                )
            )
            if existing_points:
                continue
            for point in sample_leg(leg, sample_seconds):
                db.session.add(
                    TrackPoint(
                        adsb_flight=flight,
                        observed_at=point.observed_at,
                        latitude=point.latitude,
                        longitude=point.longitude,
                        baro_altitude=point.baro_altitude,
                        geo_altitude=point.geo_altitude,
                        ground_speed=point.ground_speed,
                        track_heading=point.track_heading,
                        vertical_rate=point.vertical_rate,
                        on_ground=point.on_ground,
                    )
                )
                report["track_points_inserted"] += 1
    db.session.commit()
    return report
