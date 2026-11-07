"""
test_api_url_segregation.py
===========================
Unit tests for Task 8.1 API Runtime Role and Database Connection URL Segregation Triad.

Verifies:
1. Strict connection URL segregation:
   - DIRECT_DATABASE_URL: direct port 5432, neondb_owner only.
   - DATABASE_URL: pooled port 6543 / -pooler, sebi_api (rejects neondb_owner in production).
   - WORKER_DATABASE_URL: direct port 5432, sebi_worker (rejects neondb_owner and sebi_api).
2. Zero silent fallbacks:
   - DATABASE_URL never falls back to DIRECT_DATABASE_URL.
   - WORKER_DATABASE_URL never falls back to DATABASE_URL or DIRECT_DATABASE_URL.
3. Development mode flexibility:
   - Permits local SQLite or PostgreSQL in development / test environments.
4. Credential sanitization:
   - Password masking across all three URL endpoints.
"""
import pytest
from app.core.config import Settings


VALID_ADMIN_KEY = "a-very-secure-production-admin-key-64chars-long"


def test_production_rejects_owner_on_api_database_url(monkeypatch):
    """Production DATABASE_URL must not connect as migration owner 'neondb_owner'."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ADMIN_API_KEY", VALID_ADMIN_KEY)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://neondb_owner:secret_pw@ep-bitter-mountain-a1bfl65t-pooler.neon.tech:6543/neondb",
    )
    monkeypatch.delenv("WORKER_DATABASE_URL", raising=False)
    monkeypatch.delenv("DIRECT_DATABASE_URL", raising=False)

    with pytest.raises(ValueError) as exc_info:
        Settings()
    assert "DATABASE_URL must not connect as migration owner 'neondb_owner'" in str(exc_info.value)


def test_production_rejects_unpooled_neon_on_api_database_url(monkeypatch):
    """Production Neon DATABASE_URL must connect through a connection pooler (port 6543 or -pooler)."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ADMIN_API_KEY", VALID_ADMIN_KEY)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://sebi_api:secret_pw@ep-bitter-mountain-a1bfl65t.neon.tech:5432/neondb",
    )
    monkeypatch.delenv("WORKER_DATABASE_URL", raising=False)
    monkeypatch.delenv("DIRECT_DATABASE_URL", raising=False)

    with pytest.raises(ValueError) as exc_info:
        Settings()
    assert "Production Neon DATABASE_URL must connect via connection pooler" in str(exc_info.value)


def test_production_accepts_sebi_api_on_pooled_neon_database_url(monkeypatch):
    """Production accepts dedicated sebi_api login on pooled Neon connection."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ADMIN_API_KEY", VALID_ADMIN_KEY)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://sebi_api:secret_pw@ep-bitter-mountain-a1bfl65t-pooler.neon.tech:6543/neondb",
    )
    monkeypatch.delenv("WORKER_DATABASE_URL", raising=False)
    monkeypatch.delenv("DIRECT_DATABASE_URL", raising=False)

    settings = Settings()
    assert settings.database_url is not None
    assert "sebi_api" in settings.sanitized_database_url
    assert "secret_pw" not in settings.sanitized_database_url


def test_production_rejects_owner_or_api_on_worker_database_url(monkeypatch):
    """Production WORKER_DATABASE_URL must reject both neondb_owner and sebi_api."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ADMIN_API_KEY", VALID_ADMIN_KEY)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DIRECT_DATABASE_URL", raising=False)

    # 1. Reject neondb_owner
    monkeypatch.setenv(
        "WORKER_DATABASE_URL",
        "postgresql://neondb_owner:secret_pw@ep-bitter-mountain-a1bfl65t.neon.tech:5432/neondb",
    )
    with pytest.raises(ValueError) as exc_info:
        Settings()
    assert "WORKER_DATABASE_URL cannot connect as 'neondb_owner'" in str(exc_info.value)

    # 2. Reject sebi_api
    monkeypatch.setenv(
        "WORKER_DATABASE_URL",
        "postgresql://sebi_api:secret_pw@ep-bitter-mountain-a1bfl65t.neon.tech:5432/neondb",
    )
    with pytest.raises(ValueError) as exc_info:
        Settings()
    assert "WORKER_DATABASE_URL cannot connect as 'sebi_api'" in str(exc_info.value)


def test_production_accepts_sebi_worker_on_direct_worker_database_url(monkeypatch):
    """Production accepts dedicated sebi_worker on direct Neon connection."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ADMIN_API_KEY", VALID_ADMIN_KEY)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DIRECT_DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "WORKER_DATABASE_URL",
        "postgresql://sebi_worker:secret_pw@ep-bitter-mountain-a1bfl65t.neon.tech:5432/neondb",
    )

    settings = Settings()
    assert settings.worker_database_url is not None
    assert "sebi_worker" in settings.sanitized_worker_database_url
    assert "secret_pw" not in settings.sanitized_worker_database_url


def test_production_rejects_pooled_neon_on_direct_database_url(monkeypatch):
    """Production DIRECT_DATABASE_URL must be direct unpooled connection (port 5432)."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ADMIN_API_KEY", VALID_ADMIN_KEY)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("WORKER_DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "DIRECT_DATABASE_URL",
        "postgresql://neondb_owner:secret_pw@ep-bitter-mountain-a1bfl65t-pooler.neon.tech:6543/neondb",
    )

    with pytest.raises(ValueError) as exc_info:
        Settings()
    assert "DIRECT_DATABASE_URL must be direct unpooled connection" in str(exc_info.value)


def test_production_rejects_non_owner_on_direct_database_url(monkeypatch):
    """Production DIRECT_DATABASE_URL on Neon must connect as 'neondb_owner'."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ADMIN_API_KEY", VALID_ADMIN_KEY)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("WORKER_DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "DIRECT_DATABASE_URL",
        "postgresql://sebi_api:secret_pw@ep-bitter-mountain-a1bfl65t.neon.tech:5432/neondb",
    )

    with pytest.raises(ValueError) as exc_info:
        Settings()
    assert "DIRECT_DATABASE_URL on Neon must connect as 'neondb_owner'" in str(exc_info.value)


def test_no_silent_fallback_between_urls(monkeypatch):
    """Confirms no cross-variable fallback between DATABASE_URL, WORKER_DATABASE_URL, DIRECT_DATABASE_URL."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv(
        "DIRECT_DATABASE_URL",
        "postgresql://neondb_owner:secret_pw@ep-bitter-mountain-a1bfl65t.neon.tech:5432/neondb",
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("WORKER_DATABASE_URL", raising=False)

    settings = Settings()
    # DIRECT_DATABASE_URL is set, but DATABASE_URL must remain None
    assert settings.direct_database_url is not None
    assert settings.database_url is None
    assert settings.is_database_enabled is False

    # WORKER_DATABASE_URL must also remain None
    assert settings.worker_database_url is None
    assert settings.is_worker_database_enabled is False


def test_development_permits_local_urls(monkeypatch):
    """Development mode permits local SQLite and PostgreSQL without production restrictions."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./test.db")
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)

    settings = Settings()
    assert settings.is_database_enabled is True
    assert settings.database_url == "sqlite:///./test.db"

