"""
Shared test database target guard and isolation verifier.

Guarantees that destructive database integration tests can execute ONLY against
the explicitly approved local disposable test database target:
  - Host: localhost or 127.0.0.1
  - Port: 5432 (or default 5432)
  - Database name: sebi_rag_test (exact match)

Strictly rejects remote hosts, cloud providers (Render, Supabase, Neon, etc.),
non-standard ports, non-approved database names, and missing configurations.
Redacts all credentials from error messages and string representations.
"""
from __future__ import annotations

import os
from typing import Optional
from urllib.parse import urlparse, urlunparse


class SafeTestDatabaseError(RuntimeError):
    """Raised when an unapproved or unsafe database URL is provided for destructive tests."""
    pass


APPROVED_HOSTS = frozenset({"localhost", "127.0.0.1"})
APPROVED_PORT = 5432
APPROVED_DATABASE = "sebi_rag_test"
APPROVED_SCHEMES = frozenset({
    "postgresql",
    "postgresql+psycopg",
    "postgresql+psycopg2",
    "postgresql+asyncpg",
})


def redact_db_url(url: str) -> str:
    """
    Returns the database URL with user credentials completely redacted.
    Never exposes passwords, tokens, or usernames in exceptions or logs.
    """
    if not url or not isinstance(url, str):
        return "[EMPTY_URL]"
    try:
        parsed = urlparse(url.strip())
        if not parsed.scheme:
            return "[MALFORMED_URL]"
        netloc = parsed.netloc
        if "@" in netloc:
            host_part = netloc.split("@", 1)[1]
            netloc = f"***:***@{host_part}"
        return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
    except Exception:
        return "[UNPARSEABLE_URL]"


def validate_test_db_url(url: Optional[str]) -> str:
    """
    Structurally validates that the database URL points strictly to the approved
    disposable local test database: localhost:5432/sebi_rag_test or 127.0.0.1:5432/sebi_rag_test.

    Raises SafeTestDatabaseError with redacted URL details on any mismatch.
    Returns the cleaned URL string if approved.
    """
    if not url or not url.strip():
        raise SafeTestDatabaseError(
            "Test database URL is missing or empty. Destructive tests require an explicitly configured URL."
        )

    raw_url = url.strip()
    redacted = redact_db_url(raw_url)

    try:
        parsed = urlparse(raw_url)
    except Exception as e:
        raise SafeTestDatabaseError(f"Failed to parse test database URL: {e}") from e

    # 1. Scheme check
    if not parsed.scheme or parsed.scheme.lower() not in APPROVED_SCHEMES:
        raise SafeTestDatabaseError(
            f"Unsupported scheme '{parsed.scheme}' in test database URL '{redacted}'. "
            f"Must be one of: {sorted(APPROVED_SCHEMES)}."
        )

    # 2. Hostname check
    hostname = parsed.hostname.lower() if parsed.hostname else ""
    if not hostname or hostname not in APPROVED_HOSTS:
        raise SafeTestDatabaseError(
            f"Prohibited database host '{hostname}' in '{redacted}'. "
            f"Destructive integration tests permit ONLY local disposable targets: {sorted(APPROVED_HOSTS)}."
        )

    # 3. Port check
    port = parsed.port if parsed.port is not None else APPROVED_PORT
    if port != APPROVED_PORT:
        raise SafeTestDatabaseError(
            f"Prohibited database port '{parsed.port}' in '{redacted}'. "
            f"Destructive integration tests permit ONLY port {APPROVED_PORT}."
        )

    # 4. Database name check
    db_name = parsed.path.lstrip("/").strip()
    if db_name != APPROVED_DATABASE:
        raise SafeTestDatabaseError(
            f"Prohibited database name '{db_name}' in '{redacted}'. "
            f"Destructive integration tests permit ONLY database '{APPROVED_DATABASE}'."
        )

    return raw_url


def get_approved_test_db_url(
    env_var_precedence: tuple[str, ...] = ("TEST_DATABASE_URL", "WORKER_DATABASE_URL", "DATABASE_URL")
) -> Optional[str]:
    """
    Resolves the test database URL from environment variables according to precedence.
    If none of the environment variables are set, returns None (allowing tests to safely skip).
    If an environment variable IS set, validates it strictly; if invalid, raises SafeTestDatabaseError.
    """
    for var in env_var_precedence:
        val = os.getenv(var)
        if val and val.strip():
            return validate_test_db_url(val)
    return None

