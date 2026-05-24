import pytest
import sys
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add parent directory and src folder to sys.path
_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_root / 'src'))
sys.path.insert(0, str(_root))

import scraper as scraper_module
from scraper import Lemon8Scraper

@pytest.fixture
def scraper():
    return Lemon8Scraper()

def test_clean_media_url(scraper):
    url = "https://p16-va.lemon8cdn.com/tos-alisg-i-sdweummd6v-sg/04884c7e63f545468725916309859f51~tplv-sdweummd6v-image.webp?from=123456&s=640x640"
    expected = url
    assert scraper._clean_media_url(url) == expected

def test_is_valid_media_url(scraper):
    # Valid media
    assert scraper._is_valid_media_url("https://example.com/video.mp4") == True
    assert scraper._is_valid_media_url("https://example.com/image.jpg") == True
    assert scraper._is_valid_media_url("https://p16-va.lemon8cdn.com/tos-alisg-i-sdweummd6v-sg/foo") == True
    
    # Invalid media
    assert scraper._is_valid_media_url("https://example.com/script.js") == False
    assert scraper._is_valid_media_url("https://example.com/style.css") == False
    assert scraper._is_valid_media_url("not_a_url") == False


def test_is_relevant_post_media_url_filters_avatar_and_share_card_noise(scraper):
    assert scraper._is_relevant_post_media_url(
        "https://example.com/user-avatar/test_user_120x120.jpg?source=feed_user"
    ) is False
    assert scraper._is_relevant_post_media_url(
        "https://example.com/image.webp?source=share_card"
    ) is False
    assert scraper._is_relevant_post_media_url(
        "https://example.com/post_content.webp"
    ) is True


def test_scraper_loads_cookie_file_and_extracts_tt_webid(tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        """
# Netscape HTTP Cookie File
.lemon8-app.com\tTRUE\t/\tFALSE\t1893456000\ttt_webid\twebid_123456789
.lemon8-app.com\tTRUE\t/\tFALSE\t1893456000\tother_cookie\tvalue123
""".strip(),
        encoding="utf-8",
    )

    scraper = Lemon8Scraper(cookie_file=str(cookie_file))

    assert scraper.loaded_cookie_summary["cookie_count"] >= 2
    assert scraper.loaded_cookie_summary["tt_webid"] == "webid_123456789"
    assert scraper.session.cookies.get("tt_webid") == "webid_123456789"


def test_apply_rotating_headers_sets_referer_and_random_profile(monkeypatch, scraper):
    monkeypatch.setattr(scraper_module.random, "choice", lambda seq: seq[0])

    scraper._apply_rotating_headers(endpoint_kind="api", referer="https://example.com/referer")

    assert "Safari/605.1.15" in scraper.session.headers["User-Agent"]
    assert scraper.session.headers["Accept"] == "application/json, text/plain, */*"
    assert scraper.session.headers["Referer"] == "https://example.com/referer"


def test_rotating_header_profiles_favor_safari_before_chrome(scraper):
    profiles = scraper.ROTATING_HEADER_PROFILES

    assert "Safari" in profiles[0]["User-Agent"]
    assert "Safari" in profiles[1]["User-Agent"]
    assert "Safari" in profiles[2]["User-Agent"]
    assert "Chrome/124.0.0.0" in profiles[3]["User-Agent"]
    assert "Chrome/124.0.0.0" in profiles[4]["User-Agent"]

def test_is_small_image(scraper):
    # Small indicators
    assert scraper._is_small_image("https://example.com/avatar_150x150.jpg") == True
    assert scraper._is_small_image("https://example.com/profile_pic.png") == True
    assert scraper._is_small_image("https://example.com/icon.ico") == True
    
    # Dimensions patterns
    assert scraper._is_small_image("https://example.com/image:150:150.webp") == True
    assert scraper._is_small_image("https://example.com/image?width=200") == True
    assert scraper._is_small_image("https://example.com/image_64x64.jpg") == True
    
    # Large images
    assert scraper._is_small_image("https://example.com/post_large_image.jpg") == False
    assert scraper._is_small_image("https://example.com/image:1080:1080.webp") == False
    assert scraper._is_small_image("https://example.com/image_1280x720.jpg") == False
    assert scraper._is_small_image("https://example.com/image?width=1080") == False
    assert scraper._is_small_image("https://example.com/photo~tplv-sdweummd6v-shrink:640:0:q50.webp") == False

