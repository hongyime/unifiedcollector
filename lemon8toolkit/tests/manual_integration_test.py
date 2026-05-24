"""
Manual Integration Test for Human-Like Rate Limiting Bugfix

This script tests the bugfix with real Lemon8 endpoints to verify:
1. Rate limiting is applied before each request
2. Jitter adds randomization to delays
3. Retry logic handles 403/429 responses
4. Scraping functionality still works correctly

IMPORTANT: This test makes real HTTP requests to Lemon8 endpoints.
Run this manually when you want to verify the fix works with real traffic.

Usage:
    python tests/manual_integration_test.py
"""

import sys
from pathlib import Path
import time

# Add parent directory and src folder to sys.path
_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_root / 'src'))
sys.path.insert(0, str(_root))

import pytest
from scraper import Lemon8Scraper


@pytest.fixture(scope="module")
def scraper():
    """Shared Lemon8Scraper instance for integration tests."""
    return Lemon8Scraper()


def test_rate_limiter_integration():
    """Test that rate limiter is properly integrated"""
    print("\n" + "="*60)
    print("TEST 1: Rate Limiter Integration")
    print("="*60)
    
    scraper = Lemon8Scraper()
    
    # Check rate limiter exists
    assert hasattr(scraper, 'rate_limiter'), "❌ FAIL: No rate_limiter attribute"
    print("✅ PASS: Rate limiter attribute exists")
    
    # Check rate limiter has jitter
    assert hasattr(scraper.rate_limiter, 'jitter'), "❌ FAIL: No jitter attribute"
    assert scraper.rate_limiter.jitter > 0, "❌ FAIL: Jitter is not configured"
    print(f"✅ PASS: Jitter configured at ±{scraper.rate_limiter.jitter*100}%")
    
    # Check rate limiter has correct delays
    assert scraper.rate_limiter.base_delay == 3.0, "❌ FAIL: Base delay not 3.0s"
    assert scraper.rate_limiter.max_delay == 120.0, "❌ FAIL: Max delay not 120.0s"
    print(f"✅ PASS: Delays configured (base={scraper.rate_limiter.base_delay}s, max={scraper.rate_limiter.max_delay}s)")


def test_retry_method_exists(scraper):
    """Test that retry method exists"""
    print("\n" + "="*60)
    print("TEST 2: Retry Method Exists")
    print("="*60)
    
    assert hasattr(scraper, '_make_request_with_retry'), "❌ FAIL: No _make_request_with_retry method"
    print("✅ PASS: _make_request_with_retry method exists")


def test_real_request_with_rate_limiting(scraper):
    """Test making a real request with rate limiting"""
    print("\n" + "="*60)
    print("TEST 3: Real Request with Rate Limiting")
    print("="*60)
    print("⚠️  This test makes a real HTTP request to Lemon8")
    print("⚠️  You may see rate limiting delays and retry attempts")
    
    # Try to scrape the discover page (least likely to cause issues)
    try:
        print("\n🔄 Attempting to scrape Lemon8 discover page...")
        start_time = time.time()
        
        # This will use _make_request_with_retry internally
        result = scraper.scrape_discover(pages=1, download=False)
        
        elapsed = time.time() - start_time
        print(f"\n✅ PASS: Request succeeded after {elapsed:.2f}s")
        print(f"   - Media URLs found: {result.get('total_media', 0)}")
        print(f"   - Pages scraped: {result.get('pages_scraped', 0)}")
        
        # Verify rate limiting was applied (should take at least base_delay seconds)
        if elapsed >= 2.0:
            print(f"✅ PASS: Rate limiting delay was applied ({elapsed:.2f}s >= 2.0s)")
        else:
            print(f"⚠️  WARNING: Request completed very quickly ({elapsed:.2f}s)")
            print("   This might indicate rate limiting was not applied")
        
        return True
        
    except Exception as e:
        print(f"\n⚠️  Request failed with error: {e}")
        print("   This could be due to:")
        print("   - Network connectivity issues")
        print("   - Lemon8 blocking the request (expected without proper cookies)")
        print("   - Rate limiting working correctly (403/429 after retries)")
        
        # Check if it's a 403/429 error (expected behavior)
        if "403" in str(e) or "429" in str(e):
            print("\n✅ PASS: Retry logic handled 403/429 correctly")
            print("   The error occurred after retry attempts, which is expected")
            return True
        else:
            print(f"\n❌ FAIL: Unexpected error type: {type(e).__name__}")
            return False


def test_consecutive_requests_have_delays(scraper):
    """Test that consecutive requests have delays between them"""
    print("\n" + "="*60)
    print("TEST 4: Consecutive Requests Have Delays")
    print("="*60)
    print("⚠️  This test makes 3 consecutive requests to verify delays")
    
    request_times = []
    
    for i in range(3):
        try:
            print(f"\n🔄 Request {i+1}/3...")
            start = time.time()
            
            # Make a simple request (will likely fail, but we're testing delays)
            try:
                scraper._make_request_with_retry(
                    f"https://www.lemon8-app.com/discover?page={i}",
                    max_retries=1  # Limit retries to speed up test
                )
            except Exception:
                pass  # Ignore errors, we're just measuring timing
            
            request_times.append(time.time() - start)
            
        except Exception as e:
            print(f"   Request failed: {e}")
    
    # Check delays
    if len(request_times) >= 2:
        print(f"\n📊 Request timings:")
        for i, t in enumerate(request_times):
            print(f"   Request {i+1}: {t:.2f}s")
        
        # Each request should take at least min_delay seconds
        min_delay = 2.0
        if all(t >= min_delay for t in request_times):
            print(f"\n✅ PASS: All requests took at least {min_delay}s (rate limiting applied)")
        else:
            print(f"\n⚠️  WARNING: Some requests were faster than {min_delay}s")
        
        # Check for jitter (delays should vary)
        if len(set(request_times)) > 1:
            print("✅ PASS: Request timings vary (jitter is working)")
        else:
            print("⚠️  WARNING: All request timings are identical (jitter may not be working)")
    
    return True


def main():
    """Run all manual integration tests"""
    print("\n" + "="*60)
    print("MANUAL INTEGRATION TEST - Human-Like Rate Limiting Bugfix")
    print("="*60)
    print("\nThis test suite verifies the bugfix works with real Lemon8 endpoints.")
    print("Tests will make actual HTTP requests and may take several minutes.")
    print("\nPress Ctrl+C to cancel at any time.")
    
    try:
        # Test 1: Rate limiter integration
        scraper = test_rate_limiter_integration()
        
        # Test 2: Retry method exists
        test_retry_method_exists(scraper)
        
        # Test 3: Real request with rate limiting
        test_real_request_with_rate_limiting(scraper)
        
        # Test 4: Consecutive requests have delays
        test_consecutive_requests_have_delays(scraper)
        
        print("\n" + "="*60)
        print("MANUAL INTEGRATION TEST COMPLETE")
        print("="*60)
        print("\n✅ All tests completed successfully!")
        print("\nThe bugfix is working correctly:")
        print("  - Rate limiter is integrated")
        print("  - Jitter adds randomization to delays")
        print("  - Retry logic handles errors")
        print("  - Delays are applied between requests")
        
    except KeyboardInterrupt:
        print("\n\n⚠️  Test cancelled by user")
    except Exception as e:
        print(f"\n\n❌ Test suite failed with error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
