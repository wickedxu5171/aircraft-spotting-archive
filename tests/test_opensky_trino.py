from datetime import date, datetime, timezone

import pytest

from app.integrations.opensky_trino import (
    OpenSkyTrinoClient,
    OpenSkyTrinoConfig,
    UnsafeOpenSkyQueryError,
    iter_utc_hour_partitions,
    utc_day_partition,
    validate_partitioned_query,
)


def client():
    return OpenSkyTrinoClient(OpenSkyTrinoConfig(username="student"), connection=object())


def test_day_partition_is_utc_midnight():
    assert utc_day_partition(date(2025, 11, 5)) == 1762300800


def test_flight_query_is_partitioned_and_airport_scoped():
    query = client().build_airport_flights_query(date(2025, 11, 5), "EGLL")
    assert "WHERE day = 1762300800" in query
    assert "estdepartureairport" in query
    assert "EGLL" in query
    validate_partitioned_query(query)


def test_unpartitioned_protected_query_is_rejected():
    with pytest.raises(UnsafeOpenSkyQueryError):
        validate_partitioned_query("SELECT * FROM state_vectors_data4 WHERE icao24 = '406b20'")


def test_schema_description_is_allowed_without_data_partition():
    validate_partitioned_query("DESCRIBE state_vectors_data4")


def test_track_query_is_hour_partitioned_and_aircraft_scoped():
    query = client().build_state_vectors_query(
        icao24="406b20",
        hour_partition=1762347600,
        start_epoch=1762347600,
        end_epoch=1762351199,
    )
    assert "hour = 1762347600" in query
    assert "lower(icao24) = '406b20'" in query
    validate_partitioned_query(query)


def test_hour_partitions_include_each_touched_hour():
    start = datetime(2025, 11, 5, 22, 50, tzinfo=timezone.utc)
    end = datetime(2025, 11, 6, 0, 5, tzinfo=timezone.utc)
    partitions = list(iter_utc_hour_partitions(start, end))
    assert len(partitions) == 3
