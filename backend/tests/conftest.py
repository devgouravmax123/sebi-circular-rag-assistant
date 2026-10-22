"""
Global pytest fixtures and test-environment isolation for the backend test suite.
"""
import pytest
from app.core.config import get_settings
from app.core.rate_limiter import get_query_rate_limiter


@pytest.fixture(autouse=True)
def isolate_query_rate_limiter():
    """
    Ensures in-memory query rate-limiter state does not leak between independent tests.
    Resets request history and restores default configuration before and after each test.
    """
    settings = get_settings()
    limiter = get_query_rate_limiter()
    limiter.reset()
    limiter.requests_per_minute = settings.rate_limit_query_per_minute
    limiter.burst_limit = settings.rate_limit_query_burst
    try:
        yield
    finally:
        limiter.reset()
        limiter.requests_per_minute = settings.rate_limit_query_per_minute
        limiter.burst_limit = settings.rate_limit_query_burst
