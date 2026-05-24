from __future__ import annotations

from fastapi import APIRouter, Query

from app.models import BackfillRunResponse
from app.processes.sync_runner import runner


router = APIRouter(tags=["sync"])


@router.get("/sync/job", response_model=BackfillRunResponse)
def sync_job() -> BackfillRunResponse:
    return BackfillRunResponse(**runner.status())


@router.post("/sync/run", response_model=BackfillRunResponse)
def sync_run(
    date: str | None = Query(default=None, description="Optional YYYY-MM-DD date to sync."),
    refresh_following_roster: bool = Query(default=False, description="Refresh the following roster before syncing."),
) -> BackfillRunResponse:
    return BackfillRunResponse(**runner.start(date, refresh_following_roster))


@router.post("/sync/stop", response_model=BackfillRunResponse)
def sync_stop() -> BackfillRunResponse:
    return BackfillRunResponse(**runner.stop())
