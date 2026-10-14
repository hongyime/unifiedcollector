from src.core.reconciler import _canonical_rescrape_permalink


def test_reconciler_does_not_queue_cdn_media_urls_as_detail_targets():
    metadata = {"source_url": "https://scontent.cdninstagram.com/o1/video.mp4"}
    assert _canonical_rescrape_permalink("instagram", "hashed_content_id", metadata) is None
    assert _canonical_rescrape_permalink("tiktok", "video_hash", metadata, "alice") is None


def test_reconciler_derives_canonical_instagram_permalink_from_numeric_id():
    assert _canonical_rescrape_permalink("instagram", "3953923014190275071") == (
        "https://www.instagram.com/p/DbfKAhaDZn_/"
    )


def test_reconciler_accepts_only_first_party_tiktok_detail_permalink():
    metadata = {"page_url": "https://www.tiktok.com/@alice/video/7012345678901234567"}
    assert _canonical_rescrape_permalink("tiktok", "video_hash", metadata, "alice") == (
        "https://www.tiktok.com/@alice/video/7012345678901234567"
    )
    assert _canonical_rescrape_permalink(
        "tiktok", "video_hash", {"page_url": "https://www.tiktok.com/@alice"}, "alice"
    ) is None
