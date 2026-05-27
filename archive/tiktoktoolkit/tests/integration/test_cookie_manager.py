"""Unit tests for cookie manager module."""

import pytest
from pathlib import Path
from datetime import datetime, timedelta
from src.cookie_manager import TikTokCookieManager


@pytest.fixture
def temp_cookie_file(tmp_path):
    """Create a temporary cookie file for testing."""
    cookie_file = tmp_path / "test_cookies.txt"
    return cookie_file


@pytest.fixture
def valid_cookies_content():
    """Generate valid Netscape cookie file content."""
    # Future expiration date
    future_timestamp = int((datetime.now() + timedelta(days=30)).timestamp())
    
    return f"""# Netscape HTTP Cookie File
# This is a generated file! Do not edit.

.tiktok.com	TRUE	/	TRUE	{future_timestamp}	sessionid	test_session_id_123
.tiktok.com	TRUE	/	TRUE	{future_timestamp}	sid_tt	test_sid_tt_456
.tiktok.com	TRUE	/	TRUE	{future_timestamp}	uid_tt	test_uid_tt_789
.tiktok.com	TRUE	/	TRUE	{future_timestamp}	msToken	test_msToken_abc
.tiktok.com	TRUE	/	TRUE	{future_timestamp}	tt_chain_token	test_chain_token_def
.tiktok.com	TRUE	/	TRUE	{future_timestamp}	s_v_web_id	test_s_v_web_id_ghi
.tiktok.com	TRUE	/	TRUE	{future_timestamp}	ttwid	test_ttwid_jkl
"""


@pytest.fixture
def missing_cookies_content():
    """Generate cookie file with missing required cookies."""
    future_timestamp = int((datetime.now() + timedelta(days=30)).timestamp())
    
    return f"""# Netscape HTTP Cookie File
.tiktok.com	TRUE	/	TRUE	{future_timestamp}	sessionid	test_session_id_123
.tiktok.com	TRUE	/	TRUE	{future_timestamp}	sid_tt	test_sid_tt_456
"""


@pytest.fixture
def expired_cookies_content():
    """Generate cookie file with expired cookies."""
    # Past expiration date
    past_timestamp = int((datetime.now() - timedelta(days=30)).timestamp())
    
    return f"""# Netscape HTTP Cookie File
.tiktok.com	TRUE	/	TRUE	{past_timestamp}	sessionid	test_session_id_123
.tiktok.com	TRUE	/	TRUE	{past_timestamp}	sid_tt	test_sid_tt_456
"""


class TestCookieManager:
    """Test suite for TikTokCookieManager."""
    
    def test_validate_cookies_with_complete_set(self, temp_cookie_file, valid_cookies_content):
        """Test validation with all required cookies present."""
        temp_cookie_file.write_text(valid_cookies_content)
        
        manager = TikTokCookieManager()
        result = manager.validate_cookies(temp_cookie_file)
        
        assert result['valid'] is True
        assert result['total_cookies'] == 7
        assert len(result['required_present']) == 7
        assert len(result['required_missing']) == 0
        assert len(result['warnings']) == 0
    
    def test_validate_cookies_with_missing_cookies(self, temp_cookie_file, missing_cookies_content):
        """Test validation with missing required cookies."""
        temp_cookie_file.write_text(missing_cookies_content)
        
        manager = TikTokCookieManager()
        result = manager.validate_cookies(temp_cookie_file)
        
        assert result['valid'] is False
        assert result['total_cookies'] == 2
        assert len(result['required_present']) == 2
        assert len(result['required_missing']) > 0
        assert 'msToken' in result['required_missing']
        assert 'tt_chain_token' in result['required_missing']
        assert len(result['warnings']) > 0
    
    def test_validate_cookies_with_expired_cookies(self, temp_cookie_file, expired_cookies_content):
        """Test validation with expired cookies."""
        temp_cookie_file.write_text(expired_cookies_content)
        
        manager = TikTokCookieManager()
        result = manager.validate_cookies(temp_cookie_file)
        
        # Should detect expired cookies
        assert any('expired' in warning.lower() for warning in result['warnings'])
    
    def test_validate_cookies_with_malformed_file(self, temp_cookie_file):
        """Test validation with malformed cookie file."""
        temp_cookie_file.write_text("This is not a valid cookie file\nJust random text")
        
        manager = TikTokCookieManager()
        result = manager.validate_cookies(temp_cookie_file)
        
        # Should handle gracefully
        assert result['valid'] is False
        assert result['total_cookies'] == 0
    
    def test_validate_cookies_with_nonexistent_file(self, tmp_path):
        """Test validation with non-existent file."""
        nonexistent_file = tmp_path / "does_not_exist.txt"
        
        manager = TikTokCookieManager()
        result = manager.validate_cookies(nonexistent_file)
        
        assert result['valid'] is False
        assert 'not found' in result['warnings'][0].lower()
    
    def test_validate_cookies_with_empty_file(self, temp_cookie_file):
        """Test validation with empty cookie file."""
        temp_cookie_file.write_text("")
        
        manager = TikTokCookieManager()
        result = manager.validate_cookies(temp_cookie_file)
        
        assert result['valid'] is False
        assert result['total_cookies'] == 0
        assert len(result['required_missing']) > 0
    
    def test_required_cookies_list(self):
        """Test that required cookies list is properly defined."""
        manager = TikTokCookieManager()
        
        assert 'sessionid' in manager.required_cookies
        assert 'msToken' in manager.required_cookies
        assert 'tt_chain_token' in manager.required_cookies
        assert len(manager.required_cookies) >= 3
    
    def test_recommended_cookies_list(self):
        """Test that recommended cookies list is properly defined."""
        manager = TikTokCookieManager()
        
        assert 's_v_web_id' in manager.recommended_cookies
        assert 'ttwid' in manager.recommended_cookies
        assert len(manager.recommended_cookies) >= 2


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
