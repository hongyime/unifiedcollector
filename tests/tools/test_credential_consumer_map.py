from __future__ import annotations

import copy

from scripts.verify_credential_consumer_map import candidate_credentials, load_map, verify_map


def test_credential_map_covers_tracked_compose_credentials_without_values() -> None:
    assert verify_map(load_map()) == []


def test_unmapped_credential_is_rejected() -> None:
    candidates = candidate_credentials()
    candidates["NEW_PROVIDER_API_TOKEN"] = {"docker/env/example.env.example"}
    assert "unmapped credential: NEW_PROVIDER_API_TOKEN" in verify_map(load_map(), candidates=candidates)


def test_missing_row_is_rejected() -> None:
    document = copy.deepcopy(load_map())
    document["credentials"] = [
        row for row in document["credentials"] if row["name"] != "REDIS_PASSWORD"
    ]
    assert "unmapped credential: REDIS_PASSWORD" in verify_map(document)
