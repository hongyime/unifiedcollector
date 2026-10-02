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
