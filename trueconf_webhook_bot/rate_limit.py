"""Per-key sliding-window rate limiter (in-memory)."""

from __future__ import annotations

import time
from collections import defaultdict, deque


class SlidingWindowRateLimiter:
    """Limit `limit` hits per `window_seconds` window for every key.

    Keeps a deque of timestamps per key. On each call, stale entries are
    discarded; if the remaining deque is shorter than the limit, a new entry
    is appended and the request is allowed.

    Thread safety: used from a single event loop, no locking required.
    """

    def __init__(self, limit: int, window_seconds: float = 60.0):
        if limit <= 0:
            raise ValueError("limit must be positive")
        self._limit = limit
        self._window = float(window_seconds)
        self._timestamps: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        """Allow or deny a request. True means allowed (and counted)."""
        now = time.monotonic()
        window_start = now - self._window
        bucket = self._timestamps[key]
        while bucket and bucket[0] < window_start:
            bucket.popleft()
        if len(bucket) >= self._limit:
            return False
        bucket.append(now)
        return True

    def retry_after(self, key: str) -> float:
        """Seconds the caller should wait before the next request for this key is allowed."""
        bucket = self._timestamps.get(key)
        if not bucket:
            return 0.0
        return max(0.0, self._window - (time.monotonic() - bucket[0]))