def test_find_key_in_json(scraper):
    data = {
        "level1": {
            "level2": {
                "target_key": "found_me"
            }
        },
        "list": [
            {"other_key": 1},
            {"another_target": "also_found"}
        ]
    }
    assert scraper._find_key_in_json(data, ["target_key"]) == "found_me"
    assert scraper._find_key_in_json(data, ["another_target"]) == "also_found"
    assert scraper._find_key_in_json(data, ["missing"]) == None


def test_extract_hashtags_from_text(scraper):
    text = "Loving #Singapore #foodie and #CafeHopping today!"

    hashtags = scraper._extract_hashtags_from_text(text)

    assert hashtags == {"singapore", "foodie", "cafehopping"}


def test_extract_media_items_from_pylemon8_items_keeps_username_and_profile_photo(scraper):
    items = [
        {
            "authorInfo": {
                "uniqueId": "Test_User",
                "avatarLarger": {
                    "urlList": ["https://example.com/user-avatar/test_user_120x120.jpg"]
                },
            },
            "imageResource": [
                {"urlList": ["https://example.com/post-small.webp", "https://example.com/post-large.webp"]}
            ],
        }
    ]

    media_items = scraper._extract_media_items_from_pylemon8_items(
        items,
        include_profile_images=True,
    )

    assert media_items[0]["url"] == "https://example.com/post-large.webp"
    assert media_items[0]["username"] == "test_user"
    assert media_items[0]["is_profile_photo"] is False
    assert media_items[1]["url"] == "https://example.com/user-avatar/test_user_120x120.jpg"
    assert media_items[1]["username"] == "test_user"
    assert media_items[1]["is_profile_photo"] is True


def test_extract_media_items_from_pylemon8_feed_schema_uses_link_name_and_image_list(scraper):
    items = [
        {
            "author": {
                "linkName": "FeedAuthor",
                "avatar": "https://example.com/user-avatar/feed_author_120x120.jpg",
            },
            "imageList": [
                {"url": "https://example.com/post_card.webp"},
            ],
            "largeImage": {
                "url": "https://example.com/post_large.webp",
            },
            "articleClass": "Gallery",
        }
    ]

    media_items = scraper._extract_media_items_from_pylemon8_items(
        items,
        include_profile_images=False,
    )

    assert media_items == [
        {
            "url": "https://example.com/post_card.webp",
            "username": "feedauthor",
            "is_profile_photo": False,
            "media_type": "image",
        }
    ]


def test_extract_media_items_from_pylemon8_video_card_falls_back_to_large_image(scraper):
    items = [
        {
            "author": {
                "linkName": "video_author",
                "avatar": "https://example.com/user-avatar/video_author_120x120.jpg",
            },
            "imageList": [],
            "largeImage": {
                "url": "https://example.com/video_cover.webp",
            },
            "articleClass": "Video",
        }
    ]

    media_items = scraper._extract_media_items_from_pylemon8_items(
        items,
        include_profile_images=False,
    )

    assert media_items == [
        {
            "url": "https://example.com/video_cover.webp",
            "username": "video_author",
            "is_profile_photo": False,
            "media_type": "image",
        }
    ]


def test_extract_users_from_pylemon8_items_supports_author_link_name(scraper):
    items = [
        {
            "author": {
                "linkName": "FeedAuthor",
                "userId": "7123456789012345678",
            },
            "title": "No mentions here",
        }
    ]

    users = scraper._extract_users_from_pylemon8_items(items)

    assert "feedauthor" in users


def test_extract_media_items_from_dom_associates_username(scraper):
    html = """
    <html>
        <body>
            <div class="feed-card">
                <a href="/@card_author">Author</a>
                <img src="https://example.com/card_image.webp" />
            </div>
        </body>
    </html>
    """

    media_items = scraper._extract_media_items_from_dom(html)

    assert media_items == [{
        "url": "https://example.com/card_image.webp",
        "username": "card_author",
        "is_profile_photo": False,
        "media_type": "image",
    }]


def test_extract_media_items_from_feed_cards_can_include_profile_photo(scraper):
    html = """
    <html>
        <body>
            <a class="article_card" href="/@card_author/123?region=sg">
                <img src="https://example.com/feed_large.webp" />
                <img src="https://example.com/user-avatar/card_author_120x120.jpg" />
            </a>
        </body>
    </html>
    """

    media_items = scraper._extract_media_items_from_feed_cards(
        html,
        include_profile_images=True,
    )

    assert media_items == [
        {
            "url": "https://example.com/feed_large.webp",
            "username": "card_author",
            "is_profile_photo": False,
            "media_type": "image",
        },
        {
            "url": "https://example.com/user-avatar/card_author_120x120.jpg",
            "username": "card_author",
            "is_profile_photo": True,
            "media_type": "image",
        },
    ]


def test_deduplicate_media_items_merges_username_metadata(scraper):
    media_items = [
        {
            "url": "https://example.com/feed_large.webp",
            "username": None,
            "is_profile_photo": False,
            "media_type": "image",
        },
        {
            "url": "https://example.com/feed_large.webp",
            "username": "merged_author",
            "is_profile_photo": False,
            "media_type": "image",
        },
    ]

    deduplicated = scraper._deduplicate_media_items(media_items)

    assert deduplicated == [{
        "url": "https://example.com/feed_large.webp",
        "username": "merged_author",
        "is_profile_photo": False,
        "media_type": "image",
    }]


@patch('requests.Session.get')
def test_scrape_for_you_feed_returns_media_items_with_usernames(mock_get, scraper, monkeypatch):
    monkeypatch.setattr(scraper_module, "INCLUDE_PROFILE_IMAGES_IN_FEED", False)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = """
    <html>
        <body>
            <script type="application/json">
                {
                    "data": {
                        "items": [
                            {
                                "authorInfo": {
                                    "uniqueId": "feed_author",
                                    "avatarLarger": {
                                        "urlList": ["https://example.com/user-avatar/feed_author_120x120.jpg"]
                                    }
                                },
                                "imageResource": [
                                    {
                                        "urlList": [
                                            "https://example.com/feed_small.webp",
                                            "https://example.com/feed_large.webp"
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                }
            </script>
        </body>
    </html>
    """
    mock_get.return_value = mock_response

    result = scraper.scrape_for_you_feed(pages=1, use_api=False)

    assert result['media_urls'] == ["https://example.com/feed_large.webp"]
    assert result['media_items'][0]['username'] == "feed_author"
    assert result['media_items'][0]['is_profile_photo'] is False


@patch('requests.Session.get')
def test_scrape_for_you_feed_can_merge_dom_username_when_json_author_missing(mock_get, scraper, monkeypatch):
    monkeypatch.setattr(scraper_module, "INCLUDE_PROFILE_IMAGES_IN_FEED", False)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = """
    <html>
        <body>
            <script type="application/json">
                {
                    "data": {
                        "items": [
                            {
                                "imageResource": [
                                    {
                                        "urlList": [
                                            "https://example.com/feed_large.webp"
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                }
            </script>
            <div class="feed-card">
                <a href="/@dom_author">Author</a>
                <img src="https://example.com/feed_large.webp" />
            </div>
        </body>
    </html>
    """
    mock_get.return_value = mock_response

    result = scraper.scrape_for_you_feed(pages=1, use_api=False)

    assert result['media_items'][0]['username'] == "dom_author"


@patch('requests.Session.get')
def test_scrape_for_you_feed_can_include_profile_photos_when_requested(mock_get, scraper, monkeypatch):
    monkeypatch.setattr(scraper_module, "INCLUDE_PROFILE_IMAGES_IN_FEED", False)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = """
    <html>
        <body>
            <script type="application/json">
                {
                    "data": {
                        "items": [
                            {
                                "authorInfo": {
                                    "uniqueId": "feed_author",
                                    "avatarLarger": {
                                        "urlList": ["https://example.com/user-avatar/feed_author_120x120.jpg"]
                                    }
                                },
                                "imageResource": [
                                    {
                                        "urlList": [
                                            "https://example.com/feed_large.webp"
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                }
            </script>
        </body>
    </html>
    """
    mock_get.return_value = mock_response

    result = scraper.scrape_for_you_feed(
        pages=1,
        use_api=False,
        include_profile_images=True,
    )

    profile_items = [item for item in result["media_items"] if item["is_profile_photo"]]
    assert len(profile_items) == 1
    assert profile_items[0]["username"] == "feed_author"
    assert profile_items[0]["url"] == "https://example.com/user-avatar/feed_author_120x120.jpg"


