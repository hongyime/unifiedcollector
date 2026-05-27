"""Heatmap API router — H3 hexbin density map."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from app.database import get_db

router = APIRouter()

try:
    import h3
    _H3_AVAILABLE = True
except ImportError:
    _H3_AVAILABLE = False


@router.get("/heatmap")
def get_heatmap(
    sport_type: Optional[str] = Query(None),
    athlete_id: Optional[int] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    conn=Depends(get_db),
):
    if not _H3_AVAILABLE:
        return JSONResponse({"error": "h3 library not installed"}, status_code=503)

    where_clauses = ["s.latitude IS NOT NULL"]
    params: list = []

    if sport_type:
        where_clauses.append("a.sport_type = ?")
        params.append(sport_type)
    if athlete_id:
        where_clauses.append("a.athlete_id = ?")
        params.append(athlete_id)
    if date_from:
        where_clauses.append("a.calendar_date >= ?")
        params.append(date_from)
    if date_to:
        where_clauses.append("a.calendar_date <= ?")
        params.append(date_to)

    where_sql = " AND ".join(where_clauses)

    rows = conn.execute(
        f"""
        SELECT s.latitude, s.longitude
        FROM streams s
        JOIN activities a ON a.activity_id = s.activity_id
        WHERE {where_sql}
        LIMIT 500000
        """,
        params,
    ).fetchall()

    hex_counts: dict[str, int] = {}
    for row in rows:
        hex_id = h3.latlng_to_cell(row[0], row[1], 9)
        hex_counts[hex_id] = hex_counts.get(hex_id, 0) + 1

    result = []
    for hex_id, count in hex_counts.items():
        lat, lon = h3.cell_to_latlng(hex_id)
        result.append({"hex_id": hex_id, "lat": lat, "lon": lon, "count": count})

    return result
