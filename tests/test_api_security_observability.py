from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from hacienda_gpt.api import api
from hacienda_gpt.api.api import app
from hacienda_gpt.api.security import RateLimiter

client = TestClient(app)


# --------------------------- observability --------------------------------- #


def test_request_id_header_is_returned() -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.headers.get("X-Request-ID")


def test_request_id_is_echoed_when_provided() -> None:
    r = client.get("/health", headers={"X-Request-ID": "trace-123"})
    assert r.headers.get("X-Request-ID") == "trace-123"


def test_metrics_endpoint_exposes_prometheus_counts() -> None:
    client.get("/health")  # generate at least one request
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    body = r.text
    assert "hacienda_http_requests_total" in body
    # The route template (not an expanded id) keeps cardinality bounded.
    assert 'route="/health"' in body


# ------------------------------- auth -------------------------------------- #


def test_api_key_required_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HACIENDA_API_KEY", "s3cret")
    # Missing key → 401 before any work happens.
    r = client.post("/cases", json={"user_id": "u", "jurisdiction": "ES", "tax_period": "2025"})
    assert r.status_code == 401
    # Correct key → allowed through.
    r = client.post(
        "/cases",
        json={"user_id": "u", "jurisdiction": "ES", "tax_period": "2025"},
        headers={"X-API-Key": "s3cret"},
    )
    assert r.status_code == 200


def test_api_key_wrong_value_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HACIENDA_API_KEY", "s3cret")
    r = client.post(
        "/cases",
        json={"user_id": "u", "jurisdiction": "ES", "tax_period": "2025"},
        headers={"X-API-Key": "wrong"},
    )
    assert r.status_code == 401


def test_health_and_metrics_exempt_from_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HACIENDA_API_KEY", "s3cret")
    assert client.get("/health").status_code == 200
    assert client.get("/metrics").status_code == 200


def test_auth_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HACIENDA_API_KEY", raising=False)
    r = client.post("/cases", json={"user_id": "u", "jurisdiction": "ES", "tax_period": "2025"})
    assert r.status_code == 200


# --------------------------- rate limiting --------------------------------- #


def test_rate_limiter_unit_allows_then_blocks() -> None:
    limiter = RateLimiter(limit_per_min=2)
    assert limiter.allow("c") is True
    assert limiter.allow("c") is True
    assert limiter.allow("c") is False  # third in the same window is over budget
    assert limiter.allow("other") is True  # independent client


def test_rate_limiter_disabled_when_non_positive() -> None:
    limiter = RateLimiter(limit_per_min=0)
    assert all(limiter.allow("c") for _ in range(100))


def test_rate_limit_middleware_returns_429(monkeypatch: pytest.MonkeyPatch) -> None:
    # Tighten the live limiter the app already wired, then exhaust it.
    monkeypatch.setattr(api._rate_limiter, "limit", 1)
    monkeypatch.setattr(api._rate_limiter, "_windows", {})
    first = client.post("/cases", json={"user_id": "u", "jurisdiction": "ES", "tax_period": "2025"})
    assert first.status_code == 200
    blocked = client.post("/cases", json={"user_id": "u", "jurisdiction": "ES", "tax_period": "2025"})
    assert blocked.status_code == 429
    assert blocked.headers.get("Retry-After") == "60"
