from src.core.collection_surface import (
    COLLECTION_SURFACES,
    bounded_capture_limit,
    classify_collection_error,
    normalize_collection_surface,
)


def test_normalizes_surface_aliases_and_common_paths():
    assert normalize_collection_surface("For You") == "for_you"
    assert normalize_collection_surface("home/following") == "following"
    assert normalize_collection_surface(url="https://www.instagram.com/explore/") == "explore"
    assert normalize_collection_surface(url="https://www.threads.net/@alice") == "profile"
    assert normalize_collection_surface("unknown", context="topic") == "topic"
    assert normalize_collection_surface("not-a-known-surface") == "unknown"


def test_surface_vocabulary_is_unique_and_has_required_labels():
    assert len(COLLECTION_SURFACES) == len(set(COLLECTION_SURFACES))
    assert {
        "profile", "following", "for_you", "explore", "topic", "home",
        "search", "message_stream", "unknown",
    } <= set(COLLECTION_SURFACES)


def test_capture_limit_uses_default_and_hard_bounds():
    assert bounded_capture_limit(None, default=2, maximum=5, minimum=1) == 2
    assert bounded_capture_limit("999", default=2, maximum=5, minimum=1) == 5
    assert bounded_capture_limit("-4", default=2, maximum=5, minimum=1) == 1
    assert bounded_capture_limit("bad", default=2, maximum=5, minimum=1) == 2


def test_collection_error_classification_is_stable_and_coarse():
    assert classify_collection_error("HTTP 429") == "rate_limit"
    assert classify_collection_error("LoginRequired") == "auth"
    assert classify_collection_error("HTTP 403 private") == "visibility"
    assert classify_collection_error("ReadTimeout") == "timeout"
    assert classify_collection_error("JSONDecodeError") == "parse"
    assert classify_collection_error("PostgresWriteError") == "persistence"
    assert classify_collection_error("ConnectError") == "network"
    assert classify_collection_error("") == "unknown"
