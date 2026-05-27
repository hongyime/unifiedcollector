"""
Unit tests for AdaptiveRateLimiter jitter functionality (Task 3.1)
"""

import pytest
from unittest.mock import patch
from src.rate_limiter import AdaptiveRateLimiter


class TestAdaptiveRateLimiterJitter:
    """Test jitter support in AdaptiveRateLimiter"""
    
    def test_jitter_attribute_exists(self):
        """Test that AdaptiveRateLimiter has jitter attribute"""
        rl = AdaptiveRateLimiter(jitter=0.3)
        assert hasattr(rl, 'jitter'), "AdaptiveRateLimiter should have jitter attribute"
        assert rl.jitter == 0.3, "Jitter should be set to 0.3"
    
    def test_jitter_default_value(self):
        """Test that jitter has default value of 0.3"""
        rl = AdaptiveRateLimiter()
        assert rl.jitter == 0.3, "Default jitter should be 0.3 (±30%)"
    
    def test_delays_vary_with_jitter(self):
        """Test that delays vary when jitter is applied"""
        rl = AdaptiveRateLimiter(base_delay=2.0, jitter=0.3)
        
        delays = []
        
        def mock_sleep(seconds):
            delays.append(seconds)
        
        with patch('time.sleep', side_effect=mock_sleep):
            # Make multiple wait calls
            for _ in range(10):
                rl.wait()
        
        # Verify delays vary (not all the same)
        unique_delays = len(set(delays))
        assert unique_delays > 1, "Delays should vary due to jitter"
        
        # Most likely we'll have many unique delays with 10 samples
        assert unique_delays >= 8, f"Expected at least 8 unique delays, got {unique_delays}"
    
    def test_jitter_stays_within_bounds(self):
        """Test that jittered delays stay within expected range"""
        base_delay = 2.0
        jitter = 0.3
        rl = AdaptiveRateLimiter(base_delay=base_delay, jitter=jitter)
        
        min_expected = base_delay * (1 - jitter)
        max_expected = base_delay * (1 + jitter)
        
        delays = []
        
        def mock_sleep(seconds):
            delays.append(seconds)
        
        with patch('time.sleep', side_effect=mock_sleep):
            # Make multiple wait calls
            for _ in range(20):
                rl.wait()
        
        # Verify all delays are within expected range
        for delay in delays:
            assert min_expected <= delay <= max_expected, \
                f"Delay {delay:.4f}s outside expected range [{min_expected:.2f}s, {max_expected:.2f}s]"
    
    def test_zero_jitter_produces_fixed_delays(self):
        """Test that zero jitter produces consistent delays"""
        rl = AdaptiveRateLimiter(base_delay=2.0, jitter=0.0)
        
        delays = []
        
        def mock_sleep(seconds):
            delays.append(seconds)
        
        with patch('time.sleep', side_effect=mock_sleep):
            # Make multiple wait calls
            for _ in range(5):
                rl.wait()
        
        # With zero jitter, all delays should be the same
        unique_delays = len(set(delays))
        assert unique_delays == 1, "With zero jitter, all delays should be identical"
        assert delays[0] == 2.0, "Delay should equal base_delay when jitter is 0"
    
    def test_jitter_with_base_delay(self):
        """Test that jitter applies to base delay"""
        rl = AdaptiveRateLimiter(base_delay=5.0, jitter=0.3)

        delays = []

        def mock_sleep(seconds):
            delays.append(seconds)

        with patch('time.sleep', side_effect=mock_sleep):
            for _ in range(10):
                rl.wait()

        min_expected = 5.0 * (1 - 0.3)
        max_expected = 5.0 * (1 + 0.3)

        for delay in delays:
            assert min_expected <= delay <= max_expected, \
                f"Delay {delay:.4f}s outside expected range for base delay with jitter"

        # Verify delays vary
        unique_delays = len(set(delays))
        assert unique_delays > 1, "Delays should vary with jitter even for custom account delays"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
