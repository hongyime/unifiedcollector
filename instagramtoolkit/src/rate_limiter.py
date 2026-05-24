"""Centralized rate limiting utilities for Instagram operations.

Provides a single place to adjust delays and backoff strategies instead of
sprinkling time.sleep/random.uniform directly across modules.

All sleep calls print human-readable status lines so the operator can see
exactly what the toolkit is doing and why — mimicking real human behaviour.
"""
from __future__ import annotations

import math
import threading
import time
import random
import datetime
from typing import Optional
from src.config import (
    MIN_DELAY, MAX_DELAY,
    MIN_RANDOM_DELAY, MAX_RANDOM_DELAY,
    HUMAN_REST_INTERVAL, HUMAN_REST_CHANCE,
    HUMAN_REST_MIN, HUMAN_REST_MAX,
    OPS_BEFORE_BREAK_MIN, OPS_BEFORE_BREAK_MAX,
    BREAK_DURATION_MIN, BREAK_DURATION_MAX,
    ENUM_PAUSE_MIN, ENUM_PAUSE_MAX,
    SMART_SCHEDULING_ENABLED, SAFE_HOURS, RISKY_HOURS, RISKY_HOUR_DELAY_MULTIPLIER,
    NIGHT_HOURS, NIGHT_DELAY_MULTIPLIER_MIN, NIGHT_DELAY_MULTIPLIER_MAX,
    MICRO_PAUSE_MIN, MICRO_PAUSE_MAX, MICRO_PAUSE_PROBABILITY,
    DISTRIBUTION_GAUSSIAN_WEIGHT, DISTRIBUTION_UNIFORM_WEIGHT, DISTRIBUTION_EXPONENTIAL_WEIGHT,
    DELAY_RANGE_VARIATION,
    ENUM_PAUSE_INTERVAL_MIN, ENUM_PAUSE_INTERVAL_MAX,
    ENUM_PAUSE_DURATION_MIN, ENUM_PAUSE_DURATION_MAX,
    CONTENT_AWARE_ENABLED, CONTENT_AWARE_MAX_MULTIPLIER,
)

# Module-level shutdown event — set this to wake all interruptible_sleep calls immediately.
_SHUTDOWN_EVENT = threading.Event()

# Operation-specific delay strategies — each defines how delays differ per operation type.
# base_range_multiplier: scales the delay range (>1 = longer, <1 = shorter)
# distribution_override: forces a specific distribution instead of the random-weighted pick
# micro_pause_frequency: overrides MICRO_PAUSE_PROBABILITY for this operation type
_OPERATION_STRATEGIES: dict[str, dict] = {
    'profile_view': {
        'base_range_multiplier': 1.2,
        'distribution_override': 'gaussian',
        'micro_pause_frequency': 0.9,
    },
    'list_scroll': {
        'base_range_multiplier': 0.8,
        'distribution_override': 'uniform',
        'micro_pause_frequency': 0.5,
    },
    'media_download': {
        'base_range_multiplier': 1.0,
        'distribution_override': 'exponential',
        'micro_pause_frequency': 0.3,
    },
    'account_switch': {
        'base_range_multiplier': 2.0,
        'distribution_override': 'gaussian',
        'micro_pause_frequency': 0.8,
    },
}

# ── Human-behaviour message pools ────────────────────────────────────────────
# Each category has a pool of messages that are picked randomly so the output
# never looks like a script.

_MSG_SHORT_DELAY = [
    "Pausing briefly — like a human glancing at the screen before the next tap…",
    "Short breather between requests — keeping it natural…",
    "Simulating reading time before the next action…",
    "Brief pause — humans don't click instantly…",
    "Micro-rest between API calls — staying under the radar…",
    "Waiting a moment — mimicking natural scroll behaviour…",
]

_MSG_USER_DELAY = [
    "Moving to the next profile — taking a natural break first…",
    "Switching users — pausing like a human would between searches…",
    "Inter-profile delay — real users don't batch-process at machine speed…",
    "Resting between profiles — simulating human attention span…",
    "Natural gap before next user — avoiding bot-like cadence…",
]

_MSG_ENUM_PAUSE = [
    "Enumeration checkpoint — humans scroll in bursts, not continuously…",
    "Pausing mid-list — mimicking a human taking a break while scrolling followers…",
    "Follower list rest — real users stop and look around occasionally…",
    "Scroll fatigue simulation — taking a breather mid-enumeration…",
    "Natural pause in list traversal — keeping request patterns irregular…",
]

