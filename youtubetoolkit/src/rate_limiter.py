#!/usr/bin/env python3
"""
Rate Limiter with Human-Like Behavior
=====================================
Implements intelligent rate limiting with:
- Random jitter to mimic human behavior
- Exponential backoff on errors
- Per-domain rate limits
- Burst protection
- Sliding window tracking

Designed to prevent 429 errors and mimic realistic usage patterns.
"""

import time
import random
import threading
from collections import deque, defaultdict
from typing import Optional, Dict, List
from datetime import datetime, timedelta

# Import resilience utilities
try:
    from resilience import _SHUTDOWN, _interruptible_sleep
except ImportError:
    import threading
    _SHUTDOWN = threading.Event()
    
    def _interruptible_sleep(seconds: float, check_interval: float = 0.2) -> None:
        """Sleep in short slices"""
        if seconds <= 0:
            return
        end_time = time.time() + seconds
        while True:
            remaining = end_time - time.time()
            if remaining <= 0 or _SHUTDOWN.is_set():
                return
            time.sleep(min(check_interval, remaining))


class RateLimiter:
    """
    Rate limiter with human-like behavior including jitter and exponential backoff.
    
    Features:
    - Base delay with random jitter between requests
    - Exponential backoff on 429 errors
    - Per-domain rate limiting
    - Burst protection
    - Sliding window tracking
    """
    
    # Default rate limits (requests per time window)
    # More conservative limits to avoid 429 errors
    DEFAULT_LIMITS = {
        'youtube.com': {
            'requests_per_hour': 60,      # ~1 request per minute (very conservative)
            'requests_per_minute': 5,      # Only 5 requests per minute
            'burst_limit': 3,             # Max 3 requests in burst window
            'burst_window': 20            # 20 second burst window
        },
        'default': {
            'requests_per_minute': 10,     # Default: 10 per minute
            'requests_per_hour': 1000,
            'burst_limit': 3,
            'burst_window': 20
        }
    }
    
    def __init__(
        self,
        base_delay: float = 2.0,
        jitter_min: float = 0.5,
        jitter_max: float = 1.5,
        max_backoff: float = 60.0,
        backoff_base: float = 1.0
    ):
        """
        Initialize rate limiter.
        
        Args:
            base_delay: Base delay between requests in seconds
            jitter_min: Minimum random jitter to add/subtract
            jitter_max: Maximum random jitter to add/subtract
            max_backoff: Maximum backoff delay on errors (seconds)
            backoff_base: Base multiplier for exponential backoff
        """
        self.base_delay = base_delay
        self.jitter_min = jitter_min
        self.jitter_max = jitter_max
        self.max_backoff = max_backoff
        self.backoff_base = backoff_base
        
        # Track last request time per endpoint
        self.last_request_time: Dict[str, float] = defaultdict(float)
        
        # Track request history for burst protection (sliding window)
        # Stores timestamps of last N requests
        self.request_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=100))
        
        # Track backoff level per endpoint (for exponential backoff)
        self.backoff_levels: Dict[str, int] = defaultdict(int)
        
        # Thread safety
        self.lock = threading.Lock()
        
        # Statistics
        self.stats = {
            'total_requests': 0,
            'total_delays': 0,
            'total_backoffs': 0,
            '429_errors': 0,
            'rate_limit_hits': 0
        }
    
    def _get_domain_from_url(self, url: str) -> str:
        """Extract domain from URL"""
        if not url:
            return 'default'
        
        try:
            domain = url.split('/')[2] if '://' in url else url.split('/')[0]
            # Remove www. prefix
            domain = domain.replace('www.', '')
            return domain
        except (IndexError, AttributeError):
            return 'default'
    
    def _get_rate_limits(self, domain: str) -> Dict:
        """Get rate limits for a domain"""
        return self.DEFAULT_LIMITS.get(domain, self.DEFAULT_LIMITS['default'])
    
    def _calculate_delay(self, endpoint: str) -> float:
        """
        Calculate delay before next request.
        
        Returns:
            float: Delay in seconds
        """
        domain = self._get_domain_from_url(endpoint)
        limits = self._get_rate_limits(domain)
        
        current_time = time.time()
        delay = 0.0
        
        with self.lock:
            # 1. Check burst protection
            history = self.request_history[endpoint]
            # Remove old timestamps outside burst window
            while history and (current_time - history[0]) > limits['burst_window']:
                history.popleft()
            
            # If over burst limit, wait until oldest expires
            if len(history) >= limits['burst_limit']:
                delay = max(delay, history[0] + limits['burst_window'] - current_time)
            
            # 2. Base delay from last request
            last_time = self.last_request_time[endpoint]
            if current_time - last_time < self.base_delay:
                delay = max(delay, self.base_delay - (current_time - last_time))
            
            # 3. Add random jitter (human-like behavior)
            jitter = random.uniform(self.jitter_min, self.jitter_max)
            delay += jitter * random.choice([-1, 1])
            delay = max(0.1, delay)  # Minimum 0.1s delay
            
            # 4. Check per-minute rate limit
            minute_ago = current_time - 60
            minute_requests = sum(1 for t in history if t > minute_ago)
            
            if minute_requests >= limits['requests_per_minute']:
                # Wait until a minute slot opens up
                oldest_in_minute = min([t for t in history if t > minute_ago], default=current_time)
                delay = max(delay, oldest_in_minute + 60 - current_time + jitter)
                self.stats['rate_limit_hits'] += 1
        
        return delay
    
    def wait_for_slot(self, endpoint: str) -> float:
        """
        Wait for an available request slot.
        
        Args:
            endpoint: URL or identifier for the endpoint
            
        Returns:
            float: Actual delay time in seconds
        """
        if _SHUTDOWN.is_set():
            return 0.0
        
        delay = self._calculate_delay(endpoint)
        
        if delay > 0:
            self.stats['total_delays'] += 1
            print(f"⏳ Rate limiting: Waiting {delay:.2f}s before request to {endpoint[:50]}...")
            _interruptible_sleep(delay)
        
        return delay
    
    def record_request(self, endpoint: str) -> None:
        """Record that a request was made to this endpoint"""
        with self.lock:
            current_time = time.time()
            self.last_request_time[endpoint] = current_time
            self.request_history[endpoint].append(current_time)
            self.stats['total_requests'] += 1
    
    def record_error(self, endpoint: str, error_status: int = 429) -> None:
        """Record an error, potentially triggering exponential backoff"""
        if error_status == 429:
            with self.lock:
                self.backoff_levels[endpoint] += 1
                self.stats['429_errors'] += 1
                self.stats['total_backoffs'] += 1
    
    def reset_backoff(self, endpoint: str) -> None:
        """Reset backoff level for an endpoint after successful request"""
        with self.lock:
            if endpoint in self.backoff_levels:
                del self.backoff_levels[endpoint]
    
    def get_backoff_delay(self, endpoint: str, attempt: int = 0) -> float:
        """
        Calculate exponential backoff delay.
        
        Args:
            endpoint: Endpoint identifier
            attempt: Current retry attempt (0-indexed)
            
        Returns:
            float: Delay in seconds
        """
        # Use the higher of: endpoint-specific backoff level or current attempt
        backoff_level = max(self.backoff_levels.get(endpoint, 0), attempt)
        
        # Exponential backoff: base_delay * (2 ^ level)
        delay = self.backoff_base * (2 ** backoff_level)
        
        # Cap at max_backoff
        delay = min(delay, self.max_backoff)
        
        # Add jitter
        jitter = random.uniform(0.5, 1.5)
        final_delay = delay * jitter
        
        return final_delay
    
    def wait_with_backoff(self, endpoint: str, attempt: int = 0) -> bool:
        """
        Wait with exponential backoff.
        
        Args:
            endpoint: Endpoint identifier
            attempt: Current retry attempt
            
        Returns:
            bool: True if wait completed, False if shutdown requested
        """
        delay = self.get_backoff_delay(endpoint, attempt)
        
        print(f"⚠️  Rate limit hit. Backing off for {delay:.1f}s (attempt {attempt + 1})...")
        
        # Check for shutdown during backoff
        if _SHUTDOWN.is_set():
            return False
        
        _interruptible_sleep(delay)
        
        return not _SHUTDOWN.is_set()
    
    def can_proceed(self, endpoint: str) -> bool:
        """
        Check if it's safe to proceed with a request.
        
        Returns:
            bool: True if safe, False if shutdown requested
        """
        return not _SHUTDOWN.is_set()
    
    def get_stats(self) -> Dict:
        """Get rate limiter statistics"""
        return {
            **self.stats,
            'active_backoffs': len(self.backoff_levels),
            'tracked_endpoints': len(self.last_request_time)
        }
    
    def print_stats(self) -> None:
        """Print current statistics"""
        stats = self.get_stats()
        print("\n📊 Rate Limiter Statistics:")
        print(f"   Total requests: {stats['total_requests']}")
        print(f"   Total delays: {stats['total_delays']}")
        print(f"   Total backoffs: {stats['total_backoffs']}")
        print(f"   429 errors: {stats['429_errors']}")
        print(f"   Rate limit hits: {stats['rate_limit_hits']}")
        print(f"   Active backoffs: {stats['active_backoffs']}")
        print(f"   Tracked endpoints: {stats['tracked_endpoints']}")


# Global rate limiter instance
_global_rate_limiter: Optional[RateLimiter] = None


def get_rate_limiter(
    base_delay: float = 2.0,
    jitter_min: float = 0.5,
    jitter_max: float = 1.5,
    max_backoff: float = 60.0,
    backoff_base: float = 1.0,
    force_recreate: bool = False
) -> RateLimiter:
    """
    Get or create the global rate limiter instance.
    
    Args:
        base_delay: Base delay between requests
        jitter_min: Minimum jitter
        jitter_max: Maximum jitter
        max_backoff: Maximum backoff delay
        backoff_base: Base for exponential backoff
        force_recreate: Force recreation of the limiter
        
    Returns:
        RateLimiter: The global rate limiter instance
    """
    global _global_rate_limiter
    
    if _global_rate_limiter is None or force_recreate:
        _global_rate_limiter = RateLimiter(
            base_delay=base_delay,
            jitter_min=jitter_min,
            jitter_max=jitter_max,
            max_backoff=max_backoff,
            backoff_base=backoff_base
        )
    
    return _global_rate_limiter


def reset_global_rate_limiter() -> None:
    """Reset the global rate limiter instance"""
    global _global_rate_limiter
    _global_rate_limiter = None
