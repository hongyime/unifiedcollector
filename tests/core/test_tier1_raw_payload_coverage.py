from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_native_tier1_raw_payload_surfaces_have_rebuild_targets():
    expectations = {
        "src/collectors/telegram/__init__.py": [
            '"telegram_chats"',
            '"telegram_users"',
            '"telegram_messages"',
            '"raw_payload_kind": "chat"',
            '"raw_payload_kind": "message"',
            '"raw_payload_kind": "message_edit" if is_edit else "message"',
            '"raw_payload_kind": "user_profile"',
        ],
        "src/collectors/whatsapp/__init__.py": [
            '"whatsapp_lid_map"',
            '"whatsapp_users"',
            '"whatsapp_messages"',
            '"raw_payload_kind": "contact"',
            '"raw_payload_kind": "message"',
            '"raw_payload_kind": "message_deletion"',
        ],
        "src/collectors/beeper/__init__.py": [
            '"beeper_shadow_accounts"',
            '"beeper_shadow_chats"',
            '"beeper_shadow_participants"',
            '"beeper_shadow_messages"',
            '"raw_payload_kind": "account"',
            '"raw_payload_kind": "chat"',
            '"raw_payload_kind": "participants"',
            '"raw_payload_kind": "message"',
        ],
        "src/collectors/instagram/__init__.py": [
            '"instagram_profiles"',
            '"instagram_posts"',
            '"payload_type": "instagram_httpx_profile_response"',
            '"payload_type": "instagram_graphql_posts_page"',
            '"payload_type": "instagram_playwright_posts_window"',
            '"payload_type": "instagram_playwright_reels_window"',
        ],
        "src/collectors/strava/__init__.py": [
            '"strava_activities"',
            '"strava_gps_streams"',
            '"payload_type": "strava_activity"',
            '"payload_type": "strava_athlete_activities_page"',
            '"payload_type": "strava_web_gps_stream"',
            '"payload_type": "strava_api_gps_stream"',
        ],
    }
    for path, needles in expectations.items():
        text = _read(path)
        missing = [needle for needle in needles if needle not in text]
        assert not missing, f"{path} missing raw archive evidence: {missing}"


def test_browser_tier1_raw_capture_surfaces_have_rebuild_targets():
    text = _read("src/bridges/ig_ingest.py")
    for endpoint in (
        '"profile"',
        '"posts"',
        '"comments"',
        '"dms"',
        '"dm_probe"',
        '"dm_sample"',
        '"dm_frame"',
        '"dm_decoded"',
        '"strava_streams"',
    ):
        assert endpoint in text
    for target in (
        '"instagram_profiles"',
        '"instagram_posts"',
        '"instagram_comments"',
        '"instagram_dm_thread"',
        '"instagram_dm"',
        '"dm_probe_log"',
        '"strava_activities"',
        '"strava_gps_streams"',
    ):
        assert target in text
    assert '"dm_frame": _DM_PROBE_TARGET_TABLES' in text
