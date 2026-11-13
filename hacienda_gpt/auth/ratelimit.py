"""In-process limiter for failed login attempts.

Correct for the single-process deployment the project targets. Behind several
workers each process keeps its own counters, and behind a reverse proxy every
client shares the proxy's address unless uvicorn is started with
``--proxy-headers`` — front the service with a shared limiter in those cases.
"""

from __future__ import annotations

from collections.abc import Callable
import threading
import time

_MAX_TRACKED_KEYS = 10_000


class LoginRateLimiter:
    """Sliding window of FAILED attempts per key (e-mail + client address)."""

    def __init__(
        self, max_attempts: int, window_seconds: float, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    def _recent(self, key: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(key, []) if now - t < self.window_seconds]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def retry_after(self, key: str) -> int:
        """Seconds until ``key`` may try again; 0 when it is not blocked."""
        if self.max_attempts <= 0:
            return 0
        now = self._clock()
        with self._lock:
            recent = self._recent(key, now)
            if len(recent) < self.max_attempts:
                return 0
            return max(1, int(self.window_seconds - (now - recent[0])) + 1)

    def record_failure(self, key: str) -> None:
        now = self._clock()
        with self._lock:
            if len(self._failures) > _MAX_TRACKED_KEYS:
                for stale in [k for k in self._failures if k != key]:
                    self._recent(stale, now)
            self._failures[key] = [*self._recent(key, now), now]

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._failures.clear()
            else:
                self._failures.pop(key, None)
