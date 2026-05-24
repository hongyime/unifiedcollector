"""
Unified Lemon8 Toolkit - Adaptive Rate Limiter
Ported from searchtoolkit pattern
"""
import random
import time
from typing import Dict, Optional
from datetime import datetime, timedelta


class AdaptiveRateLimiter:
    """
    GLOBAL adaptive rate limiting with IP-level tracking
    - When ANY request gets 403, ALL requests slow down (server is tracking your IP)
    - Reduces delay after consecutive successes
    - Aggressive backoff on 403 errors (30-60s delays)
    - Random jitter on all delays to appear human-like
    """
    
    def __init__(
        self,
        base_delay: float = 2.0,
        min_delay: float = 1.0,
        max_delay: float = 120.0,  # Increased to 2 minutes for aggressive backoff
        success_threshold: int = 5,
        delay_reduction: float = 0.2,
        jitter: float = 0.3,
        forbidden_backoff: float = 30.0  # Jump to 30s on 403
    ):
        """
        Initialize adaptive rate limiter
        
        Args:
            base_delay: Starting delay between requests (seconds)
            min_delay: Minimum delay (seconds)
            max_delay: Maximum delay (seconds)
            success_threshold: Number of successes before reducing delay
            delay_reduction: Amount to reduce delay after success threshold
            jitter: Random variation to apply to delays (default 0.3 for ±30%)
            forbidden_backoff: Delay to jump to on 403 errors (default 30s)
        """
        self.base_delay = base_delay
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.success_threshold = success_threshold
        self.delay_reduction = delay_reduction
        self.jitter = jitter
        self.forbidden_backoff = forbidden_backoff
        
        # GLOBAL state (not per-account - server tracks your IP!)
        self.current_delay: float = base_delay
        self.consecutive_successes: int = 0
        self.cooldown_until: Optional[datetime] = None
        self.last_request_time: float = 0
        self.is_ip_flagged: bool = False  # Track if server has flagged our IP
    
    def wait(self, account: str = 'default'):
        """
        Wait appropriate time before next request (GLOBAL rate limiting)
        
        Args:
            account: Account identifier (kept for compatibility, but uses global state)
        """
        # Check if in cooldown
        if self.cooldown_until:
            if datetime.now() < self.cooldown_until:
                wait_seconds = (self.cooldown_until - datetime.now()).total_seconds()
                if wait_seconds > 0:
                    print(f"⏳ GLOBAL cooldown for {wait_seconds:.1f}s (IP flagged)")
                    time.sleep(wait_seconds)
            # Remove expired cooldown
            self.cooldown_until = None
        
        # Apply jitter to make delays more human-like
        jitter_factor = 1 + random.uniform(-self.jitter, self.jitter)
        jittered_delay = self.current_delay * jitter_factor
        jitter_pct = (jitter_factor - 1) * 100
        
        # Check time since last request
        if self.last_request_time > 0:
            elapsed = time.time() - self.last_request_time
            remaining = jittered_delay - elapsed
            if remaining > 0:
                flag_indicator = " 🚨 IP FLAGGED" if self.is_ip_flagged else ""
                print(f"⏱️  Waiting {remaining:.2f}s (base: {self.current_delay:.2f}s, jitter: {jitter_pct:+.1f}%){flag_indicator}")
                time.sleep(remaining)
        else:
            flag_indicator = " 🚨 IP FLAGGED" if self.is_ip_flagged else ""
            print(f"⏱️  Waiting {jittered_delay:.2f}s (base: {self.current_delay:.2f}s, jitter: {jitter_pct:+.1f}%){flag_indicator}")
            time.sleep(jittered_delay)
        
        # Update last request time
        self.last_request_time = time.time()
    
    def record_success(self, account: str = 'default'):
        """
        Record a successful request (GLOBAL)
        Reduces delay after success threshold is reached
        
        Args:
            account: Account identifier (kept for compatibility)
        """
        # Increment success counter
        self.consecutive_successes += 1
        
        # If we were flagged and now succeeding, gradually unflag
        if self.is_ip_flagged and self.consecutive_successes >= 3:
            self.is_ip_flagged = False
            print(f"✅ IP appears unflagged after {self.consecutive_successes} successes")
        
        # Check if we should reduce delay
        if self.consecutive_successes >= self.success_threshold:
            old_delay = self.current_delay
            self.current_delay = max(self.min_delay, self.current_delay - self.delay_reduction)
            self.consecutive_successes = 0  # Reset counter
            
            if self.current_delay < old_delay:
                print(f"✅ Reduced GLOBAL delay: {old_delay:.1f}s → {self.current_delay:.1f}s")
    
    def record_rate_limit(self, account: str = 'default', cooldown_seconds: int = 300):
        """
        Record a rate limit error (429) - GLOBAL impact
        Increases delay and sets cooldown period
        
        Args:
            account: Account identifier (kept for compatibility)
            cooldown_seconds: Cooldown duration in seconds (default: 5 minutes)
        """
        old_delay = self.current_delay
        self.current_delay = min(self.max_delay, self.current_delay * 2)
        self.consecutive_successes = 0
        self.is_ip_flagged = True
        
        # Set cooldown
        self.cooldown_until = datetime.now() + timedelta(seconds=cooldown_seconds)
        
        print(f"⚠️ GLOBAL rate limit (429): delay {old_delay:.1f}s → {self.current_delay:.1f}s, cooldown {cooldown_seconds}s")
        print(f"🚨 IP FLAGGED - All requests will slow down")
    
    def record_error(self, account: str = 'default'):
        """
        Record a 403 Forbidden error - AGGRESSIVE GLOBAL backoff
        Server has flagged your IP - jump to much longer delays
        
        Args:
            account: Account identifier (kept for compatibility)
        """
        old_delay = self.current_delay
        
        # AGGRESSIVE backoff on 403 - jump to forbidden_backoff delay
        if not self.is_ip_flagged:
            # First 403 - jump to forbidden_backoff
            self.current_delay = self.forbidden_backoff
            self.is_ip_flagged = True
            print(f"🚨 FIRST 403 - IP FLAGGED: delay {old_delay:.1f}s → {self.current_delay:.1f}s")
            print(f"⚠️ Server is tracking your IP - ALL requests will slow down significantly")
        else:
            # Already flagged - increase further
            self.current_delay = min(self.max_delay, self.current_delay * 1.5)
            print(f"🚨 REPEATED 403: delay {old_delay:.1f}s → {self.current_delay:.1f}s")
        
        self.consecutive_successes = 0
    
    def is_in_cooldown(self, account: str = 'default') -> bool:
        """
        Check if in cooldown (GLOBAL)
        
        Args:
            account: Account identifier (kept for compatibility)
            
        Returns:
            True if in cooldown, False otherwise
        """
        if not self.cooldown_until:
            return False
        
        if datetime.now() < self.cooldown_until:
            return True
        
        # Remove expired cooldown
        self.cooldown_until = None
        return False
    
    def get_cooldown_remaining(self, account: str = 'default') -> float:
        """
        Get remaining cooldown time (GLOBAL)
        
        Args:
            account: Account identifier (kept for compatibility)
            
        Returns:
            Remaining seconds in cooldown, or 0 if not in cooldown
        """
        if not self.is_in_cooldown(account):
            return 0.0
        
        remaining = (self.cooldown_until - datetime.now()).total_seconds()
        return max(0.0, remaining)
    
    def reset_account(self, account: str = 'default'):
        """
        Reset GLOBAL state
        
        Args:
            account: Account identifier (kept for compatibility)
        """
        self.current_delay = self.base_delay
        self.consecutive_successes = 0
        self.cooldown_until = None
        self.last_request_time = 0
        self.is_ip_flagged = False
        print(f"🔄 Reset GLOBAL rate limiter state")
    
    def get_stats(self, account: str = 'default') -> Dict[str, any]:
        """
        Get current GLOBAL stats
        
        Args:
            account: Account identifier (kept for compatibility)
            
        Returns:
            Dict with current delay, successes, cooldown status
        """
        return {
            'current_delay': self.current_delay,
            'consecutive_successes': self.consecutive_successes,
            'in_cooldown': self.is_in_cooldown(account),
            'cooldown_remaining': self.get_cooldown_remaining(account),
            'is_ip_flagged': self.is_ip_flagged
        }
