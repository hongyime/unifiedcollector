from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.database import get_db
from app.models import AthleteDetailResponse, AthleteListResponse, AthleteRoutesResponse
from ingestion.db import build_athlete_route_history, get_athlete_detail, list_athletes


router = APIRouter(tags=["athletes"])


@router.get("/athletes", response_model=AthleteListResponse)
def athletes(
    date: str | None = Query(default=None, description="Optional YYYY-MM-DD filter."),
    month: str | None = Query(default=None, description="Optional YYYY-MM month filter."),
    limit: int = Query(100, ge=1, le=1000, description="Max athletes to return"),
    offset: int = Query(0, ge=0, description="Number of athletes to skip"),
    conn: sqlite3.Connection = Depends(get_db),
) -> AthleteListResponse:
    return AthleteListResponse(athletes=list_athletes(conn, date, month, limit, offset))


@router.get("/athletes/{athlete_id}", response_model=AthleteDetailResponse)
def athlete_detail(
    athlete_id: int = Path(..., ge=1),
    month: str | None = Query(default=None, description="Optional YYYY-MM month filter."),
    conn: sqlite3.Connection = Depends(get_db),
) -> AthleteDetailResponse:
    detail = get_athlete_detail(conn, athlete_id, month)
    if detail is None:
        raise HTTPException(status_code=404, detail="Athlete not found.")
    return AthleteDetailResponse(**detail)


@router.get("/athletes/{athlete_id}/routes", response_model=AthleteRoutesResponse)
def athlete_routes(
    athlete_id: int = Path(..., ge=1),
    conn: sqlite3.Connection = Depends(get_db),
) -> AthleteRoutesResponse:
    payload = build_athlete_route_history(conn, athlete_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Athlete not found.")
    return AthleteRoutesResponse(**payload)
