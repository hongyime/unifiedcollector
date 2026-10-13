from src.core.collection_surface import COLLECTION_SURFACES, normalize_collection_surface


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
