from src.core.spider_queue_compat import (
    LEGACY_QUEUE_SPECS,
    normalize_legacy_status,
)


def test_legacy_queue_status_aliases_are_normalized():
    assert normalize_legacy_status("pending") == "pending"
    assert normalize_legacy_status("active") == "pending"
    assert normalize_legacy_status("processing") == "in_progress"
    assert normalize_legacy_status("done") == "completed"
    assert normalize_legacy_status("unresolvable") == "failed"
    assert normalize_legacy_status("operator_paused") == "unknown"


def test_legacy_queue_specs_cover_current_dedicated_queues():
    assert {
        "instagram", "instagram_targets", "tiktok", "github", "strava",
        "lemon8", "youtube", "telegram",
    } == set(LEGACY_QUEUE_SPECS)
    assert LEGACY_QUEUE_SPECS["github"].target_type_field == "target_type"
    assert not LEGACY_QUEUE_SPECS["instagram_targets"].record_id_is_uuid
