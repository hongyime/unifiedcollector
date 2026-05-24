"""On-demand analysis: route clusters, overlaps, co-occurrence, athlete stats."""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from collections import defaultdict
from typing import Optional

try:
    import h3 as _h3
    _H3_AVAILABLE = True
except ImportError:
    _H3_AVAILABLE = False

from ingestion.config import now_utc_iso
from ingestion.logging_config import get_logger

logger = get_logger(__name__)

CHUNK_SIZE = 500


def _haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in metres between two lat/lon points."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── Route Clusters ─────────────────────────────────────────────────────────


def compute_route_clusters(
    conn: sqlite3.Connection,
    sport_type: str = "Run",
    shutdown_event=None,
) -> None:
    """Cluster activities by start/end proximity using DBSCAN."""
    try:
        from sklearn.cluster import DBSCAN
        import numpy as np
    except ImportError:
        logger.error("scikit-learn not installed. Run: pip install scikit-learn")
        return

    now = now_utc_iso()
    rows = conn.execute(
        """
        SELECT activity_id, start_latlng_lat, start_latlng_lon,
               end_latlng_lat, end_latlng_lon
        FROM activities
        WHERE sport_type = ?
          AND start_latlng_lat IS NOT NULL
          AND end_latlng_lat IS NOT NULL
        """,
        (sport_type,),
    ).fetchall()

    if not rows:
        logger.info(f"No activities with GPS for sport_type={sport_type}")
        return

    activity_ids = [r["activity_id"] for r in rows]
    coords = np.array(
        [[r["start_latlng_lat"], r["start_latlng_lon"],
          r["end_latlng_lat"], r["end_latlng_lon"]] for r in rows],
        dtype=float,
    )

    # DBSCAN with ~200m tolerance (in degrees ≈ 0.002)
    eps_deg = 0.002
    db = DBSCAN(eps=eps_deg, min_samples=2, algorithm="ball_tree", metric="euclidean").fit(coords)
    labels = db.labels_

    # Delete old clusters for this sport_type
    conn.execute("DELETE FROM route_cluster_members WHERE cluster_id IN "
                 "(SELECT cluster_id FROM route_clusters WHERE sport_type=?)", (sport_type,))
    conn.execute("DELETE FROM route_clusters WHERE sport_type=?", (sport_type,))

    cluster_ids_seen: dict[int, str] = {}
    for i, label in enumerate(labels):
        if shutdown_event and shutdown_event.is_set():
            logger.info("Cluster computation interrupted.")
            return
        if label == -1:
            continue  # noise
        cluster_id = f"{sport_type}_{label}"
        cluster_ids_seen.setdefault(label, cluster_id)
        conn.execute(
            "INSERT OR IGNORE INTO route_cluster_members (cluster_id, activity_id) VALUES (?, ?)",
            (cluster_id, activity_ids[i]),
        )
        sys.stdout.flush()

    # Compute centroids and upsert route_clusters
    for label, cluster_id in cluster_ids_seen.items():
        mask = labels == label
        cluster_coords = coords[mask]
        centroid = cluster_coords.mean(axis=0)
        act_count = int(mask.sum())
        ath_count = int(conn.execute(
            "SELECT COUNT(DISTINCT a.athlete_id) FROM activities a"
            " JOIN route_cluster_members m ON m.activity_id = a.activity_id"
            " WHERE m.cluster_id = ?",
            (cluster_id,),
        ).fetchone()[0])
        conn.execute(
            """
            INSERT OR REPLACE INTO route_clusters
              (cluster_id, sport_type, centroid_start_lat, centroid_start_lon,
               centroid_end_lat, centroid_end_lon, activity_count, athlete_count, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (cluster_id, sport_type, float(centroid[0]), float(centroid[1]),
             float(centroid[2]), float(centroid[3]), act_count, ath_count, now),
        )

    logger.info(f"Route clusters: {len(cluster_ids_seen)} clusters for {sport_type}")


# ── Route Overlaps ─────────────────────────────────────────────────────────


def compute_route_overlaps(
    conn: sqlite3.Connection,
    threshold_meters: float = 50.0,
    shutdown_event=None,
) -> None:
    """Find pairs of activities on the same date whose GPS tracks come within threshold_meters.

    Uses H3 spatial bucketing (when available) to skip pairs of activities whose routes
    are geographically separated, reducing candidate pairs from O(n²) to only those that
    share at least one H3 cell.
    """
    now = now_utc_iso()
    # H3 resolution 9 = ~174m cells. Two points within threshold_meters will share a cell
    # or an immediate neighbour. We include k-ring 1 neighbours to avoid missing edge cases.
    _H3_RESOLUTION = 9

    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT calendar_date FROM activities ORDER BY calendar_date DESC"
    ).fetchall()]

    for date_str in dates:
        if shutdown_event and shutdown_event.is_set():
            return

        activities = conn.execute(
            "SELECT activity_id FROM activities WHERE calendar_date=? AND stream_status='done'",
            (date_str,),
        ).fetchall()
        act_ids = [r[0] for r in activities]

        if len(act_ids) < 2:
            continue

        streams: dict[int, list[tuple[float, float]]] = {}
        for act_id in act_ids:
            pts = conn.execute(
                "SELECT latitude, longitude FROM streams WHERE activity_id=? ORDER BY point_index",
                (act_id,),
            ).fetchall()
            if pts:
                streams[act_id] = [(r[0], r[1]) for r in pts]

        act_ids_with_streams = list(streams.keys())
        if len(act_ids_with_streams) < 2:
            continue

        if _H3_AVAILABLE:
            # Build cell → set of activity_ids, then find pairs sharing a cell
            cell_to_acts: dict[str, set[int]] = defaultdict(set)
            for act_id, pts in streams.items():
                for lat, lon in pts:
                    cell = _h3.latlng_to_cell(lat, lon, _H3_RESOLUTION)
                    cell_to_acts[cell].add(act_id)
                    for neighbour in _h3.grid_disk(cell, 1):
                        cell_to_acts[neighbour].add(act_id)

            candidate_pairs: set[tuple[int, int]] = set()
            for acts in cell_to_acts.values():
                if len(acts) >= 2:
                    acts_sorted = sorted(acts)
                    for i in range(len(acts_sorted)):
                        for j in range(i + 1, len(acts_sorted)):
                            candidate_pairs.add((acts_sorted[i], acts_sorted[j]))
        else:
            # Fallback: all pairs
            candidate_pairs = set()
            for i in range(len(act_ids_with_streams)):
                for j in range(i + 1, len(act_ids_with_streams)):
                    candidate_pairs.add((act_ids_with_streams[i], act_ids_with_streams[j]))

        for id_a, id_b in candidate_pairs:
            if shutdown_event and shutdown_event.is_set():
                return
            overlap_count = _count_overlap_points(streams[id_a], streams[id_b], threshold_meters)
            if overlap_count > 0:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO route_overlaps
                      (activity_id_a, activity_id_b, overlap_point_count, computed_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (min(id_a, id_b), max(id_a, id_b), overlap_count, now),
                )
        sys.stdout.flush()

    logger.info("Route overlaps computed.")


def _count_overlap_points(
    pts_a: list[tuple[float, float]],
    pts_b: list[tuple[float, float]],
    threshold: float,
) -> int:
    """Count how many points in pts_a are within threshold metres of any point in pts_b."""
    count = 0
    for lat_a, lon_a in pts_a:
        for lat_b, lon_b in pts_b:
            if _haversine_meters(lat_a, lon_a, lat_b, lon_b) <= threshold:
                count += 1
                break
    return count


# ── Co-occurrence ──────────────────────────────────────────────────────────


def compute_co_occurrence(
    conn: sqlite3.Connection,
    threshold_meters: float = 100.0,
    threshold_seconds: int = 300,
    shutdown_event=None,
) -> None:
    """Find pairs of athletes whose GPS tracks come within threshold on the same date."""
    now = now_utc_iso()
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT calendar_date FROM activities ORDER BY calendar_date DESC"
    ).fetchall()]

    co_counts: dict[tuple[int, int], dict] = defaultdict(lambda: {"count": 0, "last_date": ""})

    for date_str in dates:
        if shutdown_event and shutdown_event.is_set():
            break

        athletes_on_date = conn.execute(
            """
            SELECT DISTINCT a.athlete_id
            FROM activities a
            WHERE a.calendar_date = ?
              AND a.stream_status = 'done'
            """,
            (date_str,),
        ).fetchall()
        athlete_ids = [r[0] for r in athletes_on_date]

        if len(athlete_ids) < 2:
            continue

        # Load first stream point per athlete (just start point for quick proximity check)
        starts: dict[int, tuple[float, float]] = {}
        for ath_id in athlete_ids:
            row = conn.execute(
                """
                SELECT s.latitude, s.longitude
                FROM streams s
                JOIN activities a ON a.activity_id = s.activity_id
                WHERE a.athlete_id = ? AND a.calendar_date = ?
                ORDER BY a.activity_id, s.point_index
                LIMIT 1
                """,
                (ath_id, date_str),
            ).fetchone()
            if row:
                starts[ath_id] = (row[0], row[1])

        ath_list = list(starts.keys())
        for i in range(len(ath_list)):
            for j in range(i + 1, len(ath_list)):
                id_a, id_b = sorted([ath_list[i], ath_list[j]])
                lat_a, lon_a = starts[ath_list[i]]
                lat_b, lon_b = starts[ath_list[j]]
                if _haversine_meters(lat_a, lon_a, lat_b, lon_b) <= threshold_meters:
                    key = (id_a, id_b)
                    co_counts[key]["count"] += 1
                    if date_str > co_counts[key]["last_date"]:
                        co_counts[key]["last_date"] = date_str
        sys.stdout.flush()

    for (id_a, id_b), data in co_counts.items():
        conn.execute(
            """
            INSERT OR REPLACE INTO co_occurrence
              (athlete_id_a, athlete_id_b, co_occurrence_count, last_seen_date, computed_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (id_a, id_b, data["count"], data["last_date"] or None, now),
        )

    logger.info(f"Co-occurrence: {len(co_counts)} pairs found.")


# ── Athlete Stats ──────────────────────────────────────────────────────────


def compute_athlete_stats(
    conn: sqlite3.Connection,
    athlete_id: Optional[int] = None,
    shutdown_event=None,
) -> None:
    """Compute per-athlete stats and store in athlete_stats table."""
    now = now_utc_iso()
    if athlete_id is not None:
        athlete_ids = [athlete_id]
    else:
        athlete_ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT athlete_id FROM activities"
        ).fetchall()]

    processed = 0
    offset = 0

    while True:
        if shutdown_event and shutdown_event.is_set():
            return

        batch = athlete_ids[offset:offset + CHUNK_SIZE]
        if not batch:
            break

        for ath_id in batch:
            if shutdown_event and shutdown_event.is_set():
                return

            rows = conn.execute(
                """
                SELECT start_date_local, start_latlng_lat, start_latlng_lon,
                       end_latlng_lat, end_latlng_lon, elapsed_time_secs
                FROM activities
                WHERE athlete_id = ? AND start_latlng_lat IS NOT NULL
                ORDER BY start_date_local
                """,
                (ath_id,),
            ).fetchall()

            if not rows:
                continue

            # Estimate distance from start→end haversine
            distances = []
            for r in rows:
                if r["end_latlng_lat"] is not None:
                    d = _haversine_meters(r["start_latlng_lat"], r["start_latlng_lon"],
                                         r["end_latlng_lat"], r["end_latlng_lon"])
                    distances.append(d)

            total_dist = sum(distances)
            avg_dist = total_dist / len(distances) if distances else 0.0

            # Most common start/end (use median of first and last 10%)
            n = len(rows)
            common_start_lat = sum(r["start_latlng_lat"] for r in rows[:max(1, n // 10)]) / max(1, n // 10)
            common_start_lon = sum(r["start_latlng_lon"] for r in rows[:max(1, n // 10)]) / max(1, n // 10)
            end_rows = [r for r in rows if r["end_latlng_lat"] is not None]
            common_end_lat = sum(r["end_latlng_lat"] for r in end_rows[:max(1, len(end_rows) // 10)]) / max(1, len(end_rows) // 10) if end_rows else None
            common_end_lon = sum(r["end_latlng_lon"] for r in end_rows[:max(1, len(end_rows) // 10)]) / max(1, len(end_rows) // 10) if end_rows else None

            # Monthly counts
            monthly: dict[str, int] = defaultdict(int)
            for r in rows:
                month = str(r["start_date_local"])[:7] if r["start_date_local"] else "unknown"
                monthly[month] += 1

            conn.execute(
                """
                INSERT OR REPLACE INTO athlete_stats
                  (athlete_id, total_distance_m, avg_distance_m, activity_count,
                   common_start_lat, common_start_lon, common_end_lat, common_end_lon,
                   monthly_counts_json, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ath_id, total_dist, avg_dist, len(rows),
                 common_start_lat, common_start_lon, common_end_lat, common_end_lon,
                 json.dumps(dict(monthly)), now),
            )
            processed += 1

        del batch
        offset += CHUNK_SIZE
        sys.stdout.flush()

    logger.info(f"Athlete stats computed for {processed} athletes.")
