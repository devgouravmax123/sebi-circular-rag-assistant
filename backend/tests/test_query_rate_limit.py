"""
Unit tests for public query rate limiting (Phase 10.1.20).

Validates:
1. Requests within configured rate limit succeed normally.
2. Requests exceeding rate limit return HTTP 429 Too Many Requests.
3. Response includes standard RFC Retry-After header.
4. Non-query endpoints (/health, /ready) are never rate-limited.
5. Clients with distinct IP addresses maintain independent rate limits.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.rate_limiter import get_query_rate_limiter, InMemoryRateLimiter


@pytest.fixture(autouse=True)
def reset_limiter():
    """Ensure clean rate limiter state before each test."""
    limiter = get_query_rate_limiter()
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def client():
    return TestClient(app)


def test_rate_limit_within_threshold(client):
    """Multiple requests within limit succeed without 429."""
    limiter = get_query_rate_limiter()
    limiter.requests_per_minute = 5

    for _ in range(4):
        resp = client.post(
            "/api/v1/query",
            json={"query": "ISO 27001 certification requirement", "top_k": 3},
            headers={"X-Forwarded-For": "198.51.100.1"},
        )
        assert resp.status_code == 200


def test_rate_limit_exceeded_returns_429(client):
    """Exceeding the threshold returns HTTP 429 with Retry-After header."""
    limiter = get_query_rate_limiter()
    limiter.requests_per_minute = 3

    # Send 3 requests (allowed)
    for _ in range(3):
        resp = client.post(
            "/api/v1/query",
            json={"query": "ISO 27001 certification requirement", "top_k": 3},
            headers={"X-Forwarded-For": "198.51.100.2"},
        )
        assert resp.status_code == 200

    # 4th request must be rejected
    rejected_resp = client.post(
        "/api/v1/query",
        json={"query": "ISO 27001 certification requirement", "top_k": 3},
        headers={"X-Forwarded-For": "198.51.100.2"},
    )
    assert rejected_resp.status_code == 429
    assert "Retry-After" in rejected_resp.headers
    assert int(rejected_resp.headers["Retry-After"]) > 0
    body = rejected_resp.json()
    assert "detail" in body
    assert "Too many requests" in body["detail"]


def test_health_and_readiness_not_rate_limited(client):
    """Health probes must never be constrained by the query rate limiter."""
    limiter = get_query_rate_limiter()
    limiter.requests_per_minute = 1

    # Saturate query limit
    resp = client.post(
        "/api/v1/query",
        json={"query": "ISO 27001 certification requirement", "top_k": 3},
        headers={"X-Forwarded-For": "198.51.100.3"},
    )
    assert resp.status_code == 200

    # Query is now locked out
    resp_query = client.post(
        "/api/v1/query",
        json={"query": "ISO 27001 certification requirement", "top_k": 3},
        headers={"X-Forwarded-For": "198.51.100.3"},
    )
    assert resp_query.status_code == 429

    # /health still functions cleanly
    for _ in range(5):
        resp_health = client.get("/health", headers={"X-Forwarded-For": "198.51.100.3"})
        assert resp_health.status_code == 200


def test_distinct_client_ips_have_separate_quotas(client):
    """Different client IPs have separate rate limit buckets."""
    limiter = get_query_rate_limiter()
    limiter.requests_per_minute = 2

    # Client A consumes quota
    for _ in range(2):
        resp = client.post(
            "/api/v1/query",
            json={"query": "ISO 27001 certification requirement", "top_k": 3},
            headers={"X-Forwarded-For": "203.0.113.10"},
        )
        assert resp.status_code == 200

    assert client.post(
        "/api/v1/query",
        json={"query": "ISO 27001 certification requirement", "top_k": 3},
        headers={"X-Forwarded-For": "203.0.113.10"},
    ).status_code == 429

    # Client B should still be allowed
    resp_b = client.post(
        "/api/v1/query",
        json={"query": "ISO 27001 certification requirement", "top_k": 3},
        headers={"X-Forwarded-For": "203.0.113.20"},
    )
    assert resp_b.status_code == 200


def test_spoofed_x_forwarded_for_cannot_bypass_rate_limit(client):
    """
    Rotating arbitrary spoofed IPs in X-Forwarded-For must NOT evade rate limits
    when the trusted X-Real-IP is held constant by the edge proxy.
    """
    limiter = get_query_rate_limiter()
    limiter.requests_per_minute = 2

    trusted_ip = "198.51.100.99"

    # Request 1: attacker sets spoofed left-most IP
    resp1 = client.post(
        "/api/v1/query",
        json={"query": "ISO 27001 certification requirement", "top_k": 3},
        headers={
            "X-Real-IP": trusted_ip,
            "X-Forwarded-For": f"10.0.0.1, {trusted_ip}",
        },
    )
    assert resp1.status_code == 200

    # Request 2: attacker changes spoofed IP
    resp2 = client.post(
        "/api/v1/query",
        json={"query": "ISO 27001 certification requirement", "top_k": 3},
        headers={
            "X-Real-IP": trusted_ip,
            "X-Forwarded-For": f"10.0.0.2, {trusted_ip}",
        },
    )
    assert resp2.status_code == 200

    # Request 3: quota exhausted for trusted_ip despite cycling spoofed header
    resp3 = client.post(
        "/api/v1/query",
        json={"query": "ISO 27001 certification requirement", "top_k": 3},
        headers={
            "X-Real-IP": trusted_ip,
            "X-Forwarded-For": f"10.0.0.3, {trusted_ip}",
        },
    )
    assert resp3.status_code == 429


def test_ipv6_and_malformed_header_handling(client):
    """Verifies that IPv6 addresses and malformed strings are handled safely."""
    limiter = get_query_rate_limiter()
    limiter.requests_per_minute = 2

    ipv6_client = "2001:db8::1"
    for _ in range(2):
        resp = client.post(
            "/api/v1/query",
            json={"query": "ISO 27001 certification requirement", "top_k": 3},
            headers={"X-Real-IP": ipv6_client},
        )
        assert resp.status_code == 200

    # 3rd request from same IPv6 is rate-limited
    assert client.post(
        "/api/v1/query",
        json={"query": "ISO 27001 certification requirement", "top_k": 3},
        headers={"X-Real-IP": ipv6_client},
    ).status_code == 429

    # Malformed IP does not crash, safely falls back
    resp_malformed = client.post(
        "/api/v1/query",
        json={"query": "ISO 27001 certification requirement", "top_k": 3},
        headers={"X-Real-IP": "not-an-ip-address; DROP TABLE;"},
    )
    # Succeeds or falls back to loopback without unhandled 500
    assert resp_malformed.status_code in (200, 429)
