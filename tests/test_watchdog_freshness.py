import importlib
import time

import pytest


@pytest.mark.asyncio
async def test_watchdog_skips_stale_restart_during_active_429_cooldown(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://collector:collector@localhost/unifiedcollector")
    import src.watchdog.freshness as freshness

    freshness = importlib.reload(freshness)
    monkeypatch.setattr(
        freshness,
        "CHECKS",
        {"instagram": ("SELECT 20", 10, ["unifiedcollector_collector_instagram"])},
    )
    monkeypatch.setattr(freshness, "_last_restart", {})
    monkeypatch.setattr(freshness, "_last_cooldown_stale_alert", {})

    restarted: list[str] = []
    degraded: list[tuple[str, float, bool, str | None]] = []
    notified: list[str] = []

    async def fake_restart(container: str) -> None:
        restarted.append(container)

    async def fake_mark_degraded(db, source: str, age: float, restarted_any: bool, detail: str | None = None) -> None:
        degraded.append((source, age, restarted_any, detail))

    async def fake_notify(text: str) -> None:
        notified.append(text)

    monkeypatch.setattr(freshness, "_restart", fake_restart)
    monkeypatch.setattr(freshness, "_mark_degraded", fake_mark_degraded)
    monkeypatch.setattr(freshness, "_notify", fake_notify)

    class FakeDB:
        async def fetchval(self, query: str):
            assert query == "SELECT 20"
            return 20

        async def fetchrow(self, query: str, *args):
            if "FROM service_cursors" in query:
                return {
                    "service": "instagram_rate_limit",
                    "last_processed_id": f"{time.time() + 3600}:12",
                }
            raise AssertionError(query)

    await freshness._tick(FakeDB())

    assert restarted == []
    assert len(degraded) == 1
    source, age, restarted_any, detail = degraded[0]
    assert source == "instagram"
    assert age == 20
    assert restarted_any is False
    assert detail and "active HTTP 429 cooldown" in detail
    assert "not restarted" in detail
    assert notified
    assert "stale but cooling down" in notified[0]


@pytest.mark.asyncio
async def test_watchdog_bypasses_cooldown_and_restarts_realtime_source(monkeypatch):
    """Realtime sources (telegram/whatsapp/beeper) must restart when stale even
    if a per-account FloodWait cooldown is active — the cooldown reflects a
    specific backfill/resolve API path, not the live event stream, and a dead
    MTProto/WS connection can only be healed by restart. Regression pin for
    the incident where telegram sat 4h stale while a backfill FloodWait from
    hours earlier kept the watchdog deferring restarts indefinitely.
    """
    monkeypatch.setenv("DATABASE_URL", "postgres://collector:collector@localhost/unifiedcollector")
    import src.watchdog.freshness as freshness

    freshness = importlib.reload(freshness)
    monkeypatch.setattr(
        freshness,
        "CHECKS",
        {"telegram": ("SELECT 20000", 3600, ["unifiedcollector_collector_telegram"])},
    )
    monkeypatch.setattr(freshness, "_last_restart", {})

    restarted: list[str] = []
    degraded: list[tuple[str, float, bool, str | None]] = []

    async def fake_restart(container: str) -> None:
        restarted.append(container)

    async def fake_mark_degraded(db, source: str, age: float, restarted_any: bool, detail: str | None = None) -> None:
        degraded.append((source, age, restarted_any, detail))

    async def fake_notify(text: str) -> None:
        pass

    monkeypatch.setattr(freshness, "_restart", fake_restart)
    monkeypatch.setattr(freshness, "_mark_degraded", fake_mark_degraded)
    monkeypatch.setattr(freshness, "_notify", fake_notify)

    class FakeDB:
        async def fetchval(self, query: str):
            assert query == "SELECT 20000"
            return 20000  # ~5.5h stale, well over 1h telegram threshold

        async def fetchrow(self, query: str, *args):
            # Simulate an active FloodWait cooldown record — must be ignored.
            if "FROM service_cursors" in query:
                return {
                    "service": "telegram_rate_limit",
                    "last_processed_id": f"{time.time() + 36000}:1",
                }
            raise AssertionError(query)

    await freshness._tick(FakeDB())

    assert restarted == ["unifiedcollector_collector_telegram"]
    assert len(degraded) == 1
    _src, _age, restarted_any, _detail = degraded[0]
    assert restarted_any is True


@pytest.mark.asyncio
async def test_watchdog_clears_stale_marker_after_source_recovers(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://collector:collector@localhost/unifiedcollector")
    import src.watchdog.freshness as freshness

    freshness = importlib.reload(freshness)
    monkeypatch.setattr(
        freshness,
        "CHECKS",
        {"website": ("SELECT 5", 10, ["unifiedcollector_collector_website"])},
    )

    restarted: list[str] = []
    degraded: list[tuple[str, float, bool, str | None]] = []
    executed: list[tuple[str, tuple]] = []

    async def fake_restart(container: str) -> None:
        restarted.append(container)

    async def fake_mark_degraded(db, source: str, age: float, restarted_any: bool, detail: str | None = None) -> None:
        degraded.append((source, age, restarted_any, detail))

    monkeypatch.setattr(freshness, "_restart", fake_restart)
    monkeypatch.setattr(freshness, "_mark_degraded", fake_mark_degraded)

    class FakeDB:
        async def fetchval(self, query: str):
            assert query == "SELECT 5"
            return 5

        async def execute(self, query: str, *args):
            executed.append((query, args))

    await freshness._tick(FakeDB())

    assert restarted == []
    assert degraded == []
    assert len(executed) == 1
    query, args = executed[0]
    assert "UPDATE source_health" in query
    assert "LIKE 'stale %watchdog%'" in query
    assert "LIKE 'watchdog waiting for qr pairing%'" in query
    assert args == ("website",)


@pytest.mark.asyncio
async def test_mark_degraded_words_whatsapp_qr_pairing_as_operator_action(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://collector:collector@localhost/unifiedcollector")
    import src.watchdog.freshness as freshness

    freshness = importlib.reload(freshness)
    executed: list[tuple[str, tuple]] = []

    class FakeDB:
        async def execute(self, query: str, *args):
            executed.append((query, args))

    await freshness._mark_degraded(
        FakeDB(),
        "whatsapp",
        20,
        False,
        "waiting for QR pairing; not restarted",
    )

    assert len(executed) == 1
    _query, args = executed[0]
    assert args == (
        "whatsapp",
        "watchdog waiting for QR pairing; not restarted; newest row 20s ago",
    )


@pytest.mark.asyncio
async def test_watchdog_does_not_restart_whatsapp_waiting_for_qr(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://collector:collector@localhost/unifiedcollector")
    import src.watchdog.freshness as freshness

    freshness = importlib.reload(freshness)
    monkeypatch.setattr(
        freshness,
        "CHECKS",
        {
            "whatsapp": (
                "SELECT 20",
                10,
                ["unifiedcollector_wa_bridge_1", "unifiedcollector_wa_bridge_2"],
            )
        },
    )

    restarted: list[str] = []
    degraded: list[tuple[str, float, bool, str | None]] = []

    async def fake_restart(container: str) -> None:
        restarted.append(container)

    async def fake_mark_degraded(db, source: str, age: float, restarted_any: bool, detail: str | None = None) -> None:
        degraded.append((source, age, restarted_any, detail))

    async def fake_whatsapp_pairing_needed() -> str:
        return "waiting for QR pairing; not restarted"

    monkeypatch.setattr(freshness, "_restart", fake_restart)
    monkeypatch.setattr(freshness, "_mark_degraded", fake_mark_degraded)
    monkeypatch.setattr(freshness, "_whatsapp_pairing_needed", fake_whatsapp_pairing_needed)

    class FakeDB:
        async def fetchval(self, query: str):
            assert query == "SELECT 20"
            return 20

    await freshness._tick(FakeDB())

    assert restarted == []
    assert degraded == [("whatsapp", 20, False, "waiting for QR pairing; not restarted")]


@pytest.mark.asyncio
async def test_watchdog_defers_whatsapp_restart_when_bridge_health_unavailable(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://collector:collector@localhost/unifiedcollector")
    import src.watchdog.freshness as freshness

    freshness = importlib.reload(freshness)
    monkeypatch.setattr(
        freshness,
        "CHECKS",
        {"whatsapp": ("SELECT 20", 10, ["unifiedcollector_wa_bridge_1", "unifiedcollector_wa_bridge_2"])},
    )

    restarted: list[str] = []
    degraded: list[tuple[str, float, bool, str | None]] = []

    async def fake_restart(container: str) -> None:
        restarted.append(container)

    async def fake_mark_degraded(db, source: str, age: float, restarted_any: bool, detail: str | None = None) -> None:
        degraded.append((source, age, restarted_any, detail))

    async def fake_whatsapp_pairing_needed() -> str:
        return "bridge health unavailable; restart deferred to avoid QR pairing churn"

    monkeypatch.setattr(freshness, "_restart", fake_restart)
    monkeypatch.setattr(freshness, "_mark_degraded", fake_mark_degraded)
    monkeypatch.setattr(freshness, "_whatsapp_pairing_needed", fake_whatsapp_pairing_needed)

    class FakeDB:
        async def fetchval(self, query: str):
            assert query == "SELECT 20"
            return 20

    await freshness._tick(FakeDB())

    assert restarted == []
    assert degraded == [
        ("whatsapp", 20, False, "bridge health unavailable; restart deferred to avoid QR pairing churn")
    ]


@pytest.mark.asyncio
async def test_whatsapp_pairing_needed_defers_unreachable_health(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://collector:collector@localhost/unifiedcollector")
    import src.watchdog.freshness as freshness

    freshness = importlib.reload(freshness)

    async def fake_fetch_whatsapp_bridge_health(timeout: int = 0):
        return [{"bridge": "1", "ok": False}, {"bridge": "2", "ok": False}]

    monkeypatch.setattr(
        "src.core.whatsapp_bridge_health.fetch_whatsapp_bridge_health",
        fake_fetch_whatsapp_bridge_health,
    )

    detail = await freshness._whatsapp_pairing_needed()

    assert detail == "bridge health unavailable; restart deferred to avoid QR pairing churn"


@pytest.mark.asyncio
async def test_watchdog_clears_dlq_marker_after_queue_drains(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://collector:collector@localhost/unifiedcollector")
    import src.watchdog.freshness as freshness

    freshness = importlib.reload(freshness)

    executed: list[tuple[str, tuple]] = []

    class FakeDB:
        async def fetch(self, query: str):
            if "FROM dead_letter_queue WHERE status='pending' GROUP BY source" in query:
                return []
            if "FROM source_health" in query and "dlq backlog:%watchdog%" in query.lower():
                return [{"source": "threads"}]
            raise AssertionError(query)

        async def execute(self, query: str, *args):
            executed.append((query, args))

    await freshness._dlq_tick(FakeDB())

    assert len(executed) == 1
    query, args = executed[0]
    assert "UPDATE source_health" in query
    assert "LIKE 'dlq backlog:%watchdog%'" in query
    assert args == ("threads",)


@pytest.mark.asyncio
async def test_browser_source_tick_marks_stalled_browser_source(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://collector:collector@localhost/unifiedcollector")
    import src.watchdog.freshness as freshness

    freshness = importlib.reload(freshness)
    monkeypatch.setattr(freshness, "BROWSER_SOURCE_WATCH_SOURCES", {"facebook"})
    monkeypatch.setattr(freshness, "_last_browser_source_alert", {})

    degraded: list[tuple[str, str]] = []
    cleared: list[str] = []
    notified: list[str] = []

    async def fake_compute_liveness(db):
        return [
            {
                "source": "facebook",
                "status": "degraded",
                "detail": "Chrome extension heartbeat is 7200s old (> 3600s)",
                "browser_heartbeat_age_seconds": 7200,
                "browser_content_stale": True,
            }
        ]

    async def fake_mark_degraded(db, source: str, detail: str) -> None:
        degraded.append((source, detail))

    async def fake_mark_running(db, source: str) -> None:
        cleared.append(source)

    async def fake_notify(text: str) -> None:
        notified.append(text)

    monkeypatch.setattr("src.core.source_freshness.compute_liveness", fake_compute_liveness)
    monkeypatch.setattr(freshness, "_mark_degraded_browser_source", fake_mark_degraded)
    monkeypatch.setattr(freshness, "_mark_running_if_browser_watchdog", fake_mark_running)
    monkeypatch.setattr(freshness, "_notify", fake_notify)

    await freshness._browser_source_tick(object())

    assert degraded == [("facebook", "Chrome extension heartbeat is 7200s old (> 3600s)")]
    assert cleared == []
    assert notified
    assert "facebook browser collection stalled" in notified[0]
    assert "Container restart will not fix" in notified[0]


def test_clean_browser_source_detail_removes_nested_watchdog_prefixes():
    import src.watchdog.freshness as freshness

    detail = (
        "browser capture stalled: browser capture stalled: "
        "Chrome extension heartbeat is 7200s old (> 3600s) (watchdog); "
        "Chrome extension heartbeat is 7200s old (> 3600s); "
        "browser content progress is 9000s old (> 3600s) (watchdog)"
    )

    cleaned = freshness._clean_browser_source_detail(detail)

    assert cleaned == (
        "Chrome extension heartbeat is 7200s old (> 3600s); "
        "browser content progress is 9000s old (> 3600s)"
    )
    assert "browser capture stalled" not in cleaned
    assert "(watchdog)" not in cleaned


@pytest.mark.asyncio
async def test_browser_source_tick_clears_recovered_browser_source(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://collector:collector@localhost/unifiedcollector")
    import src.watchdog.freshness as freshness

    freshness = importlib.reload(freshness)
    monkeypatch.setattr(freshness, "BROWSER_SOURCE_WATCH_SOURCES", {"facebook"})

    degraded: list[tuple[str, str]] = []
    cleared: list[str] = []

    async def fake_compute_liveness(db):
        return [
            {
                "source": "facebook",
                "status": "live",
                "detail": "newest row is inside the freshness window",
                "browser_heartbeat_age_seconds": 45,
                "browser_content_stale": False,
            }
        ]

    async def fake_mark_degraded(db, source: str, detail: str) -> None:
        degraded.append((source, detail))

    async def fake_mark_running(db, source: str) -> None:
        cleared.append(source)

    monkeypatch.setattr("src.core.source_freshness.compute_liveness", fake_compute_liveness)
    monkeypatch.setattr(freshness, "_mark_degraded_browser_source", fake_mark_degraded)
    monkeypatch.setattr(freshness, "_mark_running_if_browser_watchdog", fake_mark_running)

    await freshness._browser_source_tick(object())

    assert degraded == []
    assert cleared == ["facebook"]




@pytest.mark.asyncio
async def test_telegram_default_stale_threshold_is_1h(monkeypatch):
    """Regression pin: telegram default staleness threshold is 3600s (1h).

    Under normal load (4 accounts / 162 targets) a 1h idle window is already
    very unusual; earlier detection triggers faster self-heal. Widening this
    back to 2h reintroduces the incident where telegram sat dead for hours
    while the watchdog waited out an over-generous threshold.
    """
    # Ensure no env override leaks in from the test runner.
    monkeypatch.delenv("WATCHDOG_STALE_TELEGRAM", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgres://collector:collector@localhost/unifiedcollector")
    import src.watchdog.freshness as freshness

    freshness = importlib.reload(freshness)

    _query, threshold, containers = freshness.CHECKS["telegram"]
    assert threshold == 3600, f"telegram threshold must be 1h/3600s, got {threshold}s"
    assert containers == ["unifiedcollector_collector_telegram"]



# ─── Container-liveness sweep tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_container_sweep_restarts_stopped_container(monkeypatch):
    """A single stopped project container is started once via the Docker API."""
    monkeypatch.setenv("DATABASE_URL", "postgres://collector:collector@localhost/unifiedcollector")
    import src.watchdog.freshness as freshness

    freshness = importlib.reload(freshness)
    monkeypatch.setattr(freshness, "_liveness_restarts", {})

    started: list[str] = []

    async def fake_list():
        # Two containers: one running (skip), one exited (restart target).
        return [
            {"Names": ["/unifiedcollector_backup"], "State": "exited"},
            {"Names": ["/unifiedcollector_scheduler"], "State": "running"},
        ]

    async def fake_start(name: str) -> int:
        started.append(name)
        return 204  # Docker "No Content" = started

    monkeypatch.setattr(freshness, "_list_project_containers", fake_list)
    monkeypatch.setattr(freshness, "_start_container", fake_start)

    await freshness._sweep_container_liveness(now=1_000_000.0)

    assert started == ["unifiedcollector_backup"]
    # One attempt recorded in history for the crash-loop cap.
    assert freshness._liveness_restarts["unifiedcollector_backup"] == [1_000_000.0]


@pytest.mark.asyncio
async def test_container_sweep_respects_crash_loop_cap(monkeypatch):
    """4th restart attempt within the window must be skipped, not fired.

    Regression pin: the watchdog itself must not amplify a crash loop by
    hammering `start` on a container that keeps dying. Docker's own restart
    policy handles the retry rhythm; this sweep only intervenes when that
    policy is stuck.
    """
    monkeypatch.setenv("DATABASE_URL", "postgres://collector:collector@localhost/unifiedcollector")
    import src.watchdog.freshness as freshness

    freshness = importlib.reload(freshness)

    now = 1_000_000.0
    # Pre-load 3 recent attempts (cap = 3). Next attempt must be skipped.
    monkeypatch.setattr(
        freshness,
        "_liveness_restarts",
        {"unifiedcollector_backup": [now - 900, now - 600, now - 100]},
    )
    # Also drop LIVENESS_MAX_RESTARTS explicit assertion in this test's scope:
    assert freshness.LIVENESS_MAX_RESTARTS == 3
    assert freshness.LIVENESS_WINDOW_SECONDS == 1800

    started: list[str] = []

    async def fake_list():
        return [{"Names": ["/unifiedcollector_backup"], "State": "exited"}]

    async def fake_start(name: str) -> int:
        started.append(name)
        return 204

    monkeypatch.setattr(freshness, "_list_project_containers", fake_list)
    monkeypatch.setattr(freshness, "_start_container", fake_start)

    await freshness._sweep_container_liveness(now=now)

    assert started == []  # capped, must not fire
    # History untouched (still 3 entries — the sweep never appended a 4th).
    assert len(freshness._liveness_restarts["unifiedcollector_backup"]) == 3


@pytest.mark.asyncio
async def test_container_sweep_prunes_old_attempts_outside_window(monkeypatch):
    """History older than the window slides out — a stopped container that
    recovered on its own and later re-fails must not be capped by ancient
    attempts."""
    monkeypatch.setenv("DATABASE_URL", "postgres://collector:collector@localhost/unifiedcollector")
    import src.watchdog.freshness as freshness

    freshness = importlib.reload(freshness)

    now = 1_000_000.0
    # 3 attempts, all older than 1800s window: should be pruned and 4th allowed.
    monkeypatch.setattr(
        freshness,
        "_liveness_restarts",
        {"unifiedcollector_backup": [now - 5000, now - 4000, now - 3000]},
    )

    started: list[str] = []

    async def fake_list():
        return [{"Names": ["/unifiedcollector_backup"], "State": "exited"}]

    async def fake_start(name: str) -> int:
        started.append(name)
        return 204

    monkeypatch.setattr(freshness, "_list_project_containers", fake_list)
    monkeypatch.setattr(freshness, "_start_container", fake_start)

    await freshness._sweep_container_liveness(now=now)

    assert started == ["unifiedcollector_backup"]
    assert freshness._liveness_restarts["unifiedcollector_backup"] == [now]


@pytest.mark.asyncio
async def test_container_sweep_ignores_healthy(monkeypatch):
    """All running/restarting/paused containers untouched — no start calls."""
    monkeypatch.setenv("DATABASE_URL", "postgres://collector:collector@localhost/unifiedcollector")
    import src.watchdog.freshness as freshness

    freshness = importlib.reload(freshness)
    monkeypatch.setattr(freshness, "_liveness_restarts", {})

    started: list[str] = []

    async def fake_list():
        return [
            {"Names": ["/unifiedcollector_scheduler"], "State": "running"},
            {"Names": ["/unifiedcollector_collector_telegram"], "State": "running"},
            {"Names": ["/unifiedcollector_collector_website"], "State": "restarting"},
            {"Names": ["/unifiedcollector_wa_bridge_1"], "State": "paused"},
        ]

    async def fake_start(name: str) -> int:
        started.append(name)
        return 204

    monkeypatch.setattr(freshness, "_list_project_containers", fake_list)
    monkeypatch.setattr(freshness, "_start_container", fake_start)

    await freshness._sweep_container_liveness(now=1_000_000.0)

    assert started == []
    assert freshness._liveness_restarts == {}


@pytest.mark.asyncio
async def test_container_sweep_survives_docker_error(monkeypatch):
    """Docker API blip must not kill the loop — log and continue.

    The whole point of the watchdog is to be the safety net; a Docker socket
    hiccup cannot become a reason the safety net dies. Freshness checks share
    this loop, so any exception here has broad blast radius.
    """
    monkeypatch.setenv("DATABASE_URL", "postgres://collector:collector@localhost/unifiedcollector")
    import src.watchdog.freshness as freshness

    freshness = importlib.reload(freshness)

    async def fake_list():
        raise RuntimeError("docker socket EPIPE")

    async def fake_start(name: str) -> int:
        raise AssertionError("start should never be reached when list fails")

    monkeypatch.setattr(freshness, "_list_project_containers", fake_list)
    monkeypatch.setattr(freshness, "_start_container", fake_start)

    # Must not raise.
    await freshness._sweep_container_liveness(now=1_000_000.0)


@pytest.mark.asyncio
async def test_container_sweep_skips_self(monkeypatch):
    """The watchdog must never try to restart its own container mid-loop.

    Docker's restart:unless-stopped on the watchdog already covers a real
    death; a mid-loop self-start attempt is a footgun (races the healthcheck,
    can double-book a container name in some Docker versions).
    """
    monkeypatch.setenv("DATABASE_URL", "postgres://collector:collector@localhost/unifiedcollector")
    import src.watchdog.freshness as freshness

    freshness = importlib.reload(freshness)
    monkeypatch.setattr(freshness, "_liveness_restarts", {})

    started: list[str] = []

    async def fake_list():
        # Hypothetically the watchdog reports itself as exited — should still
        # be skipped by name check.
        return [{"Names": ["/unifiedcollector_watchdog"], "State": "exited"}]

    async def fake_start(name: str) -> int:
        started.append(name)
        return 204

    monkeypatch.setattr(freshness, "_list_project_containers", fake_list)
    monkeypatch.setattr(freshness, "_start_container", fake_start)

    await freshness._sweep_container_liveness(now=1_000_000.0)

    assert started == []


@pytest.mark.asyncio
async def test_container_sweep_treats_dead_and_created_as_stopped(monkeypatch):
    """State='dead' and state='created' are both treated as restart candidates."""
    monkeypatch.setenv("DATABASE_URL", "postgres://collector:collector@localhost/unifiedcollector")
    import src.watchdog.freshness as freshness

    freshness = importlib.reload(freshness)
    monkeypatch.setattr(freshness, "_liveness_restarts", {})

    started: list[str] = []

    async def fake_list():
        return [
            {"Names": ["/unifiedcollector_backup"], "State": "dead"},
            {"Names": ["/unifiedcollector_scheduler"], "State": "created"},
        ]

    async def fake_start(name: str) -> int:
        started.append(name)
        return 204

    monkeypatch.setattr(freshness, "_list_project_containers", fake_list)
    monkeypatch.setattr(freshness, "_start_container", fake_start)

    await freshness._sweep_container_liveness(now=1_000_000.0)

    assert sorted(started) == sorted(
        ["unifiedcollector_backup", "unifiedcollector_scheduler"]
    )


def test_headless_watchdog_uses_canonical_lowrisk_progress_queries(monkeypatch):
    """GitHub/Strava share collector_lowrisk, so stale checks must match the
    dashboard/core liveness basis and not restart the shared container just
    because one narrow table, such as github_commits, is quiet.
    """
    monkeypatch.setenv("DATABASE_URL", "postgres://collector:collector@localhost/unifiedcollector")
    monkeypatch.setenv("WATCHDOG_HEADLESS_ENABLED", "1")

    import src.watchdog.freshness as freshness
    from src.core.source_freshness import GITHUB_PROGRESS_QUERY, STRAVA_PROGRESS_QUERY

    freshness = importlib.reload(freshness)

    assert freshness.CHECKS["github"][0] == GITHUB_PROGRESS_QUERY
    assert "github_issue_comments" in freshness.CHECKS["github"][0]
    assert "github_pr_reviews" in freshness.CHECKS["github"][0]
    assert "github_edges" in freshness.CHECKS["github"][0]

    assert freshness.CHECKS["strava"][0] == STRAVA_PROGRESS_QUERY
    assert "strava_athletes" in freshness.CHECKS["strava"][0]