_MSG_LONG_BREAK = [
    "Taking a longer break — simulating a human stepping away from the screen…",
    "Extended rest period — mimicking a coffee break or distraction…",
    "Human-style session pause — real users don't run for hours non-stop…",
    "Scheduled downtime — keeping session length within human norms…",
    "Long rest before continuing — simulating natural usage patterns…",
]

_MSG_HUMAN_REST = [
    "Spontaneous rest — humans randomly stop and do something else…",
    "Unplanned break — mimicking a notification distraction…",
    "Random idle period — keeping behaviour unpredictable…",
    "Simulating a human getting distracted mid-session…",
]

_MSG_RISKY_HOUR = [
    "Business hours detected — increasing delays to reduce detection risk…",
    "Peak-traffic window — slowing down to blend in with normal usage…",
    "High-activity period — applying extra caution during business hours…",
]

_MSG_SAFE_HOUR = [
    "Off-peak hours — safe window for slightly faster operation…",
    "Low-traffic period — operating at normal pace…",
    "Night/early-morning window — reduced detection risk…",
]

_MSG_ACCOUNT_SWITCH = [
    "Switching accounts — adding a realistic delay between sessions…",
    "Account rotation — pausing so Instagram doesn't see instant switching…",
    "Changing active account — mimicking a human logging out and back in…",
]

_MSG_EMERGENCY = [
    "Rate limit detected — backing off hard to avoid a ban…",
    "Instagram pushed back — entering emergency cooldown…",
    "Throttle signal received — taking a long break before retrying…",
]


def _pick(pool: list[str]) -> str:
    return random.choice(pool)


def _fmt_duration(seconds: float) -> str:
    """Format seconds into a human-readable string like '2m 34s' or '45s'."""
    seconds = int(seconds)
    if seconds >= 60:
        m, s = divmod(seconds, 60)
        return f"{m}m {s:02d}s"
    return f"{seconds}s"


def _resume_at(seconds: float) -> str:
    """Return a 'resuming at HH:MM:SS' string for long waits."""
    resume = datetime.datetime.now() + datetime.timedelta(seconds=seconds)
    return resume.strftime("%H:%M:%S")


# ── Core sleep ────────────────────────────────────────────────────────────────

def _interruptible_sleep(
    seconds: float,
    label: str = "",
    message: str = "",
    show_countdown: bool = False,
    check_interval: float = 0.2,
) -> None:
    """Sleep in short slices so Ctrl+C or _SHUTDOWN_EVENT wakes it immediately.

    Args:
        seconds:        Total sleep time.
        label:          Short tag shown in brackets, e.g. 'spider' or 'scan'.
        message:        Human-readable reason for the sleep.
        show_countdown: If True, print a countdown line for long waits (≥30s).
        check_interval: How often to poll the shutdown event.
    """
    if seconds <= 0:
        return

    tag = f"[{label.upper()}]" if label else "[WAIT]"
    duration_str = _fmt_duration(seconds)

    if message:
        print(f"{tag} {message}")

    if show_countdown and seconds >= 30:
        print(f"{tag} ⏳ Waiting {duration_str} — resuming at {_resume_at(seconds)}")
    elif not message:
        print(f"{tag} ⏳ {duration_str}")

    end_time = time.time() + seconds
    last_countdown = int(seconds)

    while True:
        if _SHUTDOWN_EVENT.is_set():
            print(f"{tag} ⚡ Shutdown requested — skipping remaining wait")
            return
        remaining = end_time - time.time()
        if remaining <= 0:
            return

        # Print countdown ticks for long waits (every 30s)
        if show_countdown and seconds >= 60:
            remaining_int = int(remaining)
            if remaining_int != last_countdown and remaining_int % 30 == 0 and remaining_int > 0:
                print(f"{tag}    … {_fmt_duration(remaining_int)} remaining")
                last_countdown = remaining_int

        time.sleep(min(check_interval, remaining))


# ── RateLimiter ───────────────────────────────────────────────────────────────

