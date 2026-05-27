"""
Unit tests for _make_request_with_retry method
"""
import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import requests

# Add parent directory and src folder to sys.path
_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_root / 'src'))
sys.path.insert(0, str(_root))

from scraper import Lemon8Scraper


@pytest.fixture
def scraper():
    """Create a scraper instance for testing"""
    return Lemon8Scraper()


def test_make_request_with_retry_success_on_first_attempt(scraper):
    """Test successful request on first attempt"""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = "Success"
    
    with patch.object(scraper.session, 'get', return_value=mock_response) as mock_get:
        with patch.object(scraper.rate_limiter, 'wait') as mock_wait:
            with patch.object(scraper.rate_limiter, 'record_success') as mock_record_success:
                result = scraper._make_request_with_retry('https://example.com/test')
                
                # Verify rate limiter was called
                mock_wait.assert_called_once_with('default')
                
                # Verify request was made
                mock_get.assert_called_once()
                
                # Verify success was recorded
                mock_record_success.assert_called_once_with('default')
                
                # Verify response is returned
                assert result == mock_response
                assert result.status_code == 200


def test_make_request_with_retry_handles_429_with_retry(scraper):
    """Test that 429 responses trigger retry logic"""
    mock_response_429 = MagicMock()
    mock_response_429.status_code = 429
    
    mock_response_200 = MagicMock()
    mock_response_200.status_code = 200
    
    with patch.object(scraper.session, 'get', side_effect=[mock_response_429, mock_response_200]) as mock_get:
        with patch.object(scraper.rate_limiter, 'wait') as mock_wait:
            with patch.object(scraper.rate_limiter, 'record_rate_limit') as mock_record_rate_limit:
                with patch.object(scraper.rate_limiter, 'record_success') as mock_record_success:
                    result = scraper._make_request_with_retry('https://example.com/test')
                    
                    # Verify rate limiter was called twice (once per attempt)
                    assert mock_wait.call_count == 2
                    
                    # Verify request was made twice
                    assert mock_get.call_count == 2
                    
                    # rate_limit is only recorded when ALL retries exhaust; a retry that
                    # succeeds on the next attempt does not trigger record_rate_limit
                    assert mock_record_rate_limit.call_count == 0

                    # Verify success was recorded on second attempt
                    mock_record_success.assert_called_once_with('default')

                    # Verify successful response is returned
                    assert result == mock_response_200


def test_make_request_with_retry_handles_403_with_retry(scraper):
    """Test that 403 responses trigger retry logic"""
    mock_response_403 = MagicMock()
    mock_response_403.status_code = 403

    mock_response_200 = MagicMock()
    mock_response_200.status_code = 200

    with patch.object(scraper.session, 'get', side_effect=[mock_response_403, mock_response_200]) as mock_get:
        with patch.object(scraper.rate_limiter, 'wait') as mock_wait:
            with patch.object(scraper.rate_limiter, 'record_error') as mock_record_error:
                with patch.object(scraper.rate_limiter, 'record_success') as mock_record_success:
                    result = scraper._make_request_with_retry('https://example.com/test')

                    # Verify rate limiter was called twice
                    assert mock_wait.call_count == 2

                    # Verify request was made twice
                    assert mock_get.call_count == 2

                    # record_error is only recorded when ALL retries exhaust; a retry
                    # that succeeds on the next attempt does not trigger record_error
                    assert mock_record_error.call_count == 0

                    # Verify success was recorded on second attempt
                    mock_record_success.assert_called_once_with('default')

                    # Verify successful response is returned
                    assert result == mock_response_200


def test_make_request_with_retry_exhausts_retries_on_429(scraper):
    """Test that retries are exhausted after max_retries attempts"""
    mock_response_429 = MagicMock()
    mock_response_429.status_code = 429
    
    with patch.object(scraper.session, 'get', return_value=mock_response_429) as mock_get:
        with patch.object(scraper.rate_limiter, 'wait') as mock_wait:
            with patch.object(scraper.rate_limiter, 'record_rate_limit') as mock_record_rate_limit:
                with pytest.raises(requests.exceptions.HTTPError) as excinfo:
                    scraper._make_request_with_retry('https://example.com/test', max_retries=3)
                
                # Verify rate limiter was called 3 times
                assert mock_wait.call_count == 3
                
                # Verify request was made 3 times
                assert mock_get.call_count == 3
                
                # Verify rate limit was recorded once (after all retries exhausted)
                assert mock_record_rate_limit.call_count == 1
                
                # Verify error message
                assert "429 Rate Limit" in str(excinfo.value)


