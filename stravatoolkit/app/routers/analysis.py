"""Analysis API router — clusters, overlaps, proximity, athlete stats."""
from __future__ import annotations

import threading
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query

from app.database import get_db

router = APIRouter()


@router.get("/routes/clusters")
def get_route_clusters(
    sport_type: Optional[str] = Query(None),
    conn=Depends(get_db),
):
    where = "WHERE sport_type = ?" if sport_type else ""
    params = [sport_type] if sport_type else []
    rows = conn.execute(
        f"""
        SELECT cluster_id, sport_type, centroid_start_lat, centroid_start_lon,
               centroid_end_lat, centroid_end_lon, activity_count, athlete_count, computed_at
        FROM route_clusters
        {where}
        ORDER BY activity_count DESC
        LIMIT 500
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


@router.get("/routes/overlaps")
def get_route_overlaps(conn=Depends(get_db)):
    rows = conn.execute(
        """
        SELECT activity_id_a, activity_id_b, overlap_point_count, computed_at
        FROM route_overlaps
        ORDER BY overlap_point_count DESC
        LIMIT 500
        """
    ).fetchall()
    return [dict(r) for r in rows]


@router.get("/analysis/proximity")
def get_co_occurrence(
    min_count: int = Query(1),
    conn=Depends(get_db),
):
    rows = conn.execute(
        """
        SELECT athlete_id_a, athlete_id_b, co_occurrence_count, last_seen_date, computed_at
        FROM co_occurrence
        WHERE co_occurrence_count >= ?
        ORDER BY co_occurrence_count DESC
        LIMIT 500
        """,
        (min_count,),
    ).fetchall()
    return [dict(r) for r in rows]


@router.post("/analysis/compute")
def trigger_analysis(
    background_tasks: BackgroundTasks,
    sport_type: str = Query("Run"),
):
    def _run():
        from ingestion.analysis import (
            compute_athlete_stats,
            compute_co_occurrence,
            compute_route_clusters,
            compute_route_overlaps,
        )
        from ingestion.config import load_settings
        from ingestion.db import connect
        settings = load_settings()
        write_conn = connect(settings.db_path)
        shutdown = threading.Event()
        try:
            compute_route_clusters(write_conn, sport_type=sport_type, shutdown_event=shutdown)
            compute_route_overlaps(write_conn, shutdown_event=shutdown)
            compute_co_occurrence(write_conn, shutdown_event=shutdown)
            compute_athlete_stats(write_conn, shutdown_event=shutdown)
        finally:
            write_conn.close()

    background_tasks.add_task(_run)
    return {"status": "computing", "sport_type": sport_type}


@router.get("/athletes/{athlete_id}/stats")
def get_athlete_stats(athlete_id: int, conn=Depends(get_db)):
    row = conn.execute(
        """
        SELECT athlete_id, total_distance_m, avg_distance_m, activity_count,
               common_start_lat, common_start_lon, common_end_lat, common_end_lon,
               monthly_counts_json, computed_at
        FROM athlete_stats
        WHERE athlete_id = ?
        """,
        (athlete_id,),
    ).fetchone()
    if row is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Stats not computed for this athlete yet")
    return dict(row)
