import csv
import re
from pathlib import Path

import requests
from sqlalchemy import select

from ..extensions import db
from ..models import AdsbFlight, Aircraft, AircraftType


OPENSKY_AIRCRAFT_DATABASE_URL = (
    "https://opensky-network.org/datasets/metadata/aircraftDatabase.csv"
)
ICAO24_PATTERN = re.compile(r"^[0-9a-f]{6}$")
TYPECODE_DETAILS = {
    "A20N": ("Airbus", "Airbus A320neo"),
    "A21N": ("Airbus", "Airbus A321neo"),
    "A319": ("Airbus", "Airbus A319"),
    "A320": ("Airbus", "Airbus A320"),
    "A333": ("Airbus", "Airbus A330-300"),
    "A339": ("Airbus", "Airbus A330-900"),
    "A35K": ("Airbus", "Airbus A350-1000"),
    "A388": ("Airbus", "Airbus A380"),
    "B763": ("Boeing", "Boeing 767-300"),
    "B772": ("Boeing", "Boeing 777-200"),
    "B77W": ("Boeing", "Boeing 777-300ER"),
    "B788": ("Boeing", "Boeing 787-8"),
    "B789": ("Boeing", "Boeing 787-9"),
    "B78X": ("Boeing", "Boeing 787-10"),
}


def apply_typecode_metadata(aircraft: Aircraft, typecode: str | None) -> dict | None:
    """Apply public type metadata without replacing the workbook display model.

    The personal workbook is the archive's source of truth for the model shown
    on the website. Public ICAO type codes are still useful for filling an
    otherwise missing model, but their broader labels (for example ``A388`` ->
    ``Airbus A380``) must not replace a recorded subtype such as
    ``Airbus A380-841``.
    """
    normalized_code = str(typecode or "").strip().upper()
    details = TYPECODE_DETAILS.get(normalized_code)
    if details is None:
        return None
    manufacturer, model = details
    previous_model = aircraft.aircraft_type.model if aircraft.aircraft_type else None

    if aircraft.aircraft_type is not None:
        if previous_model == model:
            aircraft.aircraft_type.manufacturer = (
                aircraft.aircraft_type.manufacturer or manufacturer
            )
            aircraft.aircraft_type.icao_type_code = normalized_code
            return None
        return {
            "registration": aircraft.registration,
            "workbook_type": previous_model,
            "metadata_type": model,
            "icao_type_code": normalized_code,
            "action": "preserved_workbook_type",
        }

    aircraft_type = db.session.scalar(
        select(AircraftType).where(AircraftType.model == model)
    )
    if aircraft_type is None:
        aircraft_type = AircraftType(
            manufacturer=manufacturer,
            model=model,
            icao_type_code=normalized_code,
        )
        db.session.add(aircraft_type)
        db.session.flush()
    else:
        aircraft_type.manufacturer = aircraft_type.manufacturer or manufacturer
        aircraft_type.icao_type_code = normalized_code
    aircraft.aircraft_type = aircraft_type
    return {
        "registration": aircraft.registration,
        "workbook_type": previous_model,
        "metadata_type": model,
        "icao_type_code": normalized_code,
        "action": "filled_missing_type",
    }


def download_opensky_aircraft_metadata(destination: str | Path) -> Path:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    with requests.get(OPENSKY_AIRCRAFT_DATABASE_URL, stream=True, timeout=60) as response:
        response.raise_for_status()
        with temporary.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    output.write(chunk)
    temporary.replace(destination)
    return destination


def import_opensky_aircraft_metadata(csv_path: str | Path) -> dict:
    aircraft_by_registration = {
        aircraft.registration.upper(): aircraft
        for aircraft in db.session.scalars(select(Aircraft)).all()
    }
    matched = 0
    unchanged = 0
    conflicts = []
    seen_metadata_rows = 0
    type_metadata_results = []

    with Path(csv_path).open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required = {"icao24", "registration"}
        if not reader.fieldnames or not required.issubset(
            {name.lower() for name in reader.fieldnames}
        ):
            raise ValueError("Metadata CSV must contain icao24 and registration columns.")

        normalized_fields = {name.lower(): name for name in reader.fieldnames}
        for row in reader:
            registration = str(row.get(normalized_fields["registration"]) or "").strip().upper()
            icao24 = str(row.get(normalized_fields["icao24"]) or "").strip().lower()
            aircraft = aircraft_by_registration.get(registration)
            if aircraft is None or not ICAO24_PATTERN.fullmatch(icao24):
                continue
            seen_metadata_rows += 1
            type_result = apply_typecode_metadata(
                aircraft, row.get(normalized_fields.get("typecode", ""))
            )
            if type_result:
                type_metadata_results.append(type_result)
            if aircraft.icao24 and aircraft.icao24.lower() != icao24:
                conflicts.append(
                    {
                        "registration": registration,
                        "existing_icao24": aircraft.icao24,
                        "metadata_icao24": icao24,
                    }
                )
                continue
            if aircraft.icao24 == icao24:
                unchanged += 1
            else:
                aircraft.icao24 = icao24
                matched += 1

    db.session.flush()
    mapped_aircraft = {
        aircraft.icao24.lower(): aircraft.registration
        for aircraft in aircraft_by_registration.values()
        if aircraft.icao24
    }
    flights_updated = 0
    for flight in db.session.scalars(
        select(AdsbFlight).where(AdsbFlight.registration.is_(None))
    ).all():
        registration = mapped_aircraft.get(flight.icao24.lower())
        if registration:
            flight.registration = registration
            flights_updated += 1
    db.session.commit()

    return {
        "target_aircraft": len(aircraft_by_registration),
        "metadata_matches_seen": seen_metadata_rows,
        "aircraft_updated": matched,
        "aircraft_unchanged": unchanged,
        "adsb_flights_updated": flights_updated,
        "conflict_count": len(conflicts),
        "conflicts": conflicts,
        "aircraft_types_updated": sum(
            item["action"] == "filled_missing_type"
            for item in type_metadata_results
        ),
        "workbook_types_preserved": sum(
            item["action"] == "preserved_workbook_type"
            for item in type_metadata_results
        ),
        "type_metadata_results": type_metadata_results,
    }
