from __future__ import annotations

import pytest

from src.core import verified_exclusion


class _Conn:
    async def fetch(self, _query, *_args):
        return []

    async def fetchval(self, query, *args):
        if "to_regclass" in query:
            return None if args[0].endswith("x_profiles") else args[0]
        assert "COUNT" in query
        return 2


@pytest.mark.asyncio
async def test_manifest_only_counts_existing_explicit_profile_tables():
    manifest = await verified_exclusion.build_verified_exclusion_manifest(_Conn())

    assert manifest["destructive"] is False
    assert {row["source"] for row in manifest["sources"]} == {
        "instagram", "tiktok", "pinterest", "snapchat", "youtube",
    }
    assert manifest["profile_candidates"] == 10
    assert all("dependencies" in row for row in manifest["sources"])
    assert all(row["candidate_identities"] == [] for row in manifest["sources"])


@pytest.mark.asyncio
async def test_media_cleanup_queues_paths_without_scanning_every_media_row():
    class Conn:
        def __init__(self):
            self.query = ""

        async def fetchval(self, *_args):
            return 1

        async def execute(self, query, *_args):
            self.query = query

    conn = Conn()
    assert await verified_exclusion._record_media_cleanup(conn, "receipt") == 1
    assert "'pending'" in conn.query
    assert "media_items" not in conn.query


def test_purge_uses_frozen_explicit_platform_ids_and_handles_only():
    ids, handles = verified_exclusion._candidate_values("instagram", [
        {"platform_user_id": "42", "username": "Example"},
        {"platform_user_id": "42", "username": "EXAMPLE"},
    ])

    assert ids == ["42"]
    assert handles == ["example"]


def test_purge_rejects_sources_without_a_destructive_adapter():
    with pytest.raises(ValueError, match="not implemented"):
        verified_exclusion._candidate_values("pinterest", [{"username": "example"}])