@patch('requests.Session.get')
def test_scrape_user_profile_can_include_profile_photo(mock_get, scraper, monkeypatch):
    monkeypatch.setattr(scraper_module, "INCLUDE_PROFILE_IMAGES_IN_USER_SCRAPES", True)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = """
    <html>
        <body>
            <script type="application/json">
                {
                    "data": {
                        "items": [
                            {
                                "authorInfo": {
                                    "uniqueId": "profile_owner",
                                    "avatarLarger": {
                                        "urlList": ["https://example.com/user-avatar/profile_owner_120x120.jpg"]
                                    }
                                },
                                "imageResource": [
                                    {
                                        "urlList": [
                                            "https://example.com/profile_post.webp"
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                }
            </script>
        </body>
    </html>
    """
    mock_get.return_value = mock_response

    result = scraper.scrape_user_profile("profile_owner", use_api=False)
    profile_items = [item for item in result["media_items"] if item["is_profile_photo"]]

    assert any(item["url"] == "https://example.com/profile_post.webp" for item in result["media_items"])
    assert len(profile_items) == 1
    assert profile_items[0]["username"] == "profile_owner"


@patch('requests.Session.get')
def test_scrape_user_profile_extracts_hashtags_from_caption(mock_get, scraper, monkeypatch):
    monkeypatch.setattr(scraper_module, "INCLUDE_PROFILE_IMAGES_IN_USER_SCRAPES", False)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = """
    <html>
        <body>
            <script type="application/json">
                {
                    "data": {
                        "items": [
                            {
                                "authorInfo": {"uniqueId": "hash_user"},
                                "title": "Trying #Singapore #FoodSpots tonight",
                                "imageResource": [
                                    {"urlList": ["https://example.com/hash_post.webp"]}
                                ]
                            }
                        ]
                    }
                }
            </script>
        </body>
    </html>
    """
    mock_get.return_value = mock_response

    result = scraper.scrape_user_profile("hash_user", use_api=False)

    assert "singapore" in result["hashtags"]
    assert "foodspots" in result["hashtags"]


@patch('requests.Session.get')
def test_scrape_tag_topic_returns_media_items_with_username(mock_get, scraper):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = """
    <html>
        <body>
            <script type="application/json">
                {
                    "data": {
                        "items": [
                            {
                                "author": {
                                    "linkName": "tag_author"
                                },
                                "imageList": [
                                    {
                                        "url": "https://example.com/tag_post.webp"
                                    }
                                ]
                            }
                        ]
                    }
                }
            </script>
            <a href="/topic/987654321">Related Tag</a>
        </body>
    </html>
    """
    mock_get.return_value = mock_response

    result = scraper.scrape_tag_topic("123456789")

    assert result["media_urls"] == ["https://example.com/tag_post.webp"]
    assert result["media_items"][0]["username"] == "tag_author"
    assert result["media_items"][0]["is_profile_photo"] is False


@patch('requests.Session.get')
def test_scrape_tag_topic_can_merge_dom_username_when_json_author_missing(mock_get, scraper):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = """
    <html>
        <body>
            <script type="application/json">
                {
                    "data": {
                        "items": [
                            {
                                "imageList": [
                                    {
                                        "url": "https://example.com/tag_dom_media.webp"
                                    }
                                ]
                            }
                        ]
                    }
                }
            </script>
            <div class="tag-card">
                <a href="/@dom_tag_author">Author</a>
                <img src="https://example.com/tag_dom_media.webp" />
            </div>
        </body>
    </html>
    """
    mock_get.return_value = mock_response

    result = scraper.scrape_tag_topic("123456789")

    assert result["media_items"][0]["username"] == "dom_tag_author"


@patch('requests.Session.get')
@patch('time.sleep', return_value=None)
def test_scrape_tag_topic_supports_multiple_pages_with_cursor(_mock_sleep, mock_get, scraper):
    first_page = MagicMock()
    first_page.status_code = 200
    first_page.text = """
    <html>
        <body>
            <script type="application/json">
                {
                    "cursor": "next_cursor_123",
                    "items": [
                        {
                            "author": {"linkName": "tag_author_one"},
                            "imageList": [{"url": "https://example.com/tag_page1.webp"}]
                        }
                    ]
                }
            </script>
        </body>
    </html>
    """

    second_page = MagicMock()
    second_page.status_code = 200
    second_page.text = """
    <html>
        <body>
            <script type="application/json">
                {
                    "items": [
                        {
                            "author": {"linkName": "tag_author_two"},
                            "imageList": [{"url": "https://example.com/tag_page2.webp"}]
                        }
                    ]
                }
            </script>
        </body>
    </html>
    """

    mock_get.side_effect = [first_page, second_page]

    result = scraper.scrape_tag_topic("123456789", pages=2)

    assert result["pages_requested"] == 2
    assert result["pages_scraped"] == 2
    assert "https://example.com/tag_page1.webp" in result["media_urls"]
    assert "https://example.com/tag_page2.webp" in result["media_urls"]
    assert "next_cursor_123" in mock_get.call_args_list[1].args[0]


