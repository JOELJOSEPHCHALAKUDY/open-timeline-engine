from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Lock
from time import time


@dataclass(slots=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    reset_in_seconds: int


class InMemoryRateLimiter:
    def __init__(self, limit: int, window_seconds: int = 60) -> None:
        self.limit = max(1, limit)
        self.window_seconds = max(1, window_seconds)
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str) -> RateLimitDecision:
        now = time()
        with self._lock:
            bucket = self._events[key]
            cutoff = now - self.window_seconds
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                reset_in = int(max(0, self.window_seconds - (now - bucket[0])))
                return RateLimitDecision(allowed=False, remaining=0, reset_in_seconds=reset_in)
            bucket.append(now)
            remaining = max(0, self.limit - len(bucket))
            return RateLimitDecision(
                allowed=True,
                remaining=remaining,
                reset_in_seconds=self.window_seconds,
            )
