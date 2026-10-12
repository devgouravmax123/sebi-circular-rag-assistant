"""
Thread-safe in-memory sliding-window token bucket rate limiter for FastAPI endpoints (Phase 10.1.20).

Characteristics:
- Pure Python with standard library collections and threading.Lock.
- No external dependencies (redis/slowapi not required).
- Supports rate per minute and burst limits per client IP.
- Extracts real client IP safely, taking first IP from X-Forwarded-For if behind trusted reverse proxy (Caddy).
- Emits RFC-compliant Retry-After header on HTTP 429.
- Never logs sensitive request payloads, query text, or credentials.
"""
from collections import defaultdict
import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException, Request, status

logger = logging.getLogger("rate_limiter")


class InMemoryRateLimiter:
    """
    Sliding window rate limiter tracking request timestamps per client key.
    """

    def __init__(self, requests_per_minute: int = 20, burst_limit: int = 10):
        self.requests_per_minute = max(1, requests_per_minute)
        self.burst_limit = max(1, burst_limit)
        self._window_seconds = 60.0
        self._lock = threading.Lock()
        # client_ip -> list of monotonic timestamps
        self._history: Dict[str, List[float]] = defaultdict(list)
        # Tracking last cleanup time to prune idle IP addresses periodically
        self._last_prune = time.monotonic()

    def _get_client_ip(self, request: Request) -> str:
        """
        Extracts and validates client IP under a trusted edge proxy architecture.

        Security Contract & Trust Assumptions:
        1. In production, FastAPI (api:8000) is isolated within sebi-internal-net and NEVER
           directly reachable from external networks.
        2. Caddy terminates external TLS/HTTP and explicitly overwrites both 'X-Real-IP'
           and 'X-Forwarded-For' with '{remote_host}' using header_up directives.
        3. Arbitrary client-supplied forwarding headers cannot be trusted. If 'X-Real-IP'
           is present, we validate it using the ipaddress module.
        4. If 'X-Real-IP' is absent, we inspect 'X-Forwarded-For' and take ONLY the right-most
           element (appended by the nearest trusted hop), validating it.
        5. If headers are missing, malformed, or invalid, we fall back to direct socket host
           (request.client.host) or a safe default ('127.0.0.1').
        """
        import ipaddress

        def _validate_ip(raw: Optional[str]) -> Optional[str]:
            if not raw or not raw.strip():
                return None
            val = raw.strip()
            try:
                # Validates both IPv4 and IPv6 syntax cleanly
                ip_obj = ipaddress.ip_address(val)
                return str(ip_obj)
            except ValueError:
                return None

        # 1. Prefer trusted proxy X-Real-IP header (overwritten by Caddy)
        real_ip = _validate_ip(request.headers.get("x-real-ip"))
        if real_ip:
            return real_ip

        # 2. Inspect X-Forwarded-For taking ONLY the right-most (nearest proxy) token
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            tokens = [t.strip() for t in forwarded_for.split(",") if t.strip()]
            if tokens:
                nearest_ip = _validate_ip(tokens[-1])
                if nearest_ip:
                    return nearest_ip

        # 3. Fallback to direct client socket host
        if request.client and request.client.host:
            socket_ip = _validate_ip(request.client.host)
            if socket_ip:
                return socket_ip

        return "127.0.0.1"

    def check_rate_limit(self, request: Request) -> None:
        """
        Evaluates current request against rate limit.
        Raises HTTPException(429) if exceeded with Retry-After header.
        """
        # If rate limiting is disabled (e.g. requests_per_minute <= 0)
        if self.requests_per_minute <= 0:
            return

        client_ip = self._get_client_ip(request)
        now = time.monotonic()
        window_start = now - self._window_seconds

        with self._lock:
            # Periodic housekeeping: purge old entries every 5 minutes
            if now - self._last_prune > 300.0:
                dead_ips = [
                    ip for ip, timestamps in self._history.items()
                    if not timestamps or timestamps[-1] < window_start
                ]
                for dead_ip in dead_ips:
                    del self._history[dead_ip]
                self._last_prune = now

            timestamps = self._history[client_ip]
            # Prune entries older than sliding window
            valid_timestamps = [ts for ts in timestamps if ts > window_start]
            self._history[client_ip] = valid_timestamps

            # Check window limit
            if len(valid_timestamps) >= self.requests_per_minute:
                oldest_ts = valid_timestamps[0]
                retry_after = max(1, int(oldest_ts + self._window_seconds - now) + 1)
                logger.warning(
                    f"Rate limit exceeded for client IP (sanitized): {client_ip[:8]}... "
                    f"Retry-After: {retry_after}s"
                )
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many requests. Please wait before submitting additional regulatory queries.",
                    headers={"Retry-After": str(retry_after)},
                )

            # Record this valid request
            valid_timestamps.append(now)

    def reset(self) -> None:
        """Clear all rate limit state (primarily for tests)."""
        with self._lock:
            self._history.clear()


_query_rate_limiter: Optional[InMemoryRateLimiter] = None


def get_query_rate_limiter(
    requests_per_minute: Optional[int] = None,
    burst_limit: Optional[int] = None,
) -> InMemoryRateLimiter:
    """Singleton getter for query rate limiter."""
    global _query_rate_limiter
    if _query_rate_limiter is None:
        from app.core.config import get_settings
        settings = get_settings()
        rpm = requests_per_minute if requests_per_minute is not None else settings.rate_limit_query_per_minute
        burst = burst_limit if burst_limit is not None else settings.rate_limit_query_burst
        _query_rate_limiter = InMemoryRateLimiter(requests_per_minute=rpm, burst_limit=burst)
    return _query_rate_limiter
