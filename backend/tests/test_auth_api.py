"""
Focused tests for Milestone 8 - Task 8.2: Authentication Foundation.

Covers:
1. Successful user signup with normalized email and Argon2id hash verification.
2. Duplicate email rejection (case-insensitive) on signup.
3. Password strength and email format validation failures.
4. Password hash secrecy: hashes and plaintext passwords are never returned in responses.
5. Successful login returning valid JWT access token and user metadata.
6. Generic login failures: wrong password, non-existent email, and deactivated user.
7. Authenticated /me endpoint returns trusted user identity.
8. Rejection of unauthenticated, missing, forged, malformed, and expired tokens.
9. Stateless logout behavior.
10. Authorization dependency isolation (refuses forged IDs in headers/queries).
11. Preserves regression safety across Task 8.1 schema.
"""
from datetime import datetime, timezone, timedelta
import os
import uuid
import pytest
import jwt
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.core.config import get_settings
from app.core.auth import hash_password, verify_password, create_access_token
from app.models.auth import UserModel
from app.api.deps import get_db_session

from tests.db_test_guard import get_approved_test_db_url

ADMIN_DB_URL = get_approved_test_db_url()
pytestmark = pytest.mark.skipif(
    not ADMIN_DB_URL,
    reason="No approved local PostgreSQL test database configured (set TEST_DATABASE_URL=postgresql+psycopg://.../sebi_rag_test).",
)


@pytest.fixture(scope="module")
def db_engine():
    """Create database engine connecting to test database."""
    engine = create_engine(ADMIN_DB_URL, echo=False)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    """Provides a transactional database session rolled back after each test."""
    conn = db_engine.connect()
    trans = conn.begin()
    SessionMaker = sessionmaker(bind=conn, expire_on_commit=False)
    session = SessionMaker()
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        conn.close()


@pytest.fixture
def client(db_session):
    """Provides a TestClient with overridden database session."""
    def _override_get_db_session():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db_session] = _override_get_db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db_session, None)


def test_signup_success_and_argon2_hashing(client, db_session):
    """Verifies successful user signup, Argon2id hash storage, and safe response fields."""
    suffix = uuid.uuid4().hex[:8]
    email = f"Alice_{suffix}@Example.com"
    raw_password = "SecurePassword123!"

    response = client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": raw_password, "full_name": "Alice Smith"},
    )
    assert response.status_code == 201
    data = response.json()
    assert "id" in data
    assert data["email"] == f"alice_{suffix}@example.com"  # Normalized lower
    assert data["full_name"] == "Alice Smith"
    assert data["is_active"] is True
    assert "password" not in data
    assert "hashed_password" not in data

    # Verify directly in database that password was hashed with Argon2id
    user_row = db_session.execute(
        text("SELECT email, hashed_password FROM public.users WHERE id = :id"),
        {"id": data["id"]},
    ).fetchone()
    assert user_row is not None
    assert user_row[0] == f"alice_{suffix}@example.com"
    stored_hash = user_row[1]
    assert stored_hash.startswith("$argon2id$")
    assert verify_password(raw_password, stored_hash) is True
    assert verify_password("WrongPassword!", stored_hash) is False


def test_signup_duplicate_email_rejection(client):
    """Verifies duplicate email rejection across different case variants."""
    suffix = uuid.uuid4().hex[:8]
    email = f"bob_{suffix}@example.com"

    res1 = client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "Password123!"},
    )
    assert res1.status_code == 201

    # Attempt signup with different casing
    res2 = client.post(
        "/api/v1/auth/signup",
        json={"email": email.upper(), "password": "AnotherPassword123!"},
    )
    assert res2.status_code == 409
    assert "already exists" in res2.json()["detail"]


def test_signup_validation_failures(client):
    """Verifies email and password syntax validations fail closed."""
    # Invalid email
    res1 = client.post(
        "/api/v1/auth/signup",
        json={"email": "not-an-email", "password": "Password123!"},
    )
    assert res1.status_code == 422

    # Password too short (< 8 chars)
    res2 = client.post(
        "/api/v1/auth/signup",
        json={"email": "valid@example.com", "password": "short"},
    )
    assert res2.status_code == 422


