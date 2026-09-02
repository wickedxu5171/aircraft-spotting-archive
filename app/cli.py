import json
from collections import Counter
from datetime import date, timedelta

import click
from flask import current_app
from sqlalchemy import select

from .extensions import db
from .integrations.opensky_trino import (
    OpenSkyConfigurationError,
    OpenSkyTrinoClient,
    write_query_artifact,
)
from .integrations.adsblol_archive import discover_trace_metadata, extract_trace_payloads
from .models import AdsbFlight, Aircraft, SpottingEvent
from .services.aircraft_metadata import (
    ICAO24_PATTERN,
    apply_typecode_metadata,
    download_opensky_aircraft_metadata,
    import_opensky_aircraft_metadata,
)
from .services.adsblol_ingest import delete_adsblol_day, import_adsblol_traces
from .services.database_import import (
    import_normalized_events,
    replace_normalized_events,
    restore_aircraft_types_from_events,
    seed_airline_codes,
    seed_airports,
)
from .services.excel_importer import iter_spotting_events
from .services.excel_importer import repair_future_dates
from .services.match_runner import run_matching
from .services.opensky_ingest import import_opensky_flights, import_opensky_track


def register_commands(app):
    @app.cli.command("init-db")
    def init_db_command():
        """Create the database tables and seed core reference data."""
        db.create_all()
        seed_airports()
        seed_airline_codes()
        click.echo("Database initialized.")

    @app.cli.command("import-excel")
    @click.argument("workbook_path", type=click.Path(exists=True, dir_okay=False))
    @click.option(
        "--sheet-name",
        default="航空",
        show_default=True,
        help="Worksheet containing the aircraft archive.",
    )
    @click.option(
        "--replace-existing",
        is_flag=True,
        help="Safely replace all personal spotting records after validating the workbook.",
    )
    @click.option(
        "--preview",
        is_flag=True,
        help="Parse and report the workbook without changing the database.",
    )
    @click.option(
        "--repair-future-date-to",
        type=click.DateTime(formats=["%Y-%m-%d"]),
        help=(
            "Opt-in repair for future Excel autofill years sharing this "
            "month/day; the source workbook is not modified."
        ),
    )
    @click.option(
        "--limit",
        type=click.IntRange(min=1),
        help="Import only the first N normalized events in workbook order.",
    )
    def import_excel_command(
        workbook_path,
        sheet_name,
        replace_existing,
        preview,
        repair_future_date_to,
        limit,
    ):
        """Normalize and import the 航空 worksheet."""
        events = list(iter_spotting_events(workbook_path, sheet_name=sheet_name))
        source_events = len(events)
        repaired_future_dates = 0
        if repair_future_date_to is not None:
            events, repaired_future_dates = repair_future_dates(
                events, repair_future_date_to.date()
            )
        if limit is not None:
            events = events[:limit]
        if preview:
            report = {
                "mode": "preview",
                "source_events": source_events,
                "events": len(events),
                "limit": limit,
                "excluded_by_limit": source_events - len(events),
                "repaired_future_dates": repaired_future_dates,
                "valid_dates": sum(
                    event.spotting_date is not None for event in events
                ),
                "ready": sum(event.quality_status == "ready" for event in events),
                "review": sum(event.quality_status == "review" for event in events),
                "registrations": len({event.registration for event in events}),
                "airlines": dict(
                    sorted(
                        Counter(
                            event.airline_raw
                            for event in events
                            if event.airline_raw
                        ).items()
                    )
                ),
                "locations": dict(
                    sorted(
                        Counter(
                            event.spotting_location_raw
                            for event in events
                            if event.spotting_location_raw
                        ).items()
                    )
                ),
                "dates": dict(
                    sorted(
                        Counter(
                            event.spotting_date.isoformat()
                            for event in events
                            if event.spotting_date
                        ).items()
                    )
                ),
            }
            click.echo(json.dumps(report, ensure_ascii=False, indent=2))
            return
        try:
            report = (
                replace_normalized_events(events)
                if replace_existing
                else import_normalized_events(events)
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        seed_airline_codes()
        report["source_events"] = source_events
        report["limit"] = limit
        report["excluded_by_limit"] = source_events - len(events)
        report["repaired_future_dates"] = repaired_future_dates
        click.echo(json.dumps(report, ensure_ascii=False, indent=2))

    @app.cli.command("restore-aircraft-types")
    @click.argument("workbook_path", type=click.Path(exists=True, dir_okay=False))
    @click.option("--sheet-name", default="航空", show_default=True)
    @click.option(
        "--limit",
        type=click.IntRange(min=1),
        default=100,
        show_default=True,
        help="Use the controlled first N normalized workbook events.",
    )
    @click.option("--preview", is_flag=True, help="Report changes without saving them.")
    def restore_aircraft_types_command(workbook_path, sheet_name, limit, preview):
        """Restore displayed aircraft models from the personal workbook."""
        events = list(
            iter_spotting_events(workbook_path, sheet_name=sheet_name)
        )[:limit]
        try:
            report = restore_aircraft_types_from_events(events, commit=False)
        except ValueError as exc:
            db.session.rollback()
            raise click.ClickException(str(exc)) from exc
        if preview:
            db.session.rollback()
            report["mode"] = "preview"
        else:
            db.session.commit()
            report["mode"] = "restore"
        click.echo(json.dumps(report, ensure_ascii=False, indent=2))

    @app.cli.group("opensky")
    def opensky_group():
        """Query the approved OpenSky Historical Database account."""

    @app.cli.group("adsblol")
    def adsblol_group():
        """Import targeted traces from an ADSB.lol daily split archive."""

    @adsblol_group.command("import-archive")
    @click.argument(
        "archive_parts",
        nargs=-1,
        required=True,
        type=click.Path(exists=True, dir_okay=False),
    )
    @click.option("--date", "spotting_date", required=True, help="Date: YYYY-MM-DD")
    @click.option(
        "--location",
        default="伦敦希思罗",
        show_default=True,
        help="Original spotting-location value used to select target aircraft.",
    )
    @click.option("--sample-seconds", default=60, show_default=True, type=int)
    @click.option(
        "--replace-source-day",
        is_flag=True,
        help="Delete only existing ADSB.lol flights overlapping this UTC day before import.",
    )
    def adsblol_import_archive_command(
        archive_parts, spotting_date, location, sample_seconds, replace_source_day
    ):
        """Extract and import only aircraft present in a spotting sample."""
        try:
            parsed_date = date.fromisoformat(spotting_date)
        except ValueError as exc:
            raise click.ClickException("Date must use YYYY-MM-DD.") from exc
        target_icao24s = set(
            db.session.scalars(
                select(Aircraft.icao24)
                .join(SpottingEvent, SpottingEvent.aircraft_id == Aircraft.aircraft_id)
                .where(
                    SpottingEvent.spotting_date == parsed_date,
                    SpottingEvent.spotting_location_raw == location,
                    Aircraft.icao24.is_not(None),
                )
            ).all()
        )
        if not target_icao24s:
            raise click.ClickException(
                "No target aircraft with ICAO24 metadata match this date and location."
            )
        seed_airports()
        replacement_report = (
            delete_adsblol_day(parsed_date) if replace_source_day else {}
        )
        raw_output_dir = (
            current_app.instance_path
            + f"/adsblol_raw/{parsed_date.isoformat()}"
        )
        payloads, extraction_report = extract_trace_payloads(
            archive_parts,
            target_icao24s,
            raw_output_dir=raw_output_dir,
        )
        import_report = import_adsblol_traces(
            payloads, sample_seconds=sample_seconds
        )
        click.echo(
            json.dumps(
                {
                    "source": "ADSB.lol daily archive",
                    "date": parsed_date.isoformat(),
                    "location": location,
                    "raw_output_dir": raw_output_dir,
                    **replacement_report,
                    **extraction_report,
                    **import_report,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    @adsblol_group.command("discover-metadata")
    @click.argument(
        "archive_parts",
        nargs=-1,
        required=True,
        type=click.Path(exists=True, dir_okay=False),
    )
    @click.option("--date", "spotting_date", required=True, help="Date: YYYY-MM-DD")
    @click.option("--location", required=True, help="Spotting-location value.")
    def adsblol_discover_metadata_command(archive_parts, spotting_date, location):
        """Discover missing ICAO24 identifiers from trace headers."""
        try:
            parsed_date = date.fromisoformat(spotting_date)
        except ValueError as exc:
            raise click.ClickException("Date must use YYYY-MM-DD.") from exc
        aircraft_rows = db.session.scalars(
            select(Aircraft)
            .join(SpottingEvent, SpottingEvent.aircraft_id == Aircraft.aircraft_id)
            .where(
                SpottingEvent.spotting_date == parsed_date,
                SpottingEvent.spotting_location_raw == location,
                Aircraft.icao24.is_(None),
            )
            .distinct()
        ).all()
        aircraft_by_registration = {
            aircraft.registration.upper(): aircraft for aircraft in aircraft_rows
        }
        if not aircraft_by_registration:
            raise click.ClickException("No target aircraft are missing ICAO24 metadata.")
        metadata, discovery_report = discover_trace_metadata(
            archive_parts, aircraft_by_registration
        )
        updated = 0
        type_metadata_results = []
        for registration, item in metadata.items():
            icao24 = item["icao24"]
            aircraft = aircraft_by_registration[registration]
            if ICAO24_PATTERN.fullmatch(icao24):
                aircraft.icao24 = icao24
                updated += 1
            type_result = apply_typecode_metadata(aircraft, item.get("typecode"))
            if type_result:
                type_metadata_results.append(type_result)
        db.session.commit()
        click.echo(
            json.dumps(
                {
                    **discovery_report,
                    "aircraft_updated": updated,
                    "aircraft_types_updated": sum(
                        item["action"] == "filled_missing_type"
                        for item in type_metadata_results
                    ),
                    "workbook_types_preserved": sum(
                        item["action"] == "preserved_workbook_type"
                        for item in type_metadata_results
                    ),
                    "type_metadata_results": type_metadata_results,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    def _opensky_client(*, console_authentication=False):
        try:
            return OpenSkyTrinoClient.from_env(
                console_authentication=console_authentication
            )
        except OpenSkyConfigurationError as exc:
            raise click.ClickException(str(exc)) from exc

    @opensky_group.command("test")
    @click.option(
        "--manual-login",
        is_flag=True,
        help="Print the OAuth URL instead of opening the default browser.",
    )
    def opensky_test_command(manual_login):
        """Open browser authentication and test the Trino connection."""
        rows = _opensky_client(
            console_authentication=manual_login
        ).test_connection()
        click.echo(json.dumps(rows, ensure_ascii=False, indent=2))

    @opensky_group.command("describe")
    @click.argument(
        "table",
        type=click.Choice(["flights_data4", "state_vectors_data4"]),
    )
    def opensky_describe_command(table):
        """Show the current OpenSky schema for an allowed table."""
        rows = _opensky_client().describe_table(table)
        click.echo(json.dumps(rows, ensure_ascii=False, indent=2))

    @opensky_group.command("download-metadata")
    @click.option(
        "--destination",
        type=click.Path(dir_okay=False),
        default=None,
        help="Optional output CSV path.",
    )
    def opensky_download_metadata_command(destination):
        """Download OpenSky's aircraft registration/ICAO24 reference CSV."""
        if destination is None:
            destination = current_app.instance_path + "/opensky_aircraft_database.csv"
        path = download_opensky_aircraft_metadata(destination)
        click.echo(str(path))

    @opensky_group.command("import-metadata")
    @click.argument("csv_path", type=click.Path(exists=True, dir_okay=False))
    def opensky_import_metadata_command(csv_path):
        """Map personal registrations to ICAO24 identifiers."""
        report = import_opensky_aircraft_metadata(csv_path)
        click.echo(json.dumps(report, ensure_ascii=False, indent=2))

    @opensky_group.command("fetch-flights")
    @click.option("--date", "flight_date", required=True, help="UTC date: YYYY-MM-DD")
    @click.option("--airport", default="EGLL", show_default=True, help="ICAO airport code")
    def opensky_fetch_flights_command(flight_date, airport):
        """Fetch one partition of airport flight summaries and import it."""
        try:
            parsed_date = date.fromisoformat(flight_date)
        except ValueError as exc:
            raise click.ClickException("Date must use YYYY-MM-DD.") from exc
        query, rows = _opensky_client().fetch_airport_flights(parsed_date, airport)
        artifact = write_query_artifact(
            current_app.instance_path + "/opensky_raw",
            label=f"flights_{parsed_date.isoformat()}_{airport.upper()}",
            queries=[query],
            rows=rows,
        )
        report = import_opensky_flights(rows)
        report["raw_artifact"] = str(artifact)
        report["retrieved_rows"] = len(rows)
        click.echo(json.dumps(report, ensure_ascii=False, indent=2))

    @app.cli.group("match")
    def match_group():
        """Run explainable spotting-to-flight record linkage."""

    @match_group.command("run")
    @click.option("--date", "spotting_date", required=True, help="Date: YYYY-MM-DD")
    @click.option(
        "--location",
        default="伦敦希思罗",
        show_default=True,
        help="Original spotting-location value.",
    )
    def match_run_command(spotting_date, location):
        try:
            parsed_date = date.fromisoformat(spotting_date)
        except ValueError as exc:
            raise click.ClickException("Date must use YYYY-MM-DD.") from exc
        report = run_matching(
            spotting_date=parsed_date,
            spotting_location_raw=location,
        )
        click.echo(json.dumps(report, ensure_ascii=False, indent=2))

    @opensky_group.command("fetch-track")
    @click.argument("adsb_flight_id", type=int)
    @click.option("--sample-seconds", default=60, show_default=True, type=int)
    def opensky_fetch_track_command(adsb_flight_id, sample_seconds):
        """Fetch and import a selected OpenSky flight track."""
        flight = db.session.scalar(
            select(AdsbFlight).where(AdsbFlight.adsb_flight_id == adsb_flight_id)
        )
        if flight is None:
            raise click.ClickException(f"ADS-B flight {adsb_flight_id} was not found.")
        queries, rows = _opensky_client().fetch_track(
            icao24=flight.icao24,
            start=flight.first_seen - timedelta(minutes=15),
            end=flight.last_seen + timedelta(minutes=15),
            sample_seconds=sample_seconds,
        )
        artifact = write_query_artifact(
            current_app.instance_path + "/opensky_raw",
            label=f"track_{flight.adsb_flight_id}_{flight.icao24}",
            queries=queries,
            rows=rows,
        )
        report = import_opensky_track(flight, rows)
        report["raw_artifact"] = str(artifact)
        report["retrieved_rows"] = len(rows)
        click.echo(json.dumps(report, ensure_ascii=False, indent=2))
