from pathlib import Path

from src.core.collection_priority import (
    CollectionPhase,
    PHASE_ORDER,
    classify_work,
    proximity_priority_score_sql,
)


ROOT = Path(__file__).resolve().parents[2]


def test_worker_target_loader_uses_shared_priority_sql():
    text = (ROOT / "src/worker/__init__.py").read_text(encoding="utf-8")
    assert "from src.core.collection_priority import proximity_priority_score_sql" in text
    assert "proximity_priority_score_sql(\"MIN(ap.tier)\")" in text
    assert "{proximity_score_sql} DESC" in text


def test_phase_order_matches_collector_prd():
    assert PHASE_ORDER == (
        CollectionPhase.TIER1_FRESHNESS,
        CollectionPhase.RICH_MEDIA,
        CollectionPhase.LOW_RATE_LIMIT,
        CollectionPhase.HISTORICAL_BACKFILL,
        CollectionPhase.BROAD_DISCOVERY,
    )
    assert all(a > b for a, b in zip(PHASE_ORDER, PHASE_ORDER[1:]))


def test_classify_work_prioritizes_tier1_and_broad_discovery_last():
    assert classify_work(work_kind="spider", proximity_tier=1) == CollectionPhase.TIER1_FRESHNESS
    assert classify_work(work_kind="media_download") == CollectionPhase.RICH_MEDIA
    assert classify_work(work_kind="safe_api") == CollectionPhase.LOW_RATE_LIMIT
    assert classify_work(work_kind="historical_backfill") == CollectionPhase.HISTORICAL_BACKFILL
    assert classify_work(work_kind="broad_discovery") == CollectionPhase.BROAD_DISCOVERY


def test_proximity_priority_sql_uses_shared_scores():
    sql = proximity_priority_score_sql("MIN(ap.tier)")
    assert f"THEN {int(CollectionPhase.TIER1_FRESHNESS)}" in sql
    assert f"THEN {int(CollectionPhase.LOW_RATE_LIMIT)}" in sql
    assert f"ELSE {int(CollectionPhase.HISTORICAL_BACKFILL)}" in sql