@patch('requests.Session.get')
@patch('time.sleep', return_value=None)
def test_scrape_tag_topic_stops_early_when_no_next_cursor(_mock_sleep, mock_get, scraper):
    page = MagicMock()
    page.status_code = 200
    page.text = """
    <html>
        <body>
            <script type="application/json">
                {
                    "items": [
                        {
                            "author": {"linkName": "single_page_author"},
                            "imageList": [{"url": "https://example.com/only_page.webp"}]
                        }
                    ]
                }
            </script>
        </body>
    </html>
    """
    mock_get.return_value = page

    result = scraper.scrape_tag_topic("123456789", pages=5)

    assert result["pages_requested"] == 5
    assert result["pages_scraped"] == 1
    assert mock_get.call_count == 1
    assert result["media_urls"] == ["https://example.com/only_page.webp"]


@patch('requests.Session.get')
@patch('time.sleep', return_value=None)
def test_scrape_tag_topic_keyword_uses_discover_fallback_when_topic_empty(_mock_sleep, mock_get, scraper):
    topic_empty_page = MagicMock()
    topic_empty_page.status_code = 200
    topic_empty_page.text = """
    <html>
        <body>
            <h2>Most recent</h2>
            <div>No content</div>
        </body>
    </html>
    """

    discover_page = MagicMock()
    discover_page.status_code = 200
    discover_page.text = """
    <html>
        <body>
            <script type="application/json">
                {
                    "items": [
                        {
                            "author": {"linkName": "discover_author"},
                            "imageList": [{"url": "https://example.com/discover_media.webp"}]
                        }
                    ]
                }
            </script>
        </body>
    </html>
    """

    mock_get.side_effect = [topic_empty_page, discover_page]

    result = scraper.scrape_tag_topic("singapore", pages=3)

    assert result["topic_no_content_shell"] is True
    assert result["fallback_used"] is True
    assert "/discover/singapore?region=sg" in result["fallback_url"]
    assert result["media_urls"] == ["https://example.com/discover_media.webp"]
    assert result["media_items"][0]["username"] == "discover_author"
    assert mock_get.call_count == 2


@patch('requests.Session.get')
@patch('time.sleep', return_value=None)
def test_scrape_tag_topic_keyword_fallback_recovers_media_from_post_pages(_mock_sleep, mock_get, scraper):
    topic_empty_page = MagicMock()
    topic_empty_page.status_code = 200
    topic_empty_page.text = """
    <html>
        <body>
            <h2>Most recent</h2>
            <div>No content</div>
        </body>
    </html>
    """

    discover_links_only_page = MagicMock()
    discover_links_only_page.status_code = 200
    discover_links_only_page.text = """
    <html>
        <body>
            <a href="/@foodie/1234567890?region=sg">Foodie Post</a>
            <a href="/@traveller/1234567891?region=sg">Traveller Post</a>
        </body>
    </html>
    """

    post_page = MagicMock()
    post_page.status_code = 200
    post_page.text = """
    <html>
        <body>
            <script type="application/json">
                {
                    "items": [
                        {
                            "author": {"linkName": "foodie"},
                            "imageList": [{"url": "https://example.com/post_recovered.webp"}]
                        }
                    ]
                }
            </script>
        </body>
    </html>
    """

    second_post_page = MagicMock()
    second_post_page.status_code = 200
    second_post_page.text = """
    <html>
        <body>
            <script type="application/json">{"items": []}</script>
        </body>
    </html>
    """

    mock_get.side_effect = [
        topic_empty_page,
        discover_links_only_page,
        post_page,
        second_post_page,
    ]

    result = scraper.scrape_tag_topic("sg food", pages=2)

    assert result["fallback_used"] is True
    assert "/discover/sg%20food?region=sg" in result["fallback_url"]
    assert "https://example.com/post_recovered.webp" in result["media_urls"]
    assert result["fallback_post_pages_scraped"] >= 1
    assert mock_get.call_count == 4


