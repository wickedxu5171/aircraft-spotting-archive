from datetime import datetime, timedelta
from types import SimpleNamespace

from app.services.track_visualization import build_track_visualization


def point(index, latitude, longitude, altitude, speed):
    return SimpleNamespace(
        observed_at=datetime(2025, 11, 5) + timedelta(minutes=index),
        latitude=latitude,
        longitude=longitude,
        baro_altitude=altitude,
        ground_speed=speed,
        on_ground=index == 0,
    )


def test_builds_self_contained_track_and_profile_paths():
    visual = build_track_visualization(
        [
            point(0, 51.47, -0.45, None, 0),
            point(1, 52.0, -2.0, 3048, 205.7776),
            point(2, 53.0, -4.0, 6096, 257.222),
        ]
    )

    assert len(visual["trajectory_path"].split()) == 3
    assert len(visual["altitude_path"].split()) == 3
    assert round(visual["max_altitude_ft"]) == 20000
    assert round(visual["max_speed_knots"]) == 500


def test_empty_track_has_no_paths():
    visual = build_track_visualization([])

    assert visual["trajectory_path"] == ""
    assert visual["max_altitude_ft"] is None
