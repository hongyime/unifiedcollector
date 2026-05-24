"""Test delay utilities to ensure they work correctly."""

import time
from ingestion.core.delays import random_delay, exponential_backoff, DelayManager, create_delay_manager


def test_random_delay():
    """Test that random delay works within expected range."""
    print("Testing random delay...")
    
    # Test with a small range for quick testing
    start = time.time()
    random_delay((0.1, 0.3))
    end = time.time()
    
    elapsed = end - start
    assert 0.1 <= elapsed <= 0.5, f"Delay {elapsed}s was outside expected range [0.1, 0.5]"
    print(f"✓ Random delay worked: {elapsed:.2f}s")


def test_exponential_backoff():
    """Test exponential backoff calculation."""
    print("\nTesting exponential backoff...")
    
    # Test first attempt with no jitter for predictability
    delay1 = exponential_backoff(0, base_delay=1.0, max_delay=10.0, jitter=0)
    assert 0.9 <= delay1 <= 1.1, f"Incorrect base delay: {delay1}"
    print(f"✓ Attempt 0 delay: {delay1:.2f}s")
    
    # Test second attempt (should be roughly double)
    delay2 = exponential_backoff(1, base_delay=1.0, max_delay=10.0, jitter=0)
    assert 1.9 <= delay2 <= 2.1, f"Increasing delay too small: {delay2}"
    print(f"✓ Attempt 1 delay: {delay2:.2f}s")
    
    # Test max delay cap
    delay_max = exponential_backoff(20, base_delay=1.0, max_delay=10.0, jitter=0)
    assert delay_max <= 10.1, f"Delay exceeded max: {delay_max}"
    print(f"✓ Max delay capped: {delay_max:.2f}s")


def test_delay_manager():
    """Test DelayManager class."""
    print("\nTesting DelayManager...")
    
    manager = DelayManager(
        api_delay_range=(0.1, 0.2),
        feed_delay_range=(0.15, 0.25),
        backfill_delay_range=(0.2, 0.3),
        debug=False
    )
    
    # Test each delay type
    start = time.time()
    manager.api_delay()
    elapsed = time.time() - start
    assert 0.1 <= elapsed <= 0.3, f"API delay out of range: {elapsed}"
    print(f"✓ API delay: {elapsed:.2f}s")
    
    start = time.time()
    manager.feed_delay()
    elapsed = time.time() - start
    assert 0.15 <= elapsed <= 0.35, f"Feed delay out of range: {elapsed}"
    print(f"✓ Feed delay: {elapsed:.2f}s")


def test_create_delay_manager():
    """Test factory function for DelayManager."""
    print("\nTesting create_delay_manager factory...")
    
    settings = {
        "api_delay_min_seconds": 0.5,
        "api_delay_max_seconds": 1.0,
        "debug_delays": False
    }
    
    manager = create_delay_manager(settings)
    
    start = time.time()
    manager.api_delay()
    elapsed = time.time() - start
    
    assert 0.35 <= elapsed <= 1.3, f"Factory delay out of range: {elapsed}"
    print(f"✓ Factory manager delay: {elapsed:.2f}s")


def test_delay_validation():
    """Test that delay validation works correctly."""
    print("\nTesting delay validation...")
    
    # Test min > max (should swap)
    start = time.time()
    random_delay((0.3, 0.1))  # min > max
    elapsed = time.time() - start
    assert 0.1 <= elapsed <= 0.4, f"Swapped delay out of range: {elapsed}"
    print(f"✓ Swapped delay validation: {elapsed:.2f}s")
    
    # Test negative delay (should be 0)
    start = time.time()
    random_delay((-0.5, 0.1))
    elapsed = time.time() - start
    assert 0 <= elapsed <= 0.2, f"Negative delay not handled: {elapsed}"
    print(f"✓ Negative delay validation: {elapsed:.2f}s")


if __name__ == "__main__":
    print("Running delay utility tests...\n")
    print("=" * 50)
    
    try:
        test_random_delay()
        test_exponential_backoff()
        test_delay_manager()
        test_create_delay_manager()
        test_delay_validation()
        
        print("\n" + "=" * 50)
        print("✓ All tests passed!")
        print("\nDelay utilities are working correctly.")
        print("You can now use them to prevent 429 rate limit errors.")
        
    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        exit(1)
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        exit(1)