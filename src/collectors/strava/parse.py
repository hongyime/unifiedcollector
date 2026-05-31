"""Pure normalization helpers for the strava collector.

Extracted from the collector class (STAGE 2 of the per-package refactor). These
map raw Strava payloads (training_activities and dashboard/feed) into the shape
``_upsert_activity`` expects. Pure functions — no ``self``, no I/O — so they
unit-test trivially against captured payloads. The collector keeps thin
staticmethod shims delegating here so all internal call sites are unchanged.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone


def normalize_training_activity(raw: dict) -> dict:
    """Map Strava /athlete/training_activities payload fields to the
    shape _upsert_activity expects (compatible with /api/v3/athlete/activities)."""
    # raw fields seen: id, name, type, distance ("9.99mi" or meters), moving_time
    # ("1h 2m" or seconds), elapsed_time, start_date_local, total_elevation_gain,
    # start_date_local_raw (epoch). Numeric fields are sometimes strings with units.
    def _num(v):
        if v is None: return None
        if isinstance(v, (int, float)): return v
        if isinstance(v, str):
            m = re.match(r"^\s*([\d.]+)", v)
            if m:
                try: return float(m.group(1))
                except Exception: return None
        return None

    def _seconds(v):
        if v is None: return None
        if isinstance(v, (int, float)): return int(v)
        if isinstance(v, str):
            # "1h 2m 3s" or "62:30" or "3600"
            if v.isdigit(): return int(v)
            total = 0
            for n, unit in re.findall(r"(\d+)\s*([hms])", v):
                n = int(n)
                total += n * (3600 if unit == "h" else 60 if unit == "m" else 1)
            if total: return total
            # mm:ss or hh:mm:ss
            parts = v.split(":")
            if all(p.strip().isdigit() for p in parts):
                parts = [int(p) for p in parts]
                if len(parts) == 2: return parts[0] * 60 + parts[1]
                if len(parts) == 3: return parts[0] * 3600 + parts[1] * 60 + parts[2]
        return None

    start = raw.get("start_date_local") or raw.get("start_date")
    # Convert epoch raw if that's all we have, or parse human format like "Wed, 4/15/2026".
    epoch_raw = raw.get("start_date_local_raw") or raw.get("start_date_raw")
    if epoch_raw:
        try:
            start = datetime.fromtimestamp(int(epoch_raw), tz=timezone.utc).isoformat()
        except Exception:
            pass
    if isinstance(start, str) and start and not re.match(r"^\d{4}-\d{2}-\d{2}", start):
        # Strava web returns strings like "Wed, 4/15/2026" — convert to ISO date.
        try:
            # strip leading weekday + comma
            s = re.sub(r"^[A-Za-z]+,\s*", "", start).strip()
            dt = datetime.strptime(s, "%m/%d/%Y")
            start = dt.replace(tzinfo=timezone.utc).isoformat()
        except Exception:
            start = None
    # Ensure ISO with no trailing Z handler issues.
    if isinstance(start, str) and start.endswith("Z"):
        start = start  # _upsert_activity strips Z itself

    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "type": raw.get("type") or raw.get("activity_type"),
        "sport_type": raw.get("sport_type") or raw.get("type"),
        "distance": _num(raw.get("distance")) or _num(raw.get("distance_raw")),
        "moving_time": _seconds(raw.get("moving_time")) or _seconds(raw.get("moving_time_raw")),
        "elapsed_time": _seconds(raw.get("elapsed_time")) or _seconds(raw.get("elapsed_time_raw")),
        "total_elevation_gain": _num(raw.get("elevation_gain")) or _num(raw.get("elevation_gain_raw"))
            or _num(raw.get("total_elevation_gain")),
        "average_speed": _num(raw.get("average_speed")),
        "max_speed": _num(raw.get("max_speed")),
        "average_heartrate": _num(raw.get("average_heartrate")),
        "calories": _num(raw.get("calories")),
        "start_date": start,
    }


def normalize_feed_activity(raw_item: dict) -> dict | None:
    """Map a /dashboard/feed entry to our strava_activities upsert shape.

    The feed payload nests the activity under either `activity`, `row`, or
    the raw item itself. We only need the fields _upsert_activity stores
    (id, name, type, start_date, distance, elapsed_time, etc.).
    """
    if not isinstance(raw_item, dict):
        return None
    activity_payload = raw_item.get("activity")
    entity = activity_payload if isinstance(activity_payload, dict) else (
        raw_item.get("row") if isinstance(raw_item.get("row"), dict) else raw_item
    )
    if not isinstance(entity, dict):
        return None
    athlete = entity.get("athlete") if isinstance(entity.get("athlete"), dict) else (
        raw_item.get("athlete") if isinstance(raw_item.get("athlete"), dict) else {}
    )
    activity_id = entity.get("id") or raw_item.get("entity_id") or raw_item.get("activity_id")
    if not activity_id:
        return None
    try:
        activity_id = int(activity_id)
    except (TypeError, ValueError):
        return None
    athlete_id = athlete.get("id") or athlete.get("athleteId") or entity.get("athlete_id") or raw_item.get("athlete_id")
    start_date = entity.get("start_date") or entity.get("start_date_utc") or entity.get("startDate")
    if not start_date:
        return None
    # Distance/time fields can be missing or strings on the feed payload.
    def _num(v):
        if v is None: return None
        if isinstance(v, (int, float)): return v
        if isinstance(v, str):
            m = re.match(r"^\s*([\d.]+)", v)
            if m:
                try: return float(m.group(1))
                except Exception: return None
        return None
    return {
        "id": activity_id,
        "name": entity.get("name") or entity.get("activity_name") or entity.get("activityName"),
        "type": entity.get("type") or entity.get("sport_type") or "Unknown",
        "sport_type": entity.get("sport_type") or entity.get("type"),
        "distance": _num(entity.get("distance")),
        "moving_time": entity.get("moving_time") if isinstance(entity.get("moving_time"), int) else None,
        "elapsed_time": entity.get("elapsed_time") if isinstance(entity.get("elapsed_time"), int) else None,
        "total_elevation_gain": _num(entity.get("total_elevation_gain") or entity.get("elevation_gain")),
        "average_speed": _num(entity.get("average_speed")),
        "max_speed": _num(entity.get("max_speed")),
        "average_heartrate": _num(entity.get("average_heartrate")),
        "calories": _num(entity.get("calories")),
        "start_date": start_date,
        "_athlete_id": athlete_id,
        "_source": "following_feed",
        "_athlete_name": athlete.get("name") or athlete.get("username") or
                         f"{athlete.get('firstname','')} {athlete.get('lastname','')}".strip() or None,
    }
