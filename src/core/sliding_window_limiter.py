import time
from collections import defaultdict, deque
from dataclasses import dataclass, field


@dataclass
class WindowConfig:
    name: str
    seconds: int
    max_ops: int


DEFAULT_WINDOWS = [
    WindowConfig("1h", 3600, 200),
    WindowConfig("3h", 10800, 400),
    WindowConfig("1d", 86400, 1000),
]


class SlidingWindowRateLimiter:

    def __init__(self, windows: list[WindowConfig] | None = None):
        self._windows = windows or DEFAULT_WINDOWS
        self._timestamps: dict[str, deque[float]] = defaultdict(deque)

    def _key(self, domain: str, op_type: str, window_name: str) -> str:
        return f"{domain}:{op_type}:{window_name}"

    def _prune(self, key: str, cutoff: float):
        q = self._timestamps[key]
        while q and q[0] < cutoff:
            q.popleft()

    def check(self, domain: str, op_type: str = "default") -> bool:
        now = time.monotonic()
        for w in self._windows:
            key = self._key(domain, op_type, w.name)
            self._prune(key, now - w.seconds)
            if len(self._timestamps[key]) >= w.max_ops:
                return False
        return True

    def record(self, domain: str, op_type: str = "default"):
        now = time.monotonic()
        for w in self._windows:
            key = self._key(domain, op_type, w.name)
            self._prune(key, now - w.seconds)
            self._timestamps[key].append(now)

    def time_until_allowed(self, domain: str, op_type: str = "default") -> float:
        now = time.monotonic()
        max_wait = 0.0
        for w in self._windows:
            key = self._key(domain, op_type, w.name)
            self._prune(key, now - w.seconds)
            q = self._timestamps[key]
            if len(q) >= w.max_ops:
                oldest = q[0]
                wait = (oldest + w.seconds) - now
                max_wait = max(max_wait, wait)
        return max_wait

    def get_usage(self, domain: str, op_type: str = "default") -> dict[str, tuple[int, int]]:
        now = time.monotonic()
        result = {}
        for w in self._windows:
            key = self._key(domain, op_type, w.name)
            self._prune(key, now - w.seconds)
            result[w.name] = (len(self._timestamps[key]), w.max_ops)
        return result

    def reset(self, domain: str | None = None, op_type: str | None = None):
        if domain is None and op_type is None:
            self._timestamps.clear()
            return
        keys_to_remove = []
        for key in self._timestamps:
            parts = key.split(":")
            if domain and parts[0] != domain:
                continue
            if op_type and parts[1] != op_type:
                continue
            keys_to_remove.append(key)
        for key in keys_to_remove:
            del self._timestamps[key]
