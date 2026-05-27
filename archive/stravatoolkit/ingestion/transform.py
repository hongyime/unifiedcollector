from __future__ import annotations

from datetime import datetime, timezone
from math import atan2, cos, radians, sin, sqrt


def _parse_iso_to_unix(value: str) -> int:
    normalized = value.replace("Z", "+00:00")
    return int(datetime.fromisoformat(normalized).astimezone(timezone.utc).timestamp())


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371000.0
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    a = (
        sin(d_lat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    )
    return 2 * radius * atan2(sqrt(a), sqrt(1 - a))


def transform_streams(
    activity: dict,
    latlng_stream: list[list[float]],
    time_stream: list[int],
    decimation_factor: int = 1,
) -> dict:
    if not latlng_stream or not time_stream:
        return {
            "stream_status": "incomplete",
            "privacy_zone_start": False,
            "privacy_zone_end": False,
            "truncation_point_start": None,
            "truncation_point_end": None,
            "path": [],
        }

    start_unix = _parse_iso_to_unix(activity["start_date_utc"])
    step = max(1, decimation_factor)
    path = []
    for index in range(0, min(len(latlng_stream), len(time_stream)), step):
        lat, lon = latlng_stream[index]
        path.append([lon, lat, start_unix + int(time_stream[index])])

    if not path:
        return {
            "stream_status": "truncated_empty",
            "privacy_zone_start": bool(activity.get("start_latlng")),
            "privacy_zone_end": False,
            "truncation_point_start": _to_lonlat(activity.get("start_latlng")),
            "truncation_point_end": None,
            "path": [],
        }

    start_latlng = activity.get("start_latlng")
    end_latlng = activity.get("end_latlng")
    privacy_zone_start = _is_truncated(start_latlng, path[0])
    privacy_zone_end = _is_truncated(end_latlng, path[-1])

    return {
        "stream_status": "ok",
        "privacy_zone_start": privacy_zone_start,
        "privacy_zone_end": privacy_zone_end,
        "truncation_point_start": path[0][:2] if privacy_zone_start else None,
        "truncation_point_end": path[-1][:2] if privacy_zone_end else None,
        "path": path,
    }


def _to_lonlat(value: list[float] | None) -> list[float] | None:
    if not value or len(value) != 2:
        return None
    return [value[1], value[0]]


def _is_truncated(expected_latlng: list[float] | None, actual_lonlat: list[float]) -> bool:
    if not expected_latlng or len(expected_latlng) != 2:
        return False
    expected_lat, expected_lon = expected_latlng
    actual_lon, actual_lat = actual_lonlat[:2]
    return haversine_meters(expected_lat, expected_lon, actual_lat, actual_lon) > 50