class RateLimiter:
    """Rate limiter with human-behaviour simulation and clear status output.
    
    Now includes sliding window rate limiting to prevent account bans.

    Every delay prints a message explaining *why* the toolkit is waiting,
    making it obvious to the operator that the tool is behaving like a human.

    Usage patterns:
      limiter.short_delay()                 # between lightweight API calls
      limiter.user_delay()                  # between processing different users
      limiter.periodic(count, every=12)     # enumeration checkpoint pause
      limiter.emergency_break(minutes=5)    # manual backoff on hard rate-limit
      limiter.track_operation()             # auto long-break after random N ops
      limiter.account_switch_delay()        # between account rotations
      limiter.check_sliding_window_limit(account_name, 'action')  # enforce sliding window limits
    """

    def __init__(
        self,
        min_delay: float = MIN_DELAY,
        max_delay: float = MAX_DELAY,
        label: str = "general",
        rate_limit_repo=None,
    ):
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.label = label
        # Randomise thresholds so the pattern is never identical between runs
        self._ops_before_long_pause = random.randint(OPS_BEFORE_BREAK_MIN, OPS_BEFORE_BREAK_MAX)
        self._long_pause_minutes = random.randint(BREAK_DURATION_MIN, BREAK_DURATION_MAX)
        self._op_counter = 0
        self._session_start = time.time()
        self._total_ops = 0
        # Interval (in items) at which the next enumeration pause fires (task 3.2).
        # Re-randomized after each pause so the cadence is never predictable.
        self._next_enum_pause = random.randint(ENUM_PAUSE_INTERVAL_MIN, ENUM_PAUSE_INTERVAL_MAX)
        # Sliding window rate limit repository (optional - can be None for backward compat)
        self._rate_limit_repo = rate_limit_repo

    # ── Internal helpers ─────────────────────────────────────────────────

    def _sleep(
        self,
        seconds: float,
        message: str = "",
        show_countdown: bool = False,
    ) -> None:
        _interruptible_sleep(
            seconds,
            label=self.label,
            message=message,
            show_countdown=show_countdown,
        )

    def _human_delay(self, mean: float, stddev: float = 0.0, distribution: str = 'gaussian') -> float:
        """Delay clamped to [mean/3, mean*3] using the specified distribution.

        Args:
            mean:         Centre of the delay range.
            stddev:       Standard deviation (used for gaussian; ignored otherwise).
            distribution: One of 'gaussian', 'uniform', or 'exponential'.
        """
        if distribution == 'exponential':
            return self._exponential_delay(mean)
        if distribution == 'uniform':
            low = mean * 0.5
            high = mean * 1.5
            return random.uniform(low, high)
        # Default: gaussian
        if stddev <= 0:
            stddev = mean * 0.3
        delay = random.gauss(mean, stddev)
        return max(mean / 3, min(delay, mean * 3))

    def _variable_delay_range(self) -> tuple[float, float]:
        """Return slightly varied min/max for each call (±DELAY_RANGE_VARIATION).

        Prevents fixed delay range pattern by introducing per-call variation.
        """
        variation = DELAY_RANGE_VARIATION
        min_var = self.min_delay * (1 + random.uniform(-variation, variation))
        max_var = self.max_delay * (1 + random.uniform(-variation, variation))
        # Ensure min < max and both are positive
        min_var = max(1.0, min_var)
        max_var = max(min_var + 1.0, max_var)
        return min_var, max_var

    def _choose_distribution(self) -> str:
        """Randomly select a distribution type based on configured weights.

        Returns 'gaussian', 'uniform', or 'exponential'.
        Weights: 60% Gaussian, 30% Uniform, 10% Exponential.
        """
        r = random.random()
        if r < DISTRIBUTION_GAUSSIAN_WEIGHT:
            return 'gaussian'
        elif r < DISTRIBUTION_GAUSSIAN_WEIGHT + DISTRIBUTION_UNIFORM_WEIGHT:
            return 'uniform'
        else:
            return 'exponential'

    def _exponential_delay(self, mean: float) -> float:
        """Generate delay using exponential distribution for occasional long pauses.

        Exponential distribution produces mostly short delays with occasional long ones,
        mimicking human distraction patterns.
        """
        # Exponential distribution: -mean * ln(uniform(0,1))
        u = random.random()
        if u <= 0:
            u = 1e-10
        delay = -mean * math.log(u)
        # Clamp to [mean/4, mean*4] to avoid extreme values
        return max(mean / 4, min(delay, mean * 4))

    def _gaussian_delay(self, min_val: float, max_val: float) -> float:
        """Generate delay using Gaussian distribution with variable range.

        Enhanced version that works with variable ranges from _variable_delay_range().
        """
        mean = (min_val + max_val) / 2
        stddev = (max_val - min_val) / 4  # 95% of values within range
        delay = random.gauss(mean, stddev)
        return max(min_val * 0.5, min(delay, max_val * 1.5))

    def _micro_pause(self) -> float:
        """Generate a micro-pause delay (0.5-3s) using exponential distribution.

        Simulates human thinking/reading time between operations.
        Returns the pause duration in seconds.
        """
        # Use exponential for natural distribution (mostly short, occasionally longer)
        mean = (MICRO_PAUSE_MIN + MICRO_PAUSE_MAX) / 2
        delay = self._exponential_delay(mean)
        return max(MICRO_PAUSE_MIN, min(delay, MICRO_PAUSE_MAX))

    def get_delay_multiplier(self) -> float:
        """Return a multiplier based on time-of-day. Night hours use a fresh random
        draw per hour so there's no fixed pattern Instagram can fingerprint."""
        if not SMART_SCHEDULING_ENABLED:
            return 1.0
        t = time.localtime()
        hour = t.tm_hour
        if hour in NIGHT_HOURS:
            # Cache key = (yday, hour) so the multiplier is stable within each hour
            # but freshly drawn the next hour — no fixed schedule.
            cache_key = (t.tm_yday, hour)
            if not hasattr(self, '_night_mult_cache') or self._night_mult_cache[0] != cache_key:
                mult = random.uniform(NIGHT_DELAY_MULTIPLIER_MIN, NIGHT_DELAY_MULTIPLIER_MAX)
                self._night_mult_cache = (cache_key, mult)
            return self._night_mult_cache[1]
        if hour in RISKY_HOURS:
            return RISKY_HOUR_DELAY_MULTIPLIER
        return 1.0

    def _time_of_day_note(self) -> str:
        """Return a note about current time-of-day risk level."""
        if not SMART_SCHEDULING_ENABLED:
            return ""
        hour = time.localtime().tm_hour
        if hour in NIGHT_HOURS:
            mult = self.get_delay_multiplier()
            return f"  (🌙 night hours — {mult:.1f}x delay)"
        if hour in RISKY_HOURS:
            return f"  ({_pick(_MSG_RISKY_HOUR)})"
        if hour in SAFE_HOURS:
            return f"  ({_pick(_MSG_SAFE_HOUR)})"
        return ""

    def _session_stats(self) -> str:
        """Return a compact session stats string."""
        elapsed = int(time.time() - self._session_start)
        m, s = divmod(elapsed, 60)
        return f"session {m}m{s:02d}s | {self._total_ops} ops"

    # ── Public delay methods ─────────────────────────────────────────────

    def short_delay(self, operation_type: Optional[str] = None) -> None:
        """Short jittered delay between lightweight API calls."""
        strategy = _OPERATION_STRATEGIES.get(operation_type or '', {})
        min_val, max_val = self._variable_delay_range()
        base_mult = strategy.get('base_range_multiplier', 1.0)
        mean = (min_val + max_val) / 2 * base_mult
        smart_mult = self.get_delay_multiplier()
        distribution = strategy.get('distribution_override') or self._choose_distribution()
        delay = self._human_delay(mean * smart_mult, distribution=distribution)
        note = self._time_of_day_note()
        self._sleep(
            delay,
            message=f"{_pick(_MSG_SHORT_DELAY)}{note}",
        )

    def user_delay(self, multiplier: float = 1.0, operation_type: Optional[str] = None) -> None:
        """Longer delay between processing different users."""
        strategy = _OPERATION_STRATEGIES.get(operation_type or '', {})
        min_val, max_val = self._variable_delay_range()
        base_mult = strategy.get('base_range_multiplier', 1.0)
        mean = (min_val + max_val) / 2 * multiplier * base_mult
        smart_mult = self.get_delay_multiplier()
        distribution = strategy.get('distribution_override') or self._choose_distribution()
        delay = self._human_delay(mean * smart_mult, distribution=distribution)
        note = self._time_of_day_note()
        self._sleep(
            delay,
            message=f"{_pick(_MSG_USER_DELAY)}{note}",
            show_countdown=delay >= 30,
        )

    def periodic(self, current_index: int, every: int = 10, seconds: float = 10) -> None:
        """Longer pause at variable intervals — simulates human scroll fatigue.

        The `every` parameter is intentionally ignored; `_next_enum_pause` controls
        the actual interval (10–15 items, re-randomized after each pause) so the
        pattern is never the same between runs or even between pauses.
        """
        if current_index > 0 and current_index % self._next_enum_pause == 0:
            delay = random.uniform(ENUM_PAUSE_DURATION_MIN, ENUM_PAUSE_DURATION_MAX)
            self._sleep(
                delay,
                message=(
                    f"{_pick(_MSG_ENUM_PAUSE)}\n"
                    f"[{self.label.upper()}]    📊 {current_index} items processed so far"
                ),
                show_countdown=True,
            )
            # Randomize the next pause interval so it never falls on a fixed cadence
            self._next_enum_pause = random.randint(ENUM_PAUSE_INTERVAL_MIN, ENUM_PAUSE_INTERVAL_MAX)

    def account_switch_delay(self) -> None:
        """Mandatory pause between account rotations."""
        from src.config import ACCOUNT_SWITCH_DELAY_MIN, ACCOUNT_SWITCH_DELAY_MAX
        delay = random.uniform(ACCOUNT_SWITCH_DELAY_MIN, ACCOUNT_SWITCH_DELAY_MAX)
        self._sleep(
            delay,
            message=_pick(_MSG_ACCOUNT_SWITCH),
            show_countdown=True,
        )

    def micro_pause(self) -> None:
        """Silent micro-pause (0.5–3s) simulating human thinking/reading time."""
        delay = self._micro_pause()
        _interruptible_sleep(delay, label=self.label, check_interval=0.1)

    def content_aware_delay(
        self,
        post_count: int = 0,
        follower_count: int = 0,
        media_complexity: int = 0,
        operation_type: Optional[str] = None,
    ) -> None:
        """Delay scaled by content volume — more content means a longer wait.

        Applies formula: base_delay × (1 + log10(content_metric) × 0.1),
        capped at CONTENT_AWARE_MAX_MULTIPLIER (2×).
        Falls back to user_delay() when content-aware mode is disabled.
        """
        if not CONTENT_AWARE_ENABLED:
            self.user_delay(operation_type=operation_type)
            return
        content_metric = max(post_count, follower_count, media_complexity, 1)
        multiplier = 1.0 + math.log10(content_metric) * 0.1
        multiplier = min(multiplier, CONTENT_AWARE_MAX_MULTIPLIER)
        self.user_delay(multiplier=multiplier, operation_type=operation_type)

    def emergency_break(self, minutes: int) -> None:
        """Hard backoff after a rate-limit signal."""
        delay = minutes * 60
        self._sleep(
            delay,
            message=(
                f"{_pick(_MSG_EMERGENCY)}\n"
                f"[{self.label.upper()}]    🛑 Emergency break: {minutes} minutes"
            ),
            show_countdown=True,
        )

    # ── Composite behaviour ──────────────────────────────────────────────

    def track_operation(self) -> None:
        """Track an operation; insert micro-pauses and occasional long breaks automatically.

        The break threshold is randomised each time so the pattern is never
        predictable — just like a real human session.
        """
        self._op_counter += 1
        self._total_ops += 1

        # Micro-pause with configured probability (default 70%) — simulates
        # the brief reading/thinking time humans have between actions.
        if random.random() < MICRO_PAUSE_PROBABILITY:
            self.micro_pause()

        # Occasional spontaneous human rest (random chance)
        if (self._op_counter % HUMAN_REST_INTERVAL == 0
                and random.random() < HUMAN_REST_CHANCE):
            rest = random.uniform(HUMAN_REST_MIN, HUMAN_REST_MAX)
            self._sleep(
                rest,
                message=(
                    f"{_pick(_MSG_HUMAN_REST)}\n"
                    f"[{self.label.upper()}]    📈 {self._session_stats()}"
                ),
                show_countdown=True,
            )

        # Scheduled long break after N operations
        if self._op_counter >= self._ops_before_long_pause:
            break_secs = self._long_pause_minutes * 60
            self._sleep(
                break_secs,
                message=(
                    f"{_pick(_MSG_LONG_BREAK)}\n"
                    f"[{self.label.upper()}]    📈 {self._session_stats()} | "
                    f"next break in ~{self._ops_before_long_pause} ops"
                ),
                show_countdown=True,
            )
            # Randomise next window
            self._op_counter = 0
            self._ops_before_long_pause = random.randint(OPS_BEFORE_BREAK_MIN, OPS_BEFORE_BREAK_MAX)
            self._long_pause_minutes = random.randint(BREAK_DURATION_MIN, BREAK_DURATION_MAX)

    # ── Backward-compat alias ────────────────────────────────────────────

    def interruptible_sleep(
        self,
        seconds: float,
        reason: Optional[str] = None,
        check_interval: float = 0.2,
    ) -> None:
        """Legacy alias used by older call sites."""
        _interruptible_sleep(
            seconds,
            label=self.label,
            message=reason or "",
            show_countdown=seconds >= 30,
            check_interval=check_interval,
        )

    # ── Sliding Window Rate Limiting ─────────────────────────────────────

    def check_sliding_window_limit(
        self,
        account: str,
        request_type: str = 'action',
    ) -> bool:
        """Check sliding window limits and enforce wait if needed.

        This method checks all configured time windows (1h, 3h, 5h, 1d) and
        will block execution until limits clear if any window is at capacity.

        Args:
            account: Instagram account name making the request
            request_type: Type of request ('profile_view', 'download', 'action')

        Returns:
            True if request can proceed, False if limit was hit (but wait has been enforced)
        """
        if not self._rate_limit_repo:
            return True  # Rate limiting disabled

        can_make, wait_info = self._rate_limit_repo.can_make_request(account, request_type)

        if not can_make:
            wait_seconds = wait_info['wait_seconds']
            limiting_window = wait_info['limiting_window']
            wait_until = wait_info['wait_until']
            current_counts = wait_info['current_counts']

            print(f"[RATE LIMIT] ⚠️  Account '{account}' hit {limiting_window} limit")
            print(f"[RATE LIMIT] 📊 Current usage: {current_counts}")
            print(f"[RATE LIMIT] ⏳  Waiting {_fmt_duration(wait_seconds)} until {wait_until}")
            print(f"[RATE LIMIT] 🛑  This enforcement prevents account bans")

            # Hard block: wait until limit clears
            self.interruptible_sleep(wait_seconds, reason="Rate limit cooldown", show_countdown=True)

            print(f"[RATE LIMIT] ✅ Limit cleared, resuming for account '{account}'")

        return True

    def record_request(
        self,
        account: str,
        request_type: str = 'action',
        success: bool = True,
    ) -> None:
        """Record a request timestamp after execution.

        Only records successful requests to avoid penalizing failures.

        Args:
            account: Instagram account name
            request_type: Type of request ('profile_view', 'download', 'action')
            success: Only record if True (default)
        """
        if not self._rate_limit_repo:
            return  # Rate limiting disabled

        if success:
            self._rate_limit_repo.record_request(account, request_type)

    def get_rate_limit_summary(self, account: str) -> Optional[dict]:
        """Get current rate limit usage summary for account.

        Args:
            account: Instagram account name

        Returns:
            Dict with usage stats for each window, or None if rate limiting disabled
        """
        if not self._rate_limit_repo:
            return None

        return self._rate_limit_repo.get_usage_summary(account)

    def show_rate_limit_status(self, account: str) -> None:
        """Print current rate limit status for account.

        Shows usage across all windows with progress bars.

        Args:
            account: Instagram account name
        """
        if not self._rate_limit_repo:
            print("[RATE LIMIT] Disabled (SLIDING_WINDOW_ENABLED=false)")
            return

        summary = self.get_rate_limit_summary(account)
        if not summary:
            print(f"[RATE LIMIT] No data for account '{account}'")
            return

        print(f"\n{'='*60}")
        print(f"📊 Rate Limit Status: {account}")
        print(f"{'='*60}")

        for window_name, stats in summary.items():
            count = stats['count']
            limit = stats['limit']
            percentage = stats['percentage']
            remaining = stats['remaining']

            # Create progress bar
            bar_width = 40
            filled = int(bar_width * percentage / 100)
            bar = '█' * filled + '░' * (bar_width - filled)

            # Color indicator
            if percentage >= 90:
                emoji = '🔴'
            elif percentage >= 70:
                emoji = '🟡'
            else:
                emoji = '🟢'

            print(f"{emoji} {window_name:>2s}: {count:>4}/{limit:<4} [{bar}] {percentage:>5.1f}%")
            if remaining > 0:
                print(f"        Remaining: {remaining} requests")
            else:
                print(f"        ⚠️  AT LIMIT")

        print(f"{'='*60}\n")


__all__ = [
    "RateLimiter",
    "_SHUTDOWN_EVENT",
    "_interruptible_sleep",
    "_OPERATION_STRATEGIES",
]
