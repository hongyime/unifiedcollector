"""Regression contract for fenced legacy spider-queue claims."""
from pathlib import Path

import pytest


_ROOT = Path(__file__).parents[2]
_MIGRATION = _ROOT / "src/db/migrations/20261117_add_legacy_spider_queue_leases.sql"


@pytest.mark.parametrize(
    ("collector", "table"),
    [
        ("youtube", "youtube_spider_queue"),
        ("github", "github_spider_queue"),
        ("strava", "strava_spider_queue"),
        ("lemon8", "lemon8_spider_queue"),
    ],
)
def test_legacy_spider_queue_claims_use_expiry_and_fencing(collector, table):
    source = (_ROOT / f"src/collectors/{collector}/__init__.py").read_text(encoding="utf-8")
    migration = _MIGRATION.read_text(encoding="utf-8")

    assert f"ALTER TABLE {table}" in migration
    assert "lease_expires_at TIMESTAMPTZ" in migration
    assert "claim_token UUID" in migration
    assert "lease_expires_at <= NOW()" in source
    assert "FOR UPDATE SKIP LOCKED" in source
    assert "AND claim_token = $2" in source
