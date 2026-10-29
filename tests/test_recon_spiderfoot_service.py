import asyncio
import datetime as dt
import os
from pathlib import Path
from types import SimpleNamespace

from src import recon_spiderfoot_service
from src.recon_spiderfoot_service import (
    _RunWindow,
    _env_float,
    _redact_report,
    _run_window_config,
    _worker_count,
    format_report,
)


def test_format_report_omits_target_value_from_default_logs():
    line = format_report({
        "status": "completed",
        "target": {
            "target_type": "email",
            "target_value": "person@example.com",
        },
        "modules": ["sfp_dnsresolve", "sfp_whois"],
        "observations": 3,
        "dry_run": False,
    })

    assert "status=completed" in line
    assert "target_type=email" in line
    assert "observations=3" in line
    assert "sfp_dnsresolve,sfp_whois" in line
    assert "person@example.com" not in line


def test_format_report_includes_bounded_error():
    line = format_report({
        "status": "failed",
        "target": {"target_type": "domain", "target_value": "example.com"},
        "observations": 0,
        "error": "example.com " * 30,
    })

    assert "target_type=domain" in line
    assert "error=" in line
    assert len(line) < 260
    assert "example.com" not in line


def test_json_log_report_redacts_target_and_error_fields():
    report = {
        "target": {"target_type": "email", "target_value": "private@example.org"},
        "error": "provider echoed private@example.org",
        "nested": [{"reason": "private@example.org"}],
    }
    sanitized = _redact_report(report)
    assert "private@example.org" not in str(sanitized)
    assert report["target"]["target_value"] == "private@example.org"


def test_json_service_output_redacts_target_value(capsys):
    class ConnectionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    class Pool:
        def acquire(self):
            return ConnectionContext()

    async def run_once(*_args, **_kwargs):
        return {
            "status": "failed",
            "target": {"target_type": "email", "target_value": "private@example.org"},
            "error": "provider echoed private@example.org",
        }

    module = SimpleNamespace(run_spiderfoot_once=run_once)
    args = SimpleNamespace(dry_run=False, json=True)
    asyncio.run(recon_spiderfoot_service._run_once(module, Pool(), args, worker_id=1))
    output = capsys.readouterr().out
    assert "private@example.org" not in output
    assert "[redacted]" in output


def test_run_window_enforces_target_reservations():
    async def scenario():
        budget = _RunWindow(max_targets=2, deadline_seconds=60)
        assert await budget.reserve()
        assert await budget.reserve()
        assert not await budget.reserve()
        await budget.finish(True)
        await budget.finish(False)
        assert budget.targets == 1
        assert await budget.reserve()
        await budget.finish(True)
        assert not await budget.reserve()

    asyncio.run(scenario())


def test_run_window_deadline_and_config_are_bounded(monkeypatch):
    monkeypatch.setenv("RECON_MAX_TARGETS_PER_RUN", "999")
    monkeypatch.setenv("RECON_RUN_DEADLINE_SECONDS", "999999")
    assert _run_window_config() == (25, 3600)
    budget = _RunWindow(max_targets=1, deadline_seconds=30)
    budget.started -= 31
    assert asyncio.run(budget.reserve()) is False


def test_worker_count_is_bounded():
    assert _worker_count(None) == 1
    assert _worker_count("bad") == 1
    assert _worker_count("0") == 1
    assert _worker_count("3") == 3
    assert _worker_count("99") == 8


def test_env_float_is_bounded(monkeypatch):
    monkeypatch.delenv("RECON_SPIDERFOOT_POLL_INTERVAL", raising=False)
    assert _env_float("RECON_SPIDERFOOT_POLL_INTERVAL", 60.0) == 60.0

    monkeypatch.setenv("RECON_SPIDERFOOT_POLL_INTERVAL", "bad")
    assert _env_float("RECON_SPIDERFOOT_POLL_INTERVAL", 60.0) == 60.0

    monkeypatch.setenv("RECON_SPIDERFOOT_POLL_INTERVAL", "0")
    assert _env_float("RECON_SPIDERFOOT_POLL_INTERVAL", 60.0) == 0.1

    monkeypatch.setenv("RECON_SPIDERFOOT_POLL_INTERVAL", "10")
    assert _env_float("RECON_SPIDERFOOT_POLL_INTERVAL", 60.0) == 10.0


