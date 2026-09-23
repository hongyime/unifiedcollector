"""Regressions for capture evidence under native-table lag and DB timeouts."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TypedDict

import pytest

from src.core import source_freshness


class EvidenceRow(TypedDict, total=False):
    source: str
    status: str
    last_error: str | None
    last_success_at: datetime | None
    updated_at: datetime | None
    platform: str
    last_seen_at: datetime
    last_content_at: datetime
    age_seconds: int
    endpoint: str
    observed_count: int
    stored_count: int


@dataclass(frozen=True, slots=True)
class EvidenceConnection:
    native_age: int | None
    content_age: int | None = 30
    heartbeat_age: int | None = 20
    health_status: str = "degraded"
    content_timeout: bool = False

    async def fetch(
        self, query: str, *args: list[str] | int, timeout: float = 8,
    ) -> list[EvidenceRow]:
        now = datetime.now(timezone.utc)
        if "FROM source_health" in query:
            return [{
                "source": "x", "status": self.health_status,
                "last_error": "browser capture stalled: old content (watchdog)",
                "last_success_at": None, "updated_at": None,
            }]
        if "endpoint = 'browser_heartbeat'" in query:
            if self.heartbeat_age is None:
                raise TimeoutError("heartbeat query unavailable")
            return [{
                "platform": "x", "last_seen_at": now - timedelta(seconds=self.heartbeat_age),
                "age_seconds": self.heartbeat_age,
            }]
        if "latest.created_at AS last_content_at" in query:
            if self.content_timeout:
                raise TimeoutError("content query unavailable")
            if self.content_age is None:
                return []
            return [{
                "platform": "x", "last_content_at": now - timedelta(seconds=self.content_age),
                "age_seconds": self.content_age, "endpoint": "posts",
                "observed_count": 2, "stored_count": 2,
            }]
        return []

    async def fetchval(self, query: str, *, timeout: float = 8) -> int | None:
        if self.native_age is None:
            raise TimeoutError("native freshness query unavailable")
        return self.native_age


@pytest.fixture(autouse=True)
def x_freshness_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_SOURCE_MANUAL_MODE", "0")
    monkeypatch.setenv("BROWSER_CONTENT_STALE_X_SECONDS", "7200")
    monkeypatch.setattr(source_freshness, "FRESHNESS", [
        ("x", "SELECT extract(epoch FROM now()-max(collected_at)) FROM x_posts", 172800),
    ])


@pytest.mark.parametrize("native_age", [432000, None])
async def test_fresh_content_proves_capture_when_native_freshness_is_stale_or_unavailable(
    native_age: int | None,
) -> None:
    # Given useful recent posts and a lagging/unavailable native-table query.
    conn = EvidenceConnection(native_age=native_age)
    # When the shared liveness reader classifies X.
    row, = await source_freshness.compute_liveness(conn)
    # Then capture is live, independently of the old watchdog marker.
    assert row["status"] == "live", "fresh useful browser content must determine capture liveness"
    assert row["age_seconds"] == 30
    assert row["browser_content_stale"] is False


async def test_query_timeouts_leave_capture_unknown_not_stale() -> None:
    # Given unavailable native, heartbeat and content evidence.
    conn = EvidenceConnection(None, None, None, content_timeout=True)
    # When freshness is evaluated.
    row, = await source_freshness.compute_liveness(conn)
    # Then no affirmative stall or success is fabricated.
    assert row["status"] == "unknown"
    assert row["browser_content_stale"] is False
    assert row["age_seconds"] is None


async def test_heartbeat_without_content_does_not_prove_capture() -> None:
    # Given a responding extension but no readable content-age evidence.
    conn = EvidenceConnection(None, None, 20, content_timeout=True)
    # When freshness is evaluated.
    row, = await source_freshness.compute_liveness(conn)
    # Then the heartbeat alone cannot make capture live.
    assert row["status"] == "unknown"
    assert row["browser_content_stale"] is False


async def test_heartbeat_does_not_hide_observed_stale_content() -> None:
    # Given a live extension heartbeat, old posts, and no newer useful events.
    conn = EvidenceConnection(9000, None)
    # When freshness is evaluated.
    row, = await source_freshness.compute_liveness(conn)
    # Then the affirmative stale observation is retained.
    assert row["status"] == "degraded"
    assert row["browser_content_stale"] is True


@pytest.mark.parametrize("health_status,expected", [("dead", "dead"), ("auth_paused", "degraded")])
async def test_fresh_content_preserves_explicit_source_failure(
    health_status: str, expected: str,
) -> None:
    # Given recent content but an explicit source-health failure.
    conn = EvidenceConnection(60, health_status=health_status)
    # When freshness is evaluated.
    row, = await source_freshness.compute_liveness(conn)
    # Then browser progress does not clear explicit source failure.
    assert row["status"] == expected
