import asyncio
import logging

import pytest

from src.worker import WorkerService


@pytest.mark.asyncio
async def test_report_health_timeout_is_logged_once_under_pressure(caplog):
    class Pool:
        async def acquire(self):
            raise asyncio.TimeoutError()

    svc = WorkerService()
    svc.pool = Pool()

    with caplog.at_level(logging.WARNING):
        await svc._report_health("running")
        await svc._report_health("running")

    messages = [r.getMessage() for r in caplog.records if "Health report failed" in r.getMessage()]
    assert len(messages) == 1
    assert "TimeoutError" in messages[0]


@pytest.mark.asyncio
async def test_report_health_release_cancel_does_not_fail():
    class Conn:
        async def execute(self, *args, **kwargs):
            return None

    class Pool:
        async def acquire(self):
            return Conn()

        async def release(self, conn):
            raise asyncio.CancelledError()

    svc = WorkerService()
    svc.pool = Pool()

    await svc._report_health("running")