def test_dry_run_service_skips_migrations_and_background_fp_refresh(monkeypatch):
    calls = []

    class Pool:
        pass

    async def get_pool():
        return Pool()

    async def close_pool():
        calls.append("close")

    async def worker_loop(_worker_id, _module, _pool, args, _budget):
        assert args.dry_run
        calls.append("worker")

    async def forbidden_migration(_pool):
        raise AssertionError("dry-run attempted migrations")

    monkeypatch.setattr(recon_spiderfoot_service, "get_pool", get_pool)
    monkeypatch.setattr(recon_spiderfoot_service, "close_pool", close_pool)
    monkeypatch.setattr(recon_spiderfoot_service, "_load_recon_spiderfoot", lambda: object())
    monkeypatch.setattr(recon_spiderfoot_service, "_worker_loop", worker_loop)
    monkeypatch.setattr(
        recon_spiderfoot_service,
        "admission_snapshot",
        lambda: ({"policy_id": "test"}, {"cohort_limit": 5}, None),
    )
    monkeypatch.setattr(recon_spiderfoot_service, "apply_all", forbidden_migration, raising=False)
    monkeypatch.setattr(recon_spiderfoot_service.asyncio, "create_task", lambda *_args, **_kwargs: calls.append("fp"))

    args = SimpleNamespace(
        dry_run=True,
        once=True,
        workers=1,
        poll_interval=1.0,
        json=False,
    )
    asyncio.run(recon_spiderfoot_service._run(args))

    assert calls == ["worker", "close"]


def test_worker_heartbeat_freshness_detects_stalled_worker(monkeypatch, tmp_path):
    heartbeat = tmp_path / "worker.heartbeat"
    monkeypatch.setenv("RECON_SPIDERFOOT_HEARTBEAT_PATH", str(heartbeat))
    monkeypatch.setenv("RECON_HEALTH_MAX_AGE_SECONDS", "60")
    monkeypatch.setenv("RECON_SPIDERFOOT_WORKERS", "2")

    assert not recon_spiderfoot_service._heartbeat_is_fresh()
    Path(f"{heartbeat}.1").touch()
    Path(f"{heartbeat}.2").touch()
    assert recon_spiderfoot_service._heartbeat_is_fresh()
    os.utime(Path(f"{heartbeat}.2"), (1, 1))
    assert not recon_spiderfoot_service._heartbeat_is_fresh()


def test_database_healthcheck_requires_pool_and_recent_worker_progress(monkeypatch):
    monkeypatch.setenv("SPIDERFOOT_TARGET_TIMEOUT_SECONDS", "180")
    monkeypatch.setenv("RECON_HEALTH_MAX_AGE_SECONDS", "360")
    now = dt.datetime.now(dt.timezone.utc)

    class Conn:
        def __init__(self, status="running", updated_at=now):
            self.status = status
            self.updated_at = updated_at

        async def fetchval(self, sql):
            assert sql == "SELECT 1"
            return 1

        async def fetchrow(self, sql, source):
            assert "source_health" in sql
            assert source == "spiderfoot"
            return {"status": self.status, "updated_at": self.updated_at}

    class Acquire:
        def __init__(self, conn):
            self.conn = conn

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, *_args):
            return False

    class Pool:
        def __init__(self, conn):
            self.conn = conn

        def acquire(self):
            return Acquire(self.conn)

    assert asyncio.run(recon_spiderfoot_service._database_is_healthy(Pool(Conn())))
    assert not asyncio.run(
        recon_spiderfoot_service._database_is_healthy(Pool(Conn(status="degraded")))
    )
    stale = now - dt.timedelta(seconds=600)
    assert not asyncio.run(
        recon_spiderfoot_service._database_is_healthy(Pool(Conn(updated_at=stale)))
    )


def test_worker_healthcheck_accepts_a_valid_halted_admission(monkeypatch):
    async def database_is_healthy(_pool):
        return True

    async def get_pool():
        return object()

    async def close_pool():
        return None

    monkeypatch.setattr(
        recon_spiderfoot_service,
        "admission_snapshot",
        lambda: ({"enabled": True}, {"halt": True}, "error_rate"),
    )
    monkeypatch.setattr(recon_spiderfoot_service, "_heartbeat_is_fresh", lambda: True)
    monkeypatch.setattr(recon_spiderfoot_service, "get_pool", get_pool)
    monkeypatch.setattr(recon_spiderfoot_service, "close_pool", close_pool)
    monkeypatch.setattr(recon_spiderfoot_service, "_database_is_healthy", database_is_healthy)

    assert asyncio.run(recon_spiderfoot_service._check_worker_health())


def test_explicit_schema_init_applies_migrations_once_and_closes_pool(monkeypatch):
    calls = []
    pool = object()

    async def get_pool():
        calls.append("pool")
        return pool

    async def apply_all(actual_pool):
        assert actual_pool is pool
        calls.append("migrate")
        return {"applied": 1}

    async def close_pool():
        calls.append("close")

    monkeypatch.setattr(recon_spiderfoot_service, "get_pool", get_pool)
    monkeypatch.setattr(recon_spiderfoot_service, "apply_all", apply_all)
    monkeypatch.setattr(recon_spiderfoot_service, "close_pool", close_pool)

    result = asyncio.run(recon_spiderfoot_service._run_schema_init())

    assert result == {"applied": 1}
    assert calls == ["pool", "migrate", "close"]
