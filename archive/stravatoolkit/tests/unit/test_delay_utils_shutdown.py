"""Test shutdown_event functionality in delay_utils."""

import threading
import time
from ingestion.core.delays import random_delay


def test_interruptible_delay_with_shutdown_event():
    """Test that random_delay can be interrupted by shutdown_event."""
    print("Testing interruptible delay with shutdown_event...")
    
    shutdown_event = threading.Event()
    
    # Test 1: Delay completes normally when shutdown_event is not set
    start = time.time()
    random_delay((0.1, 0.2), shutdown_event=shutdown_event)
    elapsed = time.time() - start
    assert 0.05 <= elapsed <= 0.5, f"Normal delay out of range: {elapsed}"
    print(f"✓ Normal delay with shutdown_event: {elapsed:.2f}s")
    
    # Test 2: Delay is interrupted immediately when shutdown_event is set
    shutdown_event.clear()
    
    def set_event_after_delay():
        time.sleep(0.1)  # Wait 0.1s before setting the event
        shutdown_event.set()
    
    # Start a thread that will set the event after 0.1s
    thread = threading.Thread(target=set_event_after_delay)
    thread.start()
    
    # Try to delay for 5 seconds, but should be interrupted after ~0.1s
    start = time.time()
    random_delay((5.0, 5.0), shutdown_event=shutdown_event)
    elapsed = time.time() - start
    
    thread.join()
    
    # Should be interrupted much sooner than 5 seconds
    assert elapsed < 1.0, f"Delay was not interrupted: {elapsed}s (expected < 1.0s)"
    print(f"✓ Delay interrupted by shutdown_event: {elapsed:.2f}s (expected ~0.1s)")


def test_backward_compatibility_without_shutdown_event():
    """Test that random_delay works without shutdown_event (backward compatibility)."""
    print("\nTesting backward compatibility without shutdown_event...")
    
    # Test without shutdown_event parameter (should use time.sleep)
    start = time.time()
    random_delay((0.1, 0.2))
    elapsed = time.time() - start
    assert 0.1 <= elapsed <= 0.3, f"Backward compatible delay out of range: {elapsed}"
    print(f"✓ Backward compatible delay: {elapsed:.2f}s")


def test_shutdown_event_none():
    """Test that random_delay works when shutdown_event is explicitly None."""
    print("\nTesting with shutdown_event=None...")
    
    start = time.time()
    random_delay((0.1, 0.2), shutdown_event=None)
    elapsed = time.time() - start
    assert 0.05 <= elapsed <= 0.5, f"Delay with None shutdown_event out of range: {elapsed}"
    print(f"✓ Delay with shutdown_event=None: {elapsed:.2f}s")


if __name__ == "__main__":
    print("Running shutdown_event tests for delay_utils...\n")
    print("=" * 50)
    
    try:
        test_interruptible_delay_with_shutdown_event()
        test_backward_compatibility_without_shutdown_event()
        test_shutdown_event_none()
        
        print("\n" + "=" * 50)
        print("✓ All shutdown_event tests passed!")
        print("\nInterruptible delay functionality is working correctly.")
        
    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        exit(1)
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        exit(1)
