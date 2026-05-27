from __future__ import annotations

from fastapi import APIRouter, Query

from app.processes.backfill_runner import runner
from app.models import BackfillRunResponse


router = APIRouter(tags=["backfill"])


@router.get("/backfill/job", response_model=BackfillRunResponse)
def backfill_job() -> BackfillRunResponse:
    return BackfillRunResponse(**runner.status())


@router.post("/backfill/run", response_model=BackfillRunResponse)
def backfill_run(
    steps: int = Query(default=10, ge=1, description="How many athlete-month backfill steps should run."),
) -> BackfillRunResponse:
    return BackfillRunResponse(**runner.start(steps))


@router.post("/backfill/stop", response_model=BackfillRunResponse)
def backfill_stop() -> BackfillRunResponse:
    return BackfillRunResponse(**runner.stop())
