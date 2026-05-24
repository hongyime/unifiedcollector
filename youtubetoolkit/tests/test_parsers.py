import pytest

from src.video_processor import extract_video_id, resolve_cookie_choice
from scripts.subscription_processor import SubscriptionProcessor

def test_extract_video_id_valid_urls():
    assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s") == "dQw4w9WgXcQ"

def test_extract_video_id_invalid_urls():
    assert extract_video_id("https://google.com") is None
    assert extract_video_id("not a url") is None
    assert extract_video_id("https://youtube.com/playlist?list=PLsomething") is None

def test_parse_duration_valid():
    # 1 hour, 2 minutes, 10 seconds = 3600 + 120 + 10 = 3730
    assert SubscriptionProcessor.parse_duration("PT1H2M10S") == 3730
    # 5 minutes, 33 seconds = 300 + 33 = 333
    assert SubscriptionProcessor.parse_duration("PT5M33S") == 333
    # 24 seconds = 24
    assert SubscriptionProcessor.parse_duration("PT24S") == 24
    # 1 hour = 3600
    assert SubscriptionProcessor.parse_duration("PT1H") == 3600

def test_parse_duration_invalid():
    assert SubscriptionProcessor.parse_duration("INVALID") == 0
    assert SubscriptionProcessor.parse_duration("") == 0
    assert SubscriptionProcessor.parse_duration(None) == 0

def test_resolve_cookie_choice_respects_auth_selection():
    assert resolve_cookie_choice(False, None) is None
    assert resolve_cookie_choice(False, "auto") is None
    assert resolve_cookie_choice(True, None) == "auto"
    cookie_tuple = ("chrome", None, "Chrome (Default Profile)")
    assert resolve_cookie_choice(True, cookie_tuple) == cookie_tuple
