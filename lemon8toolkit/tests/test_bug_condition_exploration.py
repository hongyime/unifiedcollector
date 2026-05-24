"""
Bug Condition Exploration Test for Human-Like Rate Limiting

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6**

This test MUST FAIL on unfixed code - failure confirms the bug exists.
DO NOT attempt to fix the test or the code when it fails.

GOAL: Surface counterexamples that demonstrate the bug exists:
- HTTP requests are made without rate limiting
- 403 responses cause immediate failure without retry logic
- Consecutive requests have no delays between them
- Lemon8Scraper instance has no rate_limiter attribute
- Fixed delays (if any) are predictable without jitter

This test encodes the EXPECTED behavior - it will validate the fix when it passes after implementation.
"""

import pytest
import sys
import os
from pathlib import Path
from hypothesis import given, strategies as st, settings, Phase
from unittest.mock import Mock, patch, MagicMock
import time

# Add parent directory and src folder to sys.path
_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_root / 'src'))
sys.path.insert(0, str(_root))

from scraper import Lemon8Scraper


class TestBugConditionExploration:
    """
    Property 1: Bug Condition - HTTP Requests Without Rate Limiting
    
    Test that the scraper makes HTTP requests without proper rate limiting,
    causing 403 Forbidden errors and predictable timing patterns.
    """
    
    @given(
        username=st.text(
            alphabet=st.characters(min_codepoint=97, max_codepoint=122),
            min_size=3,
            max_size=15
        )
    )
    @settings(
        max_examples=10,
        phases=[Phase.generate, Phase.target],
        deadline=None
    )
    def test_scraper_has_rate_limiter_attribute(self, username):
        """
        Test that Lemon8Scraper instance has a rate_limiter attribute.
        
        EXPECTED ON UNFIXED CODE: FAIL - no rate_limiter attribute exists
        EXPECTED ON FIXED CODE: PASS - rate_limiter attribute exists
        
        **Validates: Requirement 1.6**
        """
        scraper = Lemon8Scraper()
        
        # The scraper SHOULD have a rate_limiter attribute
        assert hasattr(scraper, 'rate_limiter'), \
            "Scraper should have rate_limiter attribute (Bug: missing rate limiter integration)"
        
        # The rate_limiter should not be None
        assert scraper.rate_limiter is not None, \
            "rate_limiter should be initialized (Bug: rate limiter not instantiated)"
    
    @given(
        url=st.text(min_size=10, max_size=50)
    )
    @settings(
        max_examples=10,
        phases=[Phase.generate, Phase.target],
        deadline=None
    )
    def test_http_requests_call_rate_limiter_wait(self, url):
        """
        Test that HTTP requests call rate_limiter.wait() before making the request.
        
        EXPECTED ON UNFIXED CODE: FAIL - rate_limiter.wait() is never called
        EXPECTED ON FIXED CODE: PASS - rate_limiter.wait() is called before each request
        
        **Validates: Requirement 1.1**
        """
        scraper = Lemon8Scraper()
        
        # Mock the session.get to avoid actual HTTP requests
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = "<html></html>"
        mock_response.raise_for_status = Mock()
        
        with patch.object(scraper.session, 'get', return_value=mock_response) as mock_get:
            # Mock rate_limiter if it exists, otherwise create a mock
            if hasattr(scraper, 'rate_limiter'):
                with patch.object(scraper.rate_limiter, 'wait') as mock_wait:
                    try:
                        # Try to make a request through the scraper's retry wrapper method
                        if hasattr(scraper, '_make_request_with_retry'):
                            response = scraper._make_request_with_retry(
                                f"https://www.lemon8-app.com/test/{url}"
                            )
                        else:
                            # Fallback to direct call (for unfixed code)
                            scraper._apply_rotating_headers()
                            response = scraper.session.get(url, timeout=30)
                        
                        # The rate_limiter.wait() SHOULD have been called before session.get()
                        assert mock_wait.called, \
                            "rate_limiter.wait() should be called before HTTP requests (Bug: no rate limiting applied)"
                    except AttributeError:
                        # If the method doesn't exist yet, that's expected on unfixed code
                        pytest.fail("Bug confirmed: rate_limiter.wait() is not called before HTTP requests")
            else:
                pytest.fail("Bug confirmed: Scraper has no rate_limiter attribute")
    
    @given(
        status_code=st.sampled_from([403, 429])
    )
    @settings(
        max_examples=5,
        phases=[Phase.generate, Phase.target],
        deadline=None
    )
    def test_403_429_responses_trigger_retry_logic(self, status_code):
        """
        Test that 403/429 responses trigger retry logic with exponential backoff.
        
        EXPECTED ON UNFIXED CODE: FAIL - immediate failure without retry
        EXPECTED ON FIXED CODE: PASS - retries with exponential backoff
        
        **Validates: Requirements 1.2, 1.4**
        """
        scraper = Lemon8Scraper()
        
        # Mock response with 403 or 429 status
        mock_response = Mock()
        mock_response.status_code = status_code
        mock_response.raise_for_status = Mock(side_effect=Exception(f"HTTP {status_code}"))
        
        retry_count = 0
        
        def mock_get_with_retry(*args, **kwargs):
            nonlocal retry_count
            retry_count += 1
            if retry_count < 3:
                # First 2 attempts fail
                return mock_response
            else:
                # Third attempt succeeds
                success_response = Mock()
                success_response.status_code = 200
                success_response.text = "<html></html>"
                success_response.raise_for_status = Mock()
                return success_response
        
        with patch.object(scraper.session, 'get', side_effect=mock_get_with_retry):
            # Check if scraper has retry logic
            if hasattr(scraper, '_make_request_with_retry'):
                # Try to make a request with retry logic
                try:
                    response = scraper._make_request_with_retry(
                        "https://www.lemon8-app.com/test",
                        max_retries=3
                    )
                    # Should succeed after retries
                    assert retry_count >= 2, \
                        f"Should retry at least 2 times for {status_code} (Bug: no retry logic)"
                except Exception as e:
                    pytest.fail(f"Bug confirmed: {status_code} response causes immediate failure without retry: {e}")
            else:
                pytest.fail(f"Bug confirmed: No retry logic exists for {status_code} responses")
    
    @given(
        num_requests=st.integers(min_value=2, max_value=5)
    )
    @settings(
        max_examples=5,
        phases=[Phase.generate, Phase.target],
        deadline=None
    )
    def test_consecutive_requests_have_delays(self, num_requests):
        """
        Test that consecutive requests have delays between them.
        
        EXPECTED ON UNFIXED CODE: FAIL - no delays between requests
        EXPECTED ON FIXED CODE: PASS - delays are enforced between requests
        
        **Validates: Requirement 1.3**
        """
        scraper = Lemon8Scraper()
        
        # Mock successful responses
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = "<html></html>"
        mock_response.raise_for_status = Mock()
        
        request_times = []
        
        def mock_get_with_timing(*args, **kwargs):
            request_times.append(time.time())
            return mock_response
        
        with patch.object(scraper.session, 'get', side_effect=mock_get_with_timing):
            if hasattr(scraper, 'rate_limiter'):
                # Make consecutive requests
                for i in range(num_requests):
                    try:
                        if hasattr(scraper, '_make_request_with_retry'):
                            scraper._make_request_with_retry(f"https://www.lemon8-app.com/test{i}")
                        else:
                            scraper.rate_limiter.wait()
                            scraper.session.get(f"https://www.lemon8-app.com/test{i}", timeout=30)
                    except Exception:
                        pass
                
                # Check that there were delays between requests
                if len(request_times) >= 2:
                    delays = [request_times[i+1] - request_times[i] for i in range(len(request_times)-1)]
                    
                    # All delays should be > 0 (some delay exists)
                    assert all(delay > 0 for delay in delays), \
                        "Consecutive requests should have delays between them (Bug: no delays applied)"
                    
                    # At least some delays should be >= 1 second (rate limiting is active)
                    assert any(delay >= 1.0 for delay in delays), \
                        "Delays should be at least 1 second for rate limiting (Bug: delays too short or missing)"
            else:
                pytest.fail("Bug confirmed: No rate_limiter attribute exists")
    
    @given(
        base_delay=st.floats(min_value=1.0, max_value=5.0)
    )
    @settings(
        max_examples=10,
        phases=[Phase.generate, Phase.target],
        deadline=None
    )
    def test_delays_have_randomized_jitter(self, base_delay):
        """
        Test that delays have randomized jitter to avoid predictable patterns.
        
        EXPECTED ON UNFIXED CODE: FAIL - delays are fixed without jitter
        EXPECTED ON FIXED CODE: PASS - delays vary with jitter
        
        **Validates: Requirement 1.5**
        """
        scraper = Lemon8Scraper()
        
        if hasattr(scraper, 'rate_limiter'):
            # Check if rate_limiter has jitter support
            assert hasattr(scraper.rate_limiter, 'jitter'), \
                "rate_limiter should have jitter attribute (Bug: no jitter support)"
            
            # Jitter should be > 0 to add randomization
            assert scraper.rate_limiter.jitter > 0, \
                "rate_limiter.jitter should be > 0 for randomization (Bug: jitter not configured)"
            
            # Collect multiple delay samples to verify they vary
            delays = []
            mock_sleep_calls = []
            
            def mock_sleep(seconds):
                mock_sleep_calls.append(seconds)
            
            with patch('time.sleep', side_effect=mock_sleep):
                # Make multiple wait calls
                for _ in range(5):
                    scraper.rate_limiter.wait()
            
            # Check that delays vary (not all the same)
            if len(mock_sleep_calls) >= 2:
                unique_delays = set(mock_sleep_calls)
                assert len(unique_delays) > 1, \
                    "Delays should vary due to jitter (Bug: fixed delays without randomization)"
        else:
            pytest.fail("Bug confirmed: No rate_limiter attribute exists")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