def test_scrape_user_with_api_tries_at_prefixed_identifier_when_plain_is_empty(scraper):
    target = "7131016726570255361"

    class _FakeUserEndpoint:
        def __init__(self, status_code, text):
            self._status_code = status_code
            self._text = text

        def get_forced(self):
            response = MagicMock()
            response.status_code = self._status_code
            response.text = self._text
            return response

    class _FakeApi:
        def user(self, identifier):
            if identifier == target:
                return _FakeUserEndpoint(204, "")
            if identifier == f"@{target}":
                payload = """
                {"$UserDetailV2+%407131016726570255361":{"displayName":"API User","followerCount":7,"followingCount":3,"desc":"bio","verified":true},"data":{"items":[{"author":{"linkName":"api_user"},"title":"Night look #Glow #Makeup","imageList":[{"url":"https://example.com/api_post.webp"}]}]}}
                """.strip()
                return _FakeUserEndpoint(200, payload)
            raise AssertionError(f"Unexpected identifier: {identifier}")

    scraper.lemon8_api = _FakeApi()

    result = scraper._scrape_user_with_api(target)

    assert result["method"] == "pylemon8_api"
    assert result["media_urls"] == ["https://example.com/api_post.webp"]
    assert result["user_info"]["display_name"] == "API User"
    assert result["user_info"]["api_identifier_used"] == f"@{target}"
    assert "glow" in result["hashtags"]
    assert "makeup" in result["hashtags"]


def test_fetch_user_profile_payload_via_api_raises_clear_error_when_no_json(scraper):
    class _FakeUserEndpoint:
        def get_forced(self):
            response = MagicMock()
            response.status_code = 204
            response.text = ""
            return response

    class _FakeApi:
        def user(self, _identifier):
            return _FakeUserEndpoint()

    scraper.lemon8_api = _FakeApi()

    with pytest.raises(ValueError) as excinfo:
        scraper._fetch_user_profile_payload_via_api("7131016726570255361")

    assert "did not return a usable JSON payload" in str(excinfo.value)


def test_fetch_user_profile_payload_via_api_raises_permission_error_when_forbidden(scraper):
    class _FakeUserEndpoint:
        def get_forced(self):
            response = MagicMock()
            response.status_code = 403
            response.text = "<html>forbidden</html>"
            return response

    class _FakeApi:
        def user(self, _identifier):
            return _FakeUserEndpoint()

    scraper.lemon8_api = _FakeApi()

    with pytest.raises(PermissionError) as excinfo:
        scraper._fetch_user_profile_payload_via_api("7131016726570255361")

    assert "blocked/throttled" in str(excinfo.value)


def test_scrape_user_profile_falls_back_when_api_payload_unusable(scraper, monkeypatch):
    monkeypatch.setattr(scraper_module, "PYLEMON8_AVAILABLE", True)
    scraper.lemon8_api = object()

    with patch.object(scraper, '_scrape_user_with_api', side_effect=ValueError("api unavailable")):
        with patch.object(scraper, '_scrape_user_with_web', return_value={"username": "foo", "media_urls": []}) as web_method:
            result = scraper.scrape_user_profile("foo", use_api=True)

    web_method.assert_called_once_with("foo", include_profile_images=None)
    assert result == {"username": "foo", "media_urls": []}


def test_scrape_user_profile_disables_api_after_permission_error(scraper, monkeypatch):
    monkeypatch.setattr(scraper_module, "PYLEMON8_AVAILABLE", True)
    scraper.lemon8_api = object()

    with patch.object(scraper, '_scrape_user_with_api', side_effect=PermissionError("HTTP 403")) as api_method:
        with patch.object(scraper, '_scrape_user_with_web', return_value={"username": "foo", "media_urls": []}) as web_method:
            first_result = scraper.scrape_user_profile("foo", use_api=True)
            second_result = scraper.scrape_user_profile("foo", use_api=True)

    assert api_method.call_count == 1
    assert web_method.call_count == 2
    assert scraper.api_user_endpoint_blocked is True
    assert first_result == {"username": "foo", "media_urls": []}
    assert second_result == {"username": "foo", "media_urls": []}
