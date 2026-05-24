from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from app.database import get_db
from app.models import StatusResponse
from ingestion.db import get_status_summary


router = APIRouter(tags=["status"])


@router.get("/status", response_model=StatusResponse)
def status(conn: sqlite3.Connection = Depends(get_db)) -> StatusResponse:
    return StatusResponse(**get_status_summary(conn))
