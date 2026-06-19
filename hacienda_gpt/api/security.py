"""API-edge security: optional API-key auth and a per-client rate limiter.

Both are **opt-in via environment** and disabled by default, so local dev and
the test suite keep working unauthenticated. Turn them on in any deployment that
exposes the API beyond localhost — ``/qa`` spends real OpenAI tokens and loads
multi-GB models per process, so an open endpoint is a cost/abuse footgun.

The limiter is a simple in-process fixed-window counter. That is correct for a
single-process uvicorn deployment (the project's target); behind multiple
workers each process keeps its own window, so set the per-process limit
accordingly or front the service with a shared limiter (nginx, API gateway).
"""

from __future__ import annotations

import hmac
import logging
import os
import threading
import time

from fastapi import Header, HTTPException, Request

logger = logging.getLogger(__name__)

_AUTH_DISABLED_WARNED = False


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    """FastAPI dependency enforcing the ``X-API-Key`` header when configured.

    When ``HACIENDA_API_KEY`` is unset, auth is disabled (a one-time warning is
    logged). When set, requests must present a matching key or get a 401. The
    comparison is constant-time to avoid leaking the key via timing.
    """
    expected = os.environ.get("HACIENDA_API_KEY")
    if not expected:
        global _AUTH_DISABLED_WARNED
        if not _AUTH_DISABLED_WARNED:
            logger.warning(
                "HACIENDA_API_KEY is not set — the API is UNAUTHENTICATED. "
                "Set it before exposing the service beyond localhost."
            )
            _AUTH_DISABLED_WARNED = True
        return
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


class RateLimiter:
    """Thread-safe fixed-window limiter keyed by an opaque client id.

    ``limit_per_min <= 0`` disables it entirely. Stale windows are pruned
    opportunistically so the backing dict can't grow without bound under a
    churn of distinct clients.
    """

    def __init__(self, limit_per_min: int) -> None:
        self.limit = limit_per_min
        self._lock = threading.Lock()
        self._windows: dict[str, tuple[int, int]] = {}

    def allow(self, client: str) -> bool:
        if self.limit <= 0:
            return True
        now_min = int(time.time() // 60)
        with self._lock:
            if len(self._windows) > 4096:
                self._windows = {k: v for k, v in self._windows.items() if v[0] == now_min}
            start, count = self._windows.get(client, (now_min, 0))
            if start != now_min:
                start, count = now_min, 0
            count += 1
            self._windows[client] = (start, count)
            return count <= self.limit


def client_key(request: Request) -> str:
    """Identify the caller for rate limiting: API key if present, else peer IP."""
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return f"key:{api_key}"
    client = request.client
    return f"ip:{client.host}" if client else "ip:unknown"


def make_rate_limit_middleware(limiter: RateLimiter):
    """Build a Starlette HTTP middleware that enforces ``limiter`` per client.

    Health and metrics endpoints are exempt so liveness probes and scrapers are
    never throttled. Returns 429 with a ``Retry-After`` hint when over budget.
    """
    from starlette.responses import JSONResponse

    exempt = {"/health", "/metrics"}

    async def rate_limit_middleware(request: Request, call_next):
        if request.url.path in exempt or limiter.allow(client_key(request)):
            return await call_next(request)
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded; retry shortly."},
            headers={"Retry-After": "60"},
        )

    return rate_limit_middleware
