"""Unit tests for src.core.profile_analyzer."""
import pytest

from src.core.profile_analyzer import (
    ProfileAnalyzer,
    analyze_profile_image,
    TIERS,
)


@pytest.fixture
def sample_profiles():
    return [
        {"username": "alice", "followers_count": 12_000, "following_count": 200,
         "is_verified": True, "is_private": False},
        {"username": "bob", "followers_count": 800, "following_count": 1_500,
         "is_verified": False, "is_private": False},
        {"username": "carol", "followers_count": 2_500_000, "following_count": 50,
         "is_verified": True, "is_private": False},
        {"username": "dan", "followers_count": 0, "following_count": 0,
         "is_private": True},
    ]


def test_analyze_profiles_shape(sample_profiles):
    a = ProfileAnalyzer()
    stats = a.analyze_profiles(sample_profiles)

    assert stats["total_profiles"] == 4
    assert stats["public_profiles"] == 3
    assert stats["private_profiles"] == 1
    assert stats["verified_profiles"] == 2
    assert "analysis_timestamp" in stats and "analysis_date" in stats
    # Tiers schema present even when zeroed
    assert set(stats["influencer_tiers"].keys()) == {n for n, _, _ in TIERS}


def test_influencer_tiers(sample_profiles):
    a = ProfileAnalyzer()
    stats = a.analyze_profiles(sample_profiles)
    tiers = stats["influencer_tiers"]
    # alice -> small, carol -> celebrities, others -> none
    assert tiers["small_influencers"] == 1
    assert tiers["celebrities"] == 1
    assert tiers["micro_influencers"] == 0


def test_top_lists_sorted_desc(sample_profiles):
    a = ProfileAnalyzer()
    stats = a.analyze_profiles(sample_profiles)
    fol = [p["followers_count"] for p in stats["top_followers"]]
    assert fol == sorted(fol, reverse=True)
    assert stats["top_followers"][0]["username"] == "carol"


def test_high_engagement_potential(sample_profiles):
    a = ProfileAnalyzer()
    stats = a.analyze_profiles(sample_profiles)
    high = stats["high_engagement_potential"]
    names = {h["username"] for h in high}
    # alice (12k/200) and carol (2.5M/50) both qualify
    assert "alice" in names and "carol" in names


def test_empty_input_returns_zeros():
    a = ProfileAnalyzer()
    stats = a.analyze_profiles([])
    assert stats["total_profiles"] == 0
    assert stats["avg_followers"] == 0
    assert stats["high_engagement_potential"] == []
    assert "analysis_timestamp" in stats


def test_get_influential_users(sample_profiles):
    a = ProfileAnalyzer()
    out = a.get_influential_users(sample_profiles, min_followers=10_000)
    names = {p["username"] for p in out}
    assert names == {"alice", "carol"}


def test_handles_missing_keys():
    a = ProfileAnalyzer()
    profiles = [{"username": "x"}, {"username": "y", "followers_count": None}]
    stats = a.analyze_profiles(profiles)
    assert stats["total_profiles"] == 2
    assert stats["avg_followers"] == 0


# ---- image hook --------------------------------------------------------

def test_analyze_profile_image_jpeg():
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 50
    out = analyze_profile_image(jpeg)
    assert out is not None
    assert out["format"] == "jpeg"
    assert out["ok"] is True
    assert out["size_bytes"] == len(jpeg)


def test_analyze_profile_image_png():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
    out = analyze_profile_image(png)
    assert out["format"] == "png"


def test_analyze_profile_image_none_on_empty():
    assert analyze_profile_image(None) is None
    assert analyze_profile_image(b"") is None
    assert analyze_profile_image(b"ab") is None  # too short


def test_analyze_profile_image_unknown_format():
    out = analyze_profile_image(b"not-an-image-just-some-bytes")
    assert out is not None
    assert out["format"] == "unknown"
    assert out["ok"] is False
