from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from app.database import get_db
from ingestion.db import list_available_dates


router = APIRouter(tags=["dates"])


@router.get("/dates")
def dates(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    return {"dates": list_available_dates(conn)}
