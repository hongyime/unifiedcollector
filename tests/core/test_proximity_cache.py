import asyncio
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _source() -> str:
    return (REPO_ROOT / "src" / "core" / "proximity.py").read_text(encoding="utf-8")


def test_proximity_cache_refresh_uses_cross_process_advisory_lock():
    src = _source()

    assert "_REFRESH_LOCK_KEY = \"collector:account_proximity_cache_refresh\"" in src
    assert "SELECT pg_try_advisory_lock(hashtext($1))" in src
    assert "SELECT pg_advisory_unlock(hashtext($1))" in src
    assert "{\"skipped\": \"refresh_in_progress\"}" in src


def test_proximity_cache_refresh_preserves_last_good_snapshot():
    src = _source()

    assert "empty_analyzer_snapshot_preserved" in src
    assert "DELETE FROM account_proximity_cache WHERE synced_at < $1::timestamptz" in src
    assert "DELETE FROM account_proximity_cache\")" not in src
    assert "synced_at = EXCLUDED.synced_at" in src


def test_proximity_cache_refresh_has_bounded_analyzer_and_write_timeouts():
    src = _source()

    assert "PROXIMITY_CACHE_ANALYZER_TIMEOUT_SECONDS" in src
    assert "PROXIMITY_CACHE_WRITE_TIMEOUT_SECONDS" in src
    assert "command_timeout=analyzer_timeout" in src
    assert "timeout=write_timeout" in src


def test_analyzer_database_url_is_explicit_only_no_derivation():
    # W7 split: the old urlsplit/urlunsplit path-rewrite of DATABASE_URL to
    # /unifiedanalyzer is gone. The analyzer lives on its own Postgres now, so
    # the collector must be told its URL explicitly via ANALYZER_DATABASE_URL.
    src = _source()

    assert "urlsplit" not in src
    assert "urlunsplit" not in src
    assert 'os.getenv("ANALYZER_DATABASE_URL")' in src


def test_analyzer_database_url_returns_env_or_none(monkeypatch):
    import importlib

    proximity = importlib.import_module("src.core.proximity")

    monkeypatch.delenv("ANALYZER_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgres://collector:pw@postgres:5432/unifiedcollector")
    assert proximity.analyzer_database_url() is None

    monkeypatch.setenv("ANALYZER_DATABASE_URL", "postgres://collector:pw@analyzer_pg:5432/unifiedanalyzer")
    assert proximity.analyzer_database_url() == "postgres://collector:pw@analyzer_pg:5432/unifiedanalyzer"


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, sql: str) -> str:
        self.executed.append(sql)
        return "INSERT 0 1"


class _AcquireCtx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    def acquire(self) -> _AcquireCtx:
        return _AcquireCtx(self.conn)


def _instagram_seed_sql() -> tuple[str, list[str]]:
    import src.core.proximity as proximity

    pool = _FakePool()
    out = asyncio.run(proximity.seed_proximity_backfill_targets(pool))
    # Result key is retained so callers/metrics keep reading out["instagram_queue"].
    assert "instagram_queue" in out
    sql = next(s for s in pool.conn.executed if "INSERT INTO spider_queue (" in s)
    return sql, pool.conn.executed


def test_proximity_instagram_seeds_into_unified_spider_queue():
    sql, executed = _instagram_seed_sql()

    assert "INSERT INTO spider_queue" in sql
    assert "instagram_spider_queue" not in sql
    assert not any("instagram_spider_queue" in s for s in executed)
    assert "'instagram'" in sql
    assert "'pending'" in sql
    assert "'proximity'" in sql
    assert "'instagram:' || platform_user_id" in sql


def test_proximity_instagram_seed_requires_numeric_platform_user_id():
    sql, _ = _instagram_seed_sql()

    # Predicate must guard the FINAL select (FROM users), not only the source
    # account_id, because platform_user_id can be synthesized from profiles.
    final_select = sql.split("INSERT INTO spider_queue", 1)[1]
    assert "FROM users" in final_select
    assert "WHERE platform_user_id ~ '^[0-9]+$'" in final_select


def test_proximity_instagram_seed_duplicate_is_noop():
    sql, _ = _instagram_seed_sql()

    assert "ON CONFLICT (platform, idempotency_key) DO NOTHING" in sql
