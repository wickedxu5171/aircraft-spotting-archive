from datetime import datetime, timezone

from sqlalchemy import select

from ..extensions import db
from ..models import AdsbFlight, Airport, TrackPoint


def _utc_datetime(epoch_seconds):
    return datetime.fromtimestamp(int(epoch_seconds), tz=timezone.utc).replace(tzinfo=None)


def _get_or_create_airport(icao_code):
    if not icao_code:
        return None
    icao_code = str(icao_code).strip().upper()
    airport = db.session.scalar(select(Airport).where(Airport.icao_code == icao_code))
    if airport is None:
        airport = Airport(icao_code=icao_code, name=icao_code)
        db.session.add(airport)
        db.session.flush()
    return airport


def import_opensky_flights(rows: list[dict]) -> dict:
    inserted = 0
    skipped_existing = 0
    skipped_invalid = 0

    for row in rows:
        icao24 = str(row.get("icao24") or "").strip().lower()
        first_seen = row.get("firstseen")
        last_seen = row.get("lastseen")
        if len(icao24) != 6 or first_seen is None or last_seen is None:
            skipped_invalid += 1
            continue

        source_flight_id = f"{icao24}:{int(first_seen)}:{int(last_seen)}"
        existing = db.session.scalar(
            select(AdsbFlight).where(
                AdsbFlight.source_name == "OpenSky",
                AdsbFlight.source_flight_id == source_flight_id,
            )
        )
        if existing:
            skipped_existing += 1
            continue

        db.session.add(
            AdsbFlight(
                source_name="OpenSky",
                source_flight_id=source_flight_id,
                icao24=icao24,
                callsign=(str(row.get("callsign") or "").strip().upper() or None),
                first_seen=_utc_datetime(first_seen),
                last_seen=_utc_datetime(last_seen),
                origin_airport=_get_or_create_airport(row.get("estdepartureairport")),
                destination_airport=_get_or_create_airport(row.get("estarrivalairport")),
            )
        )
        inserted += 1

    db.session.commit()
    return {
        "inserted": inserted,
        "skipped_existing": skipped_existing,
        "skipped_invalid": skipped_invalid,
    }


def import_opensky_track(adbs_flight: AdsbFlight, rows: list[dict]) -> dict:
    inserted = 0
    skipped_existing = 0
    skipped_invalid = 0

    for row in rows:
        observed_at_raw = row.get("time")
        if observed_at_raw is None or row.get("lat") is None or row.get("lon") is None:
            skipped_invalid += 1
            continue
        observed_at = _utc_datetime(observed_at_raw)
        existing = db.session.scalar(
            select(TrackPoint).where(
                TrackPoint.adsb_flight_id == adbs_flight.adsb_flight_id,
                TrackPoint.observed_at == observed_at,
            )
        )
        if existing:
            skipped_existing += 1
            continue
        db.session.add(
            TrackPoint(
                adsb_flight=adbs_flight,
                observed_at=observed_at,
                latitude=row["lat"],
                longitude=row["lon"],
                baro_altitude=row.get("baroaltitude"),
                geo_altitude=row.get("geoaltitude"),
                ground_speed=row.get("velocity"),
                track_heading=row.get("heading"),
                vertical_rate=row.get("vertrate"),
                on_ground=row.get("onground"),
            )
        )
        inserted += 1

    db.session.commit()
    return {
        "inserted": inserted,
        "skipped_existing": skipped_existing,
        "skipped_invalid": skipped_invalid,
    }
