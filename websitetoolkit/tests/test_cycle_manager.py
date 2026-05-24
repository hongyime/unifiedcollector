import pytest
import asyncio
import time
from cycle_manager import GlobalRateLimiter, RateLimitConfig, CycleManager, CycleStats

@pytest.mark.asyncio
async def test_rate_limiter_respects_minute_limit():
    # Set a very low limit for testing: 2 requests per minute
    config = RateLimitConfig(
        requests_per_minute=2,
        requests_per_hour=100,
        requests_per_day=1000,
        min_delay_between_requests=0.1
    )
    limiter = GlobalRateLimiter(config)
    
    # First 2 requests should be immediate (or with min_delay)
    assert await limiter.acquire() is True
    assert await limiter.acquire() is True
    
    # 3rd request should fail as it exceeds 2 per minute
    assert await limiter.acquire() is False

@pytest.mark.asyncio
async def test_rate_limiter_delay_between_requests():
    config = RateLimitConfig(
        requests_per_minute=10,
        min_delay_between_requests=0.5
    )
    limiter = GlobalRateLimiter(config)
    
    start_time = time.time()
    await limiter.acquire() # 1st request
    await limiter.acquire() # 2nd request, should be delayed by ~0.5s
    end_time = time.time()
    
    duration = end_time - start_time
    assert duration >= 0.5

def test_cycle_stats_initialization():
    stats = CycleStats(cycle_id="test_cycle", start_time="2024-01-01T00:00:00")
    assert stats.cycle_id == "test_cycle"
    assert stats.websites_crawled == 0
    assert stats.photos_downloaded == 0

@pytest.mark.asyncio
async def test_cycle_manager_id_generation():
    manager = CycleManager()
    cycle_id = manager.generate_cycle_id()
    assert len(cycle_id) > 0
    # Should be in YYYYMMDD_HHMMSS format
    assert "_" in cycle_id
    assert len(cycle_id) >= 15
