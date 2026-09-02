import gzip
import io
import json
import tarfile
from datetime import datetime

from sqlalchemy.dialects import mysql

from app.models import TrackPoint
from app.integrations.adsblol_archive import (
    SplitArchiveReader,
    discover_trace_metadata,
    extract_trace_payloads,
)
from app.services.adsblol_ingest import parse_trace, sample_leg, split_legs


def test_mysql_track_timestamps_preserve_microseconds():
    dialect_type = TrackPoint.__table__.c.observed_at.type.dialect_impl(mysql.dialect())

    assert dialect_type.fsp == 6


def trace_payload():
    return {
        "icao": "406b20",
        "r": "G-XLEG",
        "t": "A388",
        "desc": "AIRBUS A380-800",
        "timestamp": 1762300800.0,
        "trace": [
            [0, 51.47, -0.45, "ground", 5, 270, 0, 0, {"flight": "BAW285"}, "adsb_icao", 100],
            [70, 51.50, -0.50, 1000, 160, 280, 0, 500, None, "adsb_icao", 1100],
            [140, 51.60, -0.60, 3000, 220, 285, 2, 800, {"flight": "BAW999"}, "adsb_icao", 3100],
            [210, 51.70, -0.70, 5000, 250, 290, 0, 900, None, "adsb_icao", 5100],
        ],
    }


def test_parse_trace_converts_units_and_splits_legs():
    points = parse_trace(trace_payload())
    legs = split_legs(points)

    assert len(legs) == 2
    assert points[0].on_ground is True
    assert points[1].baro_altitude == 304.8
    assert points[1].callsign == "BAW285"
    assert sample_leg(points, 60) == points


def test_same_callsign_is_one_leg_across_coverage_gap():
    payload = trace_payload()
    payload["trace"][2][6] = 0
    payload["trace"][2][8]["flight"] = "BAW285"
    payload["trace"][2][0] = 4000
    payload["trace"][3][0] = 4070

    assert len(split_legs(parse_trace(payload))) == 1


def test_callsign_change_starts_new_leg_without_flag():
    payload = trace_payload()
    payload["trace"][2][6] = 0

    assert len(split_legs(parse_trace(payload))) == 2


def test_split_archive_reader_crosses_part_boundary(tmp_path):
    first = tmp_path / "archive.aa"
    second = tmp_path / "archive.ab"
    first.write_bytes(b"abc")
    second.write_bytes(b"def")

    with SplitArchiveReader([first, second]) as reader:
        assert reader.read(4) == b"abcd"
        assert reader.read() == b"ef"


def test_extracts_only_requested_gzip_trace_from_split_tar(tmp_path):
    archive_buffer = io.BytesIO()
    payload = json.dumps(trace_payload()).encode("utf-8")
    compressed = gzip.compress(payload)
    with tarfile.open(fileobj=archive_buffer, mode="w") as archive:
        info = tarfile.TarInfo("./traces/20/trace_full_406b20.json")
        info.size = len(compressed)
        archive.addfile(info, io.BytesIO(compressed))
    archive_bytes = archive_buffer.getvalue()
    split_at = len(archive_bytes) // 2
    first = tmp_path / "archive.aa"
    second = tmp_path / "archive.ab"
    first.write_bytes(archive_bytes[:split_at])
    second.write_bytes(archive_bytes[split_at:])

    payloads, report = extract_trace_payloads([first, second], {"406b20"})

    assert payloads["406b20"]["r"] == "G-XLEG"
    assert report["found_aircraft"] == 1
    assert report["missing_icao24"] == []


def test_discovers_icao24_from_trace_header(tmp_path):
    archive_buffer = io.BytesIO()
    compressed = gzip.compress(json.dumps(trace_payload()).encode("utf-8"))
    with tarfile.open(fileobj=archive_buffer, mode="w") as archive:
        info = tarfile.TarInfo("./traces/20/trace_full_406b20.json")
        info.size = len(compressed)
        archive.addfile(info, io.BytesIO(compressed))
    archive_file = tmp_path / "archive.tar"
    archive_file.write_bytes(archive_buffer.getvalue())

    metadata, report = discover_trace_metadata([archive_file], {"G-XLEG"})

    assert metadata["G-XLEG"]["icao24"] == "406b20"
    assert metadata["G-XLEG"]["typecode"] == "A388"
    assert report["missing_registrations"] == []