def test_make_request_with_retry_exhausts_retries_on_403(scraper):
    """Test that retries are exhausted after max_retries attempts for 403"""
    mock_response_403 = MagicMock()
    mock_response_403.status_code = 403
    
    with patch.object(scraper.session, 'get', return_value=mock_response_403) as mock_get:
        with patch.object(scraper.rate_limiter, 'wait') as mock_wait:
            with patch.object(scraper.rate_limiter, 'record_error') as mock_record_error:
                with pytest.raises(requests.exceptions.HTTPError) as excinfo:
                    scraper._make_request_with_retry('https://example.com/test', max_retries=3)
                
                # Verify rate limiter was called 3 times
                assert mock_wait.call_count == 3
                
                # Verify request was made 3 times
                assert mock_get.call_count == 3
                
                # Verify error was recorded once (after all retries exhausted)
                assert mock_record_error.call_count == 1
                
                # Verify error message
                assert "403 Forbidden" in str(excinfo.value)


def test_make_request_with_retry_does_not_retry_404(scraper):
    """Test that 404 errors are not retried"""
    mock_response_404 = MagicMock()
    mock_response_404.status_code = 404
    
    # Create a proper HTTPError with response attribute
    http_error = requests.exceptions.HTTPError("404 Not Found")
    http_error.response = mock_response_404
    mock_response_404.raise_for_status.side_effect = http_error
    
    with patch.object(scraper.session, 'get', return_value=mock_response_404) as mock_get:
        with patch.object(scraper.rate_limiter, 'wait') as mock_wait:
            with pytest.raises(requests.exceptions.HTTPError) as excinfo:
                scraper._make_request_with_retry('https://example.com/test', max_retries=3)
            
            # Verify rate limiter was called only once
            assert mock_wait.call_count == 1
            
            # Verify request was made only once
            assert mock_get.call_count == 1
            
            # Verify error message
            assert "404 Not Found" in str(excinfo.value)


def test_make_request_with_retry_applies_referer(scraper):
    """Test that referer is applied to headers"""
    mock_response = MagicMock()
    mock_response.status_code = 200
    
    with patch.object(scraper.session, 'get', return_value=mock_response) as mock_get:
        with patch.object(scraper.rate_limiter, 'wait'):
            with patch.object(scraper.rate_limiter, 'record_success'):
                with patch.object(scraper, '_apply_rotating_headers') as mock_apply_headers:
                    scraper._make_request_with_retry(
                        'https://example.com/test',
                        referer='https://example.com/referer'
                    )
                    
                    # Verify headers were applied with referer
                    mock_apply_headers.assert_called_with(
                        endpoint_kind='page',
                        referer='https://example.com/referer'
                    )


def test_make_request_with_retry_uses_custom_account(scraper):
    """Test that custom account identifier is used"""
    mock_response = MagicMock()
    mock_response.status_code = 200
    
    with patch.object(scraper.session, 'get', return_value=mock_response):
        with patch.object(scraper.rate_limiter, 'wait') as mock_wait:
            with patch.object(scraper.rate_limiter, 'record_success') as mock_record_success:
                scraper._make_request_with_retry(
                    'https://example.com/test',
                    account='custom_account'
                )
                
                # Verify custom account was used
                mock_wait.assert_called_with('custom_account')
                mock_record_success.assert_called_with('custom_account')


def test_make_request_with_retry_uses_custom_timeout(scraper):
    """Test that custom timeout is used"""
    mock_response = MagicMock()
    mock_response.status_code = 200
    
    with patch.object(scraper.session, 'get', return_value=mock_response) as mock_get:
        with patch.object(scraper.rate_limiter, 'wait'):
            with patch.object(scraper.rate_limiter, 'record_success'):
                scraper._make_request_with_retry(
                    'https://example.com/test',
                    timeout=60
                )
                
                # Verify timeout was passed to session.get
                mock_get.assert_called_once()
                assert mock_get.call_args[1]['timeout'] == 60


def test_make_request_with_retry_handles_connection_error_with_retry(scraper):
    """Test that connection errors trigger retry logic"""
    with patch.object(scraper.session, 'get', side_effect=[
        requests.exceptions.ConnectionError("Connection failed"),
        MagicMock(status_code=200)
    ]) as mock_get:
        with patch.object(scraper.rate_limiter, 'wait') as mock_wait:
            with patch.object(scraper.rate_limiter, 'record_success'):
                result = scraper._make_request_with_retry('https://example.com/test')
                
                # Verify rate limiter was called twice
                assert mock_wait.call_count == 2
                
                # Verify request was made twice
                assert mock_get.call_count == 2
                
                # Verify successful response is returned
                assert result.status_code == 200
