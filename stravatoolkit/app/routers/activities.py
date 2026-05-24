from __future__ import annotations

import re
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

from app.database import get_db
from app.models import DayPlaybackResponse
from ingestion.db import build_day_playback, list_available_dates


router = APIRouter(tags=["activities"])


@router.get("/activities", response_model=DayPlaybackResponse)
def activities(
    date: str = Query(..., description="Playback date in YYYY-MM-DD format."),
    conn: sqlite3.Connection = Depends(get_db),
) -> DayPlaybackResponse:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    if date not in set(list_available_dates(conn)):
        raise HTTPException(status_code=404, detail="No activities found for this date.")
    return DayPlaybackResponse(**build_day_playback(conn, date))