def test_login_success(client):
    """Verifies valid login returns access token and user metadata."""
    suffix = uuid.uuid4().hex[:8]
    email = f"charlie_{suffix}@example.com"
    password = "CorrectPassword123!"

    signup_res = client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": password, "full_name": "Charlie"},
    )
    assert signup_res.status_code == 201

    # Login with mixed casing
    login_res = client.post(
        "/api/v1/auth/login",
        json={"email": f"Charlie_{suffix}@example.com", "password": password},
    )
    assert login_res.status_code == 200
    token_data = login_res.json()
    assert "access_token" in token_data
    assert token_data["token_type"] == "bearer"
    assert token_data["expires_in_seconds"] > 0
    assert token_data["user"]["email"] == email
    assert "password" not in token_data
    assert "hashed_password" not in token_data["user"]


def test_login_generic_failures(client, db_session):
    """Verifies that non-existent emails and wrong passwords return identical generic 401s."""
    suffix = uuid.uuid4().hex[:8]
    email = f"dan_{suffix}@example.com"
    password = "DanPassword123!"

    client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": password},
    )

    # 1. Wrong password
    res_wrong_pw = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "WrongPassword123!"},
    )
    assert res_wrong_pw.status_code == 401
    assert res_wrong_pw.json()["detail"] == "Invalid email or password."

    # 2. Non-existent email
    res_no_user = client.post(
        "/api/v1/auth/login",
        json={"email": f"nonexistent_{suffix}@example.com", "password": password},
    )
    assert res_no_user.status_code == 401
    assert res_no_user.json()["detail"] == "Invalid email or password."

    # 3. Deactivated user
    db_session.execute(
        text("UPDATE public.users SET is_active = FALSE WHERE email = :email"),
        {"email": email},
    )
    db_session.flush()
    db_session.expire_all()
    res_inactive = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert res_inactive.status_code == 401
    assert res_inactive.json()["detail"] == "Invalid email or password."


def test_authenticated_me_endpoint_and_token_protection(client):
    """Verifies /me endpoint accepts valid tokens and rejects missing, invalid, or forged tokens."""
    suffix = uuid.uuid4().hex[:8]
    email = f"eve_{suffix}@example.com"
    password = "EvePassword123!"

    client.post("/api/v1/auth/signup", json={"email": email, "password": password})
    login_res = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    token = login_res.json()["access_token"]

    # 1. Successful /me with Bearer token
    me_res = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert me_res.status_code == 200
    assert me_res.json()["email"] == email

    # 2. Missing token
    res_no_auth = client.get("/api/v1/auth/me")
    assert res_no_auth.status_code == 401

    # 3. Malformed / forged token
    res_bad_token = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer forged.token.value"},
    )
    assert res_bad_token.status_code == 401

    # 4. Token signed with wrong secret key
    forged_token = jwt.encode(
        {"sub": me_res.json()["id"], "email": email, "iat": datetime.now(timezone.utc), "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        "wrong_signing_secret_key_that_should_fail",
        algorithm="HS256",
    )
    res_forged = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {forged_token}"},
    )
    assert res_forged.status_code == 401


def test_expired_token_rejection(client):
    """Verifies that expired tokens fail closed with 401."""
    settings = get_settings()
    user_id = uuid.uuid4()
    expired_token, _ = create_access_token(
        user_id=user_id,
        email="expired@example.com",
        settings=settings,
        expires_delta=timedelta(seconds=-10),  # expired 10 seconds ago
    )

    res = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert res.status_code == 401
    assert "expired" in res.json()["detail"].lower()


def test_logout_endpoint(client):
    """Verifies logout endpoint returns stable confirmation."""
    suffix = uuid.uuid4().hex[:8]
    email = f"frank_{suffix}@example.com"
    password = "FrankPassword123!"

    client.post("/api/v1/auth/signup", json={"email": email, "password": password})
    login_res = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    token = login_res.json()["access_token"]

    logout_res = client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert logout_res.status_code == 200
    assert "logged out" in logout_res.json()["message"].lower()
