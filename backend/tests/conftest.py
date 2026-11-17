"""
Global pytest fixtures and test-environment isolation for the backend test suite.
"""
import os
import pytest

# Prevent accidental fallback to remote production/cloud database during test collection.
# If TEST_DATABASE_URL is not explicitly configured for local disposable testing,
# candidate database variables are neutralized so get_approved_test_db_url() returns None safely.
if not os.getenv("TEST_DATABASE_URL"):
    for _var in ("DATABASE_URL", "WORKER_DATABASE_URL", "DIRECT_DATABASE_URL", "MIGRATION_DATABASE_URL"):
        os.environ[_var] = ""

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
