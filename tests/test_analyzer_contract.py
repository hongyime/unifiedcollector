"""Fail-closed recovery contract between the restored collector DB and Analyzer.

This does not start either service.  It checks the immutable, aggregate-only
Task 11 recheck against the Compose mount shape that Analyzer consumes.  A
passing test means the known incompatibility is recorded and promotion remains
blocked; it is not a claim that the missing media exists.
"""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROPOSAL = ROOT / ".omo/evidence/collector-production-recovery/task-11-reconciliation-proposal.json"
COMPOSE = ROOT / "docker/docker-compose.yml"


def test_schema_media_smoke_records_restored_layout_as_incompatible() -> None:
    """The restored schema has media rows, but the current mounted vault lacks them.

    The analyzer's established contract rebases both `/media/...` and
    `/vault/media/...` below the collector root's `media/` mount.  The actual
    Task 11 deterministic recheck therefore has to block promotion when none
    of its selected rows resolve there.  This assertion prevents a copied-tree
    receipt or a Compose-root change from being misreported as compatibility.
    """
    proposal = json.loads(PROPOSAL.read_text(encoding="utf-8"))
    media = proposal["media_reconciliation"]
    recheck = media["sample"]["post_copy_destination_recheck"]

    assert proposal["database_recovery_point"]["database"] == "unifiedcollector"
    assert media["database_rows"] > 0
    assert media["database_file_path_classes"] == {
        "/media/<source>/...": 572622,
        "/vault/media/...": 492150,
    }
    assert recheck["root"] == "/media/sg-ultra/unifiedcollector"
    assert media["sample"]["rows"] == 100
    assert recheck["files_present"] == 0
    assert recheck["missing"] == media["sample"]["rows"]
    assert proposal["authority"]["promotion_allowed"] is False

    compose = COMPOSE.read_text(encoding="utf-8")
    assert "${UNIFIEDCOLLECTOR_HOST_ROOT:-/media/sg-ultra/unifiedcollector}/media:/media" in compose
    assert "${UNIFIEDCOLLECTOR_HOST_ROOT:-/media/sg-ultra/unifiedcollector}:/vault" in compose
