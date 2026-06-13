"""One-off backfill: populate strava_activities privacy-zone/truncation fields
for activities that already have a stored GPS stream but no stream_status.

Uses the ORIGINAL API summary start/end from strava_activities.metadata (the
start_latlng/end_latlng columns may have been COALESCE-overwritten with the
track's first point, so metadata is the authoritative source for the summary).

Run inside the collector container:
  docker compose exec -T collector_strava python /app/scripts/backfill_strava_privacy_zones.py
"""
import asyncio
import json
import math
import os

import asyncpg

THRESHOLD_M = 50.0


def _hav(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _is_trunc(summary, pt):
    if not pt or len(pt) != 2:
        return False
    if not summary or len(summary) != 2:
        return True
    return _hav(summary[0], summary[1], pt[0], pt[1]) > THRESHOLD_M


def _coerce(v):
    return json.loads(v) if isinstance(v, str) else v


async def main():
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    # Reprocess ALL activities that have a stored stream (re-runnable / idempotent).
    rows = await pool.fetch(
        """
        SELECT a.id, a.metadata, s.latlng
        FROM strava_gps_streams s
        JOIN strava_activities a ON a.id = s.activity_id
        """
    )
    updated = 0
    for r in rows:
        path = _coerce(r["latlng"])
        meta = _coerce(r["metadata"]) or {}
        if path is None:
            status, pzs, pze, tps, tpe = "incomplete", False, False, None, None
        elif len(path) == 0:
            status, pzs, pze, tps, tpe = "truncated_empty", False, False, None, None
        else:
            status = "ok"
            ss, se = meta.get("start_latlng"), meta.get("end_latlng")
            # The original API summary was not preserved in metadata for most
            # historical rows. Without it we CANNOT determine privacy truncation
            # retroactively, so leave the flag NULL (unknown) rather than guessing.
            # The forward path (_collect_gps_streams) sets it correctly going fwd.
            pzs = _is_trunc(ss, path[0]) if (ss and len(ss) == 2) else None
            pze = _is_trunc(se, path[-1]) if (se and len(se) == 2) else None
            tps = f"{path[0][0]},{path[0][1]}" if pzs else None
            tpe = f"{path[-1][0]},{path[-1][1]}" if pze else None
        await pool.execute(
            "UPDATE strava_activities SET stream_status=$1, privacy_zone_start=$2, "
            "privacy_zone_end=$3, truncation_point_start=$4, truncation_point_end=$5 "
            "WHERE id=$6",
            status, pzs, pze, tps, tpe, r["id"],
        )
        updated += 1
    print(f"backfilled {updated} activities")
    await pool.close()


asyncio.run(main())
