from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from app.database import get_db
from app.models import BackfillCoverageResponse
from ingestion.db import get_backfill_coverage


router = APIRouter(tags=["coverage"])


@router.get("/backfill/coverage", response_model=BackfillCoverageResponse)
def backfill_coverage(conn: sqlite3.Connection = Depends(get_db)) -> BackfillCoverageResponse:
    return BackfillCoverageResponse(**get_backfill_coverage(conn))
