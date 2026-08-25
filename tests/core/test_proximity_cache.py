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
