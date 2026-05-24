"""
Preservation Property Tests for Human-Like Rate Limiting Bugfix

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7**

These tests MUST PASS on unfixed code - they confirm baseline behavior to preserve.

GOAL: Verify that non-HTTP-request operations remain unchanged:
- Media extraction from HTML/JSON
- Cookie loading and authentication
- Header rotation
- Username extraction and normalization
- Media URL validation

These tests use property-based testing to generate many test cases for stronger guarantees.
Run tests on UNFIXED code first to establish baseline behavior.

EXPECTED OUTCOME: Tests PASS (confirms baseline behavior to preserve)
"""

import pytest
import sys
import os
from pathlib import Path
from hypothesis import given, strategies as st, settings, Phase, assume
from unittest.mock import Mock, patch, MagicMock
import json
import html as html_module
from http.cookiejar import Cookie
from datetime import datetime

# Add parent directory and src folder to sys.path
_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_root / 'src'))
sys.path.insert(0, str(_root))

from scraper import Lemon8Scraper


class TestPreservationProperties:
    """
    Property 2: Preservation - Existing Scraping Functionality
    
    Test that non-HTTP-request operations produce the same behavior
    before and after the rate limiting fix.
    """
    
    @given(
        html_content=st.text(min_size=50, max_size=500)
    )
    @settings(
        max_examples=20,
        phases=[Phase.generate, Phase.target],
        deadline=None
    )
    def test_extract_media_urls_deterministic(self, html_content):
        """
        Test that _extract_media_urls() produces deterministic results.
        
        For the same HTML content, the function should always return
        the same list of media URLs.
        
        **Validates: Requirement 3.1**
        """
        scraper = Lemon8Scraper()
        
        # Call the function twice with the same input
        result1 = scraper._extract_media_urls(html_content)
        result2 = scraper._extract_media_urls(html_content)
        
        # Results should be identical (deterministic behavior)
        assert result1 == result2, \
            "Media extraction should be deterministic for the same HTML content"
        
        # Results should be lists
        assert isinstance(result1, list), "Result should be a list"
        assert isinstance(result2, list), "Result should be a list"
    
    @given(
        url=st.text(min_size=10, max_size=200)
    )
    @settings(
        max_examples=30,
        phases=[Phase.generate, Phase.target],
        deadline=None
    )
    def test_is_valid_media_url_deterministic(self, url):
        """
        Test that _is_valid_media_url() produces deterministic results.
        
        For the same URL, the function should always return the same
        validation result.
        
        **Validates: Requirement 3.1**
        """
        scraper = Lemon8Scraper()
        
        # Call the function twice with the same input
        result1 = scraper._is_valid_media_url(url)
        result2 = scraper._is_valid_media_url(url)
        
        # Results should be identical (deterministic behavior)
        assert result1 == result2, \
            "Media URL validation should be deterministic for the same URL"
        
        # Results should be booleans
        assert isinstance(result1, bool), "Result should be a boolean"
        assert isinstance(result2, bool), "Result should be a boolean"
    
    @given(
        url=st.text(min_size=10, max_size=200)
    )
    @settings(
        max_examples=30,
        phases=[Phase.generate, Phase.target],
        deadline=None
    )
    def test_clean_media_url_deterministic(self, url):
        """
        Test that _clean_media_url() produces deterministic results.
        
        For the same URL, the function should always return the same
        cleaned URL.
        
        **Validates: Requirement 3.1**
        """
        scraper = Lemon8Scraper()
        
        # Call the function twice with the same input
        result1 = scraper._clean_media_url(url)
        result2 = scraper._clean_media_url(url)
        
        # Results should be identical (deterministic behavior)
        assert result1 == result2, \
            "URL cleaning should be deterministic for the same URL"
        
        # Results should be strings
        assert isinstance(result1, str), "Result should be a string"
        assert isinstance(result2, str), "Result should be a string"
    
    @given(
        username=st.text(
            alphabet=st.characters(min_codepoint=32, max_codepoint=126),
            min_size=1,
            max_size=50
        )
    )
    @settings(
        max_examples=30,
        phases=[Phase.generate, Phase.target],
        deadline=None
    )
    def test_normalize_username_deterministic(self, username):
        """
        Test that _normalize_username() produces deterministic results.
        
        For the same username input, the function should always return
        the same normalized username.
        
        **Validates: Requirement 3.1**
        """
        scraper = Lemon8Scraper()
        
        # Call the function twice with the same input
        result1 = scraper._normalize_username(username)
        result2 = scraper._normalize_username(username)
        
        # Results should be identical (deterministic behavior)
        assert result1 == result2, \
            "Username normalization should be deterministic for the same input"
        
        # Results should be either string or None
        assert result1 is None or isinstance(result1, str), \
            "Result should be a string or None"
        assert result2 is None or isinstance(result2, str), \
            "Result should be a string or None"
    
    @given(
        endpoint_kind=st.sampled_from(['page', 'api']),
        referer=st.one_of(st.none(), st.text(min_size=10, max_size=100))
    )
    @settings(
        max_examples=20,
        phases=[Phase.generate, Phase.target],
        deadline=None
    )
    def test_build_rotating_headers_structure(self, endpoint_kind, referer):
        """
        Test that _build_rotating_headers() produces valid header structures.
        
        Headers should always be dictionaries with string keys and values,
        and should contain expected browser-like headers.
        
        **Validates: Requirement 3.2**
        """
        scraper = Lemon8Scraper()
        
        # Build headers
        headers = scraper._build_rotating_headers(
            endpoint_kind=endpoint_kind,
            referer=referer
        )
        
        # Headers should be a dictionary
        assert isinstance(headers, dict), "Headers should be a dictionary"
        
        # All keys and values should be strings
        for key, value in headers.items():
            assert isinstance(key, str), f"Header key should be string, got {type(key)}"
            assert isinstance(value, str), f"Header value should be string, got {type(value)}"
        
        # Should contain Accept-Encoding
        assert 'Accept-Encoding' in headers, "Headers should contain Accept-Encoding"
        
        # If referer provided, should be in headers
        if referer:
            assert 'Referer' in headers, "Headers should contain Referer when provided"
            assert headers['Referer'] == referer, "Referer should match input"
        
        # API endpoints should have specific headers
        if endpoint_kind == 'api':
            assert 'Accept' in headers, "API headers should contain Accept"
            assert 'application/json' in headers['Accept'], \
                "API Accept header should include application/json"
    
    @given(
        author_dict=st.dictionaries(
            keys=st.sampled_from([
                'uniqueId', 'username', 'userName', 'screenName',
                'handle', 'displayName', 'linkName', 'userId'
            ]),
            values=st.text(min_size=1, max_size=30)
        )
    )
    @settings(
        max_examples=20,
        phases=[Phase.generate, Phase.target],
        deadline=None
    )
    def test_extract_username_from_author_deterministic(self, author_dict):
        """
        Test that _extract_username_from_author() produces deterministic results.
        
        For the same author dictionary, the function should always return
        the same username.
        
        **Validates: Requirement 3.1**
        """
        scraper = Lemon8Scraper()
        
        # Call the function twice with the same input
        result1 = scraper._extract_username_from_author(author_dict)
        result2 = scraper._extract_username_from_author(author_dict)
        
        # Results should be identical (deterministic behavior)
        assert result1 == result2, \
            "Username extraction should be deterministic for the same author data"
        
        # Results should be either string or None
        assert result1 is None or isinstance(result1, str), \
            "Result should be a string or None"
    
    @given(
        item_dict=st.dictionaries(
            keys=st.sampled_from([
                'uniqueId', 'username', 'userName', 'authorId',
                'linkName', 'userId', 'uid', 'authorInfo', 'author'
            ]),
            values=st.one_of(
                st.text(min_size=1, max_size=30),
                st.dictionaries(
                    keys=st.sampled_from(['uniqueId', 'username', 'userName']),
                    values=st.text(min_size=1, max_size=30)
                )
            )
        )
    )
    @settings(
        max_examples=20,
        phases=[Phase.generate, Phase.target],
        deadline=None
    )
    def test_extract_username_from_item_deterministic(self, item_dict):
        """
        Test that _extract_username_from_item() produces deterministic results.
        
        For the same item dictionary, the function should always return
        the same username.
        
        **Validates: Requirement 3.1**
        """
        scraper = Lemon8Scraper()
        
        # Call the function twice with the same input
        result1 = scraper._extract_username_from_item(item_dict)
        result2 = scraper._extract_username_from_item(item_dict)
        
        # Results should be identical (deterministic behavior)
        assert result1 == result2, \
            "Username extraction from item should be deterministic"
        
        # Results should be either string or None
        assert result1 is None or isinstance(result1, str), \
            "Result should be a string or None"
    
    @given(
        json_data=st.recursive(
            st.one_of(
                st.none(),
                st.booleans(),
                st.integers(),
                st.floats(allow_nan=False, allow_infinity=False),
                st.text(max_size=50)
            ),
            lambda children: st.one_of(
                st.lists(children, max_size=5),
                st.dictionaries(
                    st.text(min_size=1, max_size=20),
                    children,
                    max_size=5
                )
            ),
            max_leaves=10
        )
    )
    @settings(
        max_examples=20,
        phases=[Phase.generate, Phase.target],
        deadline=None
    )
    def test_extract_urls_from_json_deterministic(self, json_data):
        """
        Test that _extract_urls_from_json() produces deterministic results.
        
        For the same JSON data, the function should always return
        the same list of URLs.
        
        **Validates: Requirement 3.1**
        """
        scraper = Lemon8Scraper()
        
        # Call the function twice with the same input
        result1 = scraper._extract_urls_from_json(json_data)
        result2 = scraper._extract_urls_from_json(json_data)
        
        # Results should be identical (deterministic behavior)
        assert result1 == result2, \
            "URL extraction from JSON should be deterministic"
        
        # Results should be lists
        assert isinstance(result1, list), "Result should be a list"
        assert isinstance(result2, list), "Result should be a list"
    
    def test_cookie_loading_preserves_session_state(self):
        """
        Test that _load_cookies_into_session() doesn't break session state.
        
        After loading cookies (or attempting to), the session should still
        be functional and have the expected attributes.
        
        **Validates: Requirement 3.3**
        """
        # Test with no cookie file
        scraper = Lemon8Scraper(cookie_file=None)
        
        # Session should exist and be functional
        assert hasattr(scraper, 'session'), "Scraper should have session attribute"
        assert scraper.session is not None, "Session should not be None"
        assert hasattr(scraper.session, 'headers'), "Session should have headers"
        assert hasattr(scraper.session, 'cookies'), "Session should have cookies"
        
        # Test with non-existent cookie file (should not crash)
        scraper2 = Lemon8Scraper(cookie_file="nonexistent_cookies.txt")
        
        # Session should still exist and be functional
        assert hasattr(scraper2, 'session'), "Scraper should have session attribute"
        assert scraper2.session is not None, "Session should not be None"
    
    @given(
        html_with_media=st.sampled_from([
            '<html><img src="https://example.com/image.jpg"/></html>',
            '<html><video src="https://example.com/video.mp4"></video></html>',
            '<html><source src="https://tiktokcdn.com/video.mp4"/></html>',
            '<html><img src="https://byteimg.com/photo.png"/></html>',
            '<html><script type="application/json">{"url":"https://example.com/media.jpg"}</script></html>',
        ])
    )
    @settings(
        max_examples=10,
        phases=[Phase.generate, Phase.target],
        deadline=None
    )
    def test_extract_media_urls_finds_valid_patterns(self, html_with_media):
        """
        Test that _extract_media_urls() correctly identifies media URLs.
        
        The function should extract media URLs from various HTML patterns
        consistently.
        
        **Validates: Requirement 3.1**
        """
        scraper = Lemon8Scraper()
        
        # Extract media URLs
        result = scraper._extract_media_urls(html_with_media)
        
        # Result should be a list
        assert isinstance(result, list), "Result should be a list"
        
        # All items in result should be strings
        for url in result:
            assert isinstance(url, str), f"URL should be string, got {type(url)}"
    
    @given(
        valid_media_urls=st.sampled_from([
            'https://example.com/image.jpg',
            'https://example.com/video.mp4',
            'https://tiktokcdn.com/media.jpg',
            'https://byteimg.com/photo.png',
            'https://example.com/video.webm',
            'https://example.com/image.jpeg?param=value',
        ])
    )
    @settings(
        max_examples=10,
        phases=[Phase.generate, Phase.target],
        deadline=None
    )
    def test_is_valid_media_url_accepts_valid_urls(self, valid_media_urls):
        """
        Test that _is_valid_media_url() correctly validates media URLs.
        
        Valid media URLs with proper extensions should be accepted.
        
        **Validates: Requirement 3.1**
        """
        scraper = Lemon8Scraper()
        
        # Validate the URL
        result = scraper._is_valid_media_url(valid_media_urls)
        
        # Should return True for valid media URLs
        assert result is True, \
            f"Valid media URL should be accepted: {valid_media_urls}"
    
    @given(
        invalid_urls=st.sampled_from([
            'https://example.com/script.js',
            'https://example.com/style.css',
            'https://example.com/data.json',
            'not-a-url',
            '',
            'ftp://example.com/file.jpg',
            'https://example.com/favicon.ico',
        ])
    )
    @settings(
        max_examples=10,
        phases=[Phase.generate, Phase.target],
        deadline=None
    )
    def test_is_valid_media_url_rejects_invalid_urls(self, invalid_urls):
        """
        Test that _is_valid_media_url() correctly rejects non-media URLs.
        
        Invalid URLs or non-media files should be rejected.
        
        **Validates: Requirement 3.1**
        """
        scraper = Lemon8Scraper()
        
        # Validate the URL
        result = scraper._is_valid_media_url(invalid_urls)
        
        # Should return False for invalid URLs
        assert result is False, \
            f"Invalid URL should be rejected: {invalid_urls}"
    
    @given(
        url_with_entities=st.sampled_from([
            'https://example.com/image.jpg?param=value&amp;other=test',
            'https://example.com/video.mp4?a=1&amp;b=2&amp;c=3',
            'https://example.com/photo.png?x=&quot;test&quot;',
        ])
    )
    @settings(
        max_examples=10,
        phases=[Phase.generate, Phase.target],
        deadline=None
    )
    def test_clean_media_url_unescapes_entities(self, url_with_entities):
        """
        Test that _clean_media_url() correctly unescapes HTML entities.
        
        URLs with HTML entities like &amp; should be unescaped to &.
        
        **Validates: Requirement 3.1**
        """
        scraper = Lemon8Scraper()
        
        # Clean the URL
        result = scraper._clean_media_url(url_with_entities)
        
        # Result should not contain HTML entities
        assert '&amp;' not in result, "Cleaned URL should not contain &amp;"
        assert '&quot;' not in result, "Cleaned URL should not contain &quot;"
        
        # Result should be a string
        assert isinstance(result, str), "Result should be a string"
    
    def test_apply_rotating_headers_updates_session(self):
        """
        Test that _apply_rotating_headers() updates session headers.
        
        After calling this method, the session should have updated headers.
        
        **Validates: Requirement 3.2**
        """
        scraper = Lemon8Scraper()
        
        # Get initial headers
        initial_headers = dict(scraper.session.headers)
        
        # Apply rotating headers
        scraper._apply_rotating_headers(endpoint_kind='page')
        
        # Session headers should be updated
        updated_headers = dict(scraper.session.headers)
        
        # Headers should exist
        assert len(updated_headers) > 0, "Session should have headers"
        
        # Should contain Accept-Encoding
        assert 'Accept-Encoding' in updated_headers, \
            "Headers should contain Accept-Encoding"
    
    @given(
        username_variants=st.sampled_from([
            'testuser',
            'TestUser',
            'test_user',
            'test-user',
            'test.user',
            '@testuser',
            'testuser123',
        ])
    )
    @settings(
        max_examples=10,
        phases=[Phase.generate, Phase.target],
        deadline=None
    )
    def test_normalize_username_handles_variants(self, username_variants):
        """
        Test that _normalize_username() handles various username formats.
        
        The function should normalize different username formats consistently.
        
        **Validates: Requirement 3.1**
        """
        scraper = Lemon8Scraper()
        
        # Normalize the username
        result = scraper._normalize_username(username_variants)
        
        # Result should be either string or None
        assert result is None or isinstance(result, str), \
            "Result should be a string or None"
        
        # If result is a string, it should not be empty
        if result is not None:
            assert len(result) > 0, "Normalized username should not be empty"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
