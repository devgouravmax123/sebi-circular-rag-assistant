"""
Unit tests for the shared test database target guard (Phase 8B.7 Stage C.1).

These tests run entirely in memory and do not attempt to connect to any database.
"""
import pytest
from tests.db_test_guard import (
    validate_test_db_url,
    get_approved_test_db_url,
    redact_db_url,
    SafeTestDatabaseError,
    APPROVED_DATABASE,
)


def test_approved_localhost_target():
    url = "postgresql+psycopg://postgres:secret_pass123@localhost:5432/sebi_rag_test"
    validated = validate_test_db_url(url)
    assert validated == url


def test_approved_127_0_0_1_target():
    url = "postgresql+psycopg://test_user:secure_pwd@127.0.0.1:5432/sebi_rag_test"
    validated = validate_test_db_url(url)
    assert validated == url


def test_approved_default_port_implicit():
    url = "postgresql://postgres:pass@localhost/sebi_rag_test"
    validated = validate_test_db_url(url)
    assert validated == url


def test_missing_or_empty_url_raises():
    with pytest.raises(SafeTestDatabaseError, match="missing or empty"):
        validate_test_db_url("")

    with pytest.raises(SafeTestDatabaseError, match="missing or empty"):
        validate_test_db_url("   ")

    with pytest.raises(SafeTestDatabaseError, match="missing or empty"):
        validate_test_db_url(None)


def test_unexpected_database_name_rejected():
    # Production DB
    with pytest.raises(SafeTestDatabaseError, match=f"Prohibited database name 'sebi_rag_prod'") as exc:
        validate_test_db_url("postgresql+psycopg://postgres:secret@localhost:5432/sebi_rag_prod")
    assert "secret" not in str(exc.value)

    # Generic postgres DB
    with pytest.raises(SafeTestDatabaseError, match=f"Prohibited database name 'postgres'"):
        validate_test_db_url("postgresql+psycopg://postgres:secret@localhost:5432/postgres")

    # Name containing 'test' but not matching approved database
    with pytest.raises(SafeTestDatabaseError, match=f"Prohibited database name 'test_other_db'"):
        validate_test_db_url("postgresql+psycopg://postgres:secret@localhost:5432/test_other_db")


def test_unexpected_port_rejected():
    with pytest.raises(SafeTestDatabaseError, match="Prohibited database port '5433'") as exc:
        validate_test_db_url("postgresql+psycopg://postgres:secret@localhost:5433/sebi_rag_test")
    assert "secret" not in str(exc.value)

    with pytest.raises(SafeTestDatabaseError, match="Prohibited database port '6543'"):
        validate_test_db_url("postgresql+psycopg://postgres:secret@localhost:6543/sebi_rag_test")


def test_remote_host_rejected():
    with pytest.raises(SafeTestDatabaseError, match="Prohibited database host 'db.render.com'") as exc:
        validate_test_db_url("postgresql+psycopg://postgres:secret@db.render.com:5432/sebi_rag_test")
    assert "secret" not in str(exc.value)

    with pytest.raises(SafeTestDatabaseError, match="Prohibited database host '192.168.1.100'"):
        validate_test_db_url("postgresql+psycopg://postgres:secret@192.168.1.100:5432/sebi_rag_test")


def test_hosted_cloud_hostname_rejected():
    # Neon
    with pytest.raises(SafeTestDatabaseError, match="Prohibited database host 'ep-cool-flower-12345.us-east-2.aws.neon.tech'") as exc:
        validate_test_db_url("postgresql://user:top_secret_token@ep-cool-flower-12345.us-east-2.aws.neon.tech:5432/sebi_rag_test")
    assert "top_secret_token" not in str(exc.value)

    # Supabase
    with pytest.raises(SafeTestDatabaseError, match="Prohibited database host 'db.supabase.co'") as exc:
        validate_test_db_url("postgresql://postgres:supabase_secret@db.supabase.co:5432/sebi_rag_test")
    assert "supabase_secret" not in str(exc.value)


def test_malformed_url_rejected():
    with pytest.raises(SafeTestDatabaseError, match="Unsupported scheme"):
        validate_test_db_url("http://localhost:5432/sebi_rag_test")

    with pytest.raises(SafeTestDatabaseError, match="Unsupported scheme"):
        validate_test_db_url("not-a-valid-url")


def test_credential_redaction():
    secret_pass = "P@$$w0rd_With_Spec!al_Ch@rs"
    url = f"postgresql+psycopg://admin_user:{secret_pass}@remote-host.example.com:5432/sebi_rag_test"
    redacted = redact_db_url(url)
    assert secret_pass not in redacted
    assert "admin_user" not in redacted
    assert "***:***" in redacted
    assert "remote-host.example.com" in redacted

    with pytest.raises(SafeTestDatabaseError) as exc_info:
        validate_test_db_url(url)
    err_msg = str(exc_info.value)
    assert secret_pass not in err_msg
    assert "admin_user" not in err_msg


def test_get_approved_test_db_url_precedence_and_skipping(monkeypatch):
    # When all env vars are unset -> returns None safely
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("WORKER_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert get_approved_test_db_url() is None

    # When valid TEST_DATABASE_URL is set -> returns URL
    valid_url = "postgresql+psycopg://postgres:pass@localhost:5432/sebi_rag_test"
    monkeypatch.setenv("TEST_DATABASE_URL", valid_url)
    assert get_approved_test_db_url() == valid_url

    # When unsafe remote URL is set -> raises SafeTestDatabaseError (fails closed)
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://user:pass@remote-host:5432/sebi_rag_test")
    with pytest.raises(SafeTestDatabaseError, match="Prohibited database host"):
        get_approved_test_db_url()

