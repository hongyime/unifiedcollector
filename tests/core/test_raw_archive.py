import asyncio
from contextlib import asynccontextmanager

import pytest

from src.core.raw_archive import (
    _PENDING_FAILURE_TASKS,
    raw_archive_content_id,
    raw_archive_entity_id,
    report_raw_archive_result,
)
from src.core.vault import RawPayloadResult


class _Conn:
    def __init__(self):
        self.execute_calls = []

    async def execute(self, *args):
        self.execute_calls.append(args)
        return "INSERT 0 1"


class _Pool:
    def __init__(self):
        self.conn = _Conn()

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def test_raw_archive_identifiers_fit_dlq_columns():
    assert raw_archive_entity_id("telegram", {"platform_chat_id": "123"}) == "123"
    assert raw_archive_content_id("messages/" + ("x" * 200)).startswith("raw:messages/")
    assert len(raw_archive_content_id("messages/" + ("x" * 200))) == 100


@pytest.mark.asyncio
async def test_report_raw_archive_result_queues_failed_write():
    _PENDING_FAILURE_TASKS.clear()
    pool = _Pool()

    report_raw_archive_result(
        pool,
        source="telegram",
        artifact_id="messages/456",
        result=RawPayloadResult(ok=False, error="vault unavailable"),
        metadata={"platform_chat_id": "123"},
    )
    await asyncio.sleep(0)

    assert pool.conn.execute_calls
    query, source, entity_id, content_id, error = pool.conn.execute_calls[0]
    assert "dead_letter_queue" in query
    assert source == "telegram"
    assert entity_id == "123"
    assert content_id == "raw:messages/456"
    assert "vault unavailable" in error


@pytest.mark.asyncio
async def test_report_raw_archive_result_respects_pending_task_limit(monkeypatch):
    _PENDING_FAILURE_TASKS.clear()
    monkeypatch.setenv("RAW_ARCHIVE_FAILURE_TASK_LIMIT", "0")
    pool = _Pool()

    report_raw_archive_result(
        pool,
        source="whatsapp",
        artifact_id="messages/456",
        result=RawPayloadResult(ok=False, error="cannot allocate memory"),
        metadata={"platform_chat_id": "123"},
    )
    await asyncio.sleep(0)

    assert pool.conn.execute_calls == []
