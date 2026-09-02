from __future__ import annotations


def _scaled_path(values, *, width, height, padding=24):
    usable = [(index, value) for index, value in enumerate(values) if value is not None]
    if len(usable) < 2:
        return ""
    minimum = min(value for _, value in usable)
    maximum = max(value for _, value in usable)
    span = maximum - minimum or 1
    index_span = max(len(values) - 1, 1)
    coordinates = []
    for index, value in usable:
        x = padding + (index / index_span) * (width - 2 * padding)
        y = height - padding - ((value - minimum) / span) * (height - 2 * padding)
        coordinates.append(f"{x:.1f},{y:.1f}")
    return " ".join(coordinates)


def build_track_visualization(points, *, width=960):
    records = [
        {
            "observed_at": point.observed_at,
            "latitude": float(point.latitude),
            "longitude": float(point.longitude),
            "altitude_ft": (
                float(point.baro_altitude) / 0.3048
                if point.baro_altitude is not None
                else 0 if point.on_ground else None
            ),
            "speed_knots": (
                float(point.ground_speed) / 0.514444
                if point.ground_speed is not None
                else None
            ),
        }
        for point in points
    ]
    if not records:
        return {
            "records": [],
            "trajectory_path": "",
            "altitude_path": "",
            "speed_path": "",
            "max_altitude_ft": None,
            "max_speed_knots": None,
        }

    latitudes = [record["latitude"] for record in records]
    longitudes = [record["longitude"] for record in records]
    min_latitude, max_latitude = min(latitudes), max(latitudes)
    min_longitude, max_longitude = min(longitudes), max(longitudes)
    latitude_span = max_latitude - min_latitude or 1
    longitude_span = max_longitude - min_longitude or 1
    map_height = 420
    padding = 28
    trajectory = []
    for record in records:
        x = padding + (
            (record["longitude"] - min_longitude) / longitude_span
        ) * (width - 2 * padding)
        y = map_height - padding - (
            (record["latitude"] - min_latitude) / latitude_span
        ) * (map_height - 2 * padding)
        trajectory.append(f"{x:.1f},{y:.1f}")

    altitudes = [record["altitude_ft"] for record in records]
    speeds = [record["speed_knots"] for record in records]
    return {
        "records": records,
        "trajectory_path": " ".join(trajectory),
        "altitude_path": _scaled_path(altitudes, width=width, height=220),
        "speed_path": _scaled_path(speeds, width=width, height=220),
        "max_altitude_ft": max(
            (value for value in altitudes if value is not None), default=None
        ),
        "max_speed_knots": max(
            (value for value in speeds if value is not None), default=None
        ),
        "min_latitude": min_latitude,
        "max_latitude": max_latitude,
        "min_longitude": min_longitude,
        "max_longitude": max_longitude,
        "width": width,
        "map_height": map_height,
    }
