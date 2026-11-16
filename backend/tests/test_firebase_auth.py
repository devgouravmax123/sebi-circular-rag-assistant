"""
Comprehensive automated tests for Firebase Authentication integration (Milestone 18).

Covers:
1. Auto-provisioning of new verified Firebase users and their personal workspace on first request.
2. Idempotency of verified Firebase user resolution on subsequent requests.
3. Blocking of unverified emails with HTTP 403 Forbidden.
4. Rejection of invalid / forged / malformed Firebase tokens with HTTP 401 Unauthorized.
5. Legacy account conflict protection: unlinked accounts return HTTP 409 Conflict to prevent auto-merge hijacking.
6. Safe explicit linking via POST /api/v1/auth/link-legacy with correct password.
7. Rejection of incorrect legacy passwords with HTTP 401 Unauthorized.
8. Rejection of link-legacy attempts with unverified Firebase emails (HTTP 403 Forbidden).
9. Backward compatibility: existing signed internal JWT access tokens continue to function.
10. Tenant and guest boundary protection: guest session tokens are rejected on registered endpoints.
"""
from datetime import datetime, timezone, timedelta
import uuid
import jwt
import pytest
from starlette.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID

# SQLite dialect mappings
@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "TEXT"

@compiles(PG_UUID, "sqlite")
def compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"

from app.main import app
from app.core.config import get_settings
from app.core.auth import hash_password, create_access_token
from app.core.firebase import (
    set_firebase_verifier_override,
    reset_firebase_verifier_override,
)
from app.persistence.models import Base
from app.models.auth import UserModel, WorkspaceModel, WorkspaceMembershipModel
from app.api.deps import get_db_session, get_db_session_optional


@pytest.fixture
def sqlite_db():
    """Provides an isolated in-memory SQLite database session with Base tables."""
    engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionMaker = sessionmaker(bind=engine, expire_on_commit=False)
    session = SessionMaker()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def test_client(sqlite_db):
    """Provides a TestClient with overridden database session."""
    def _override_get_db_session():
        yield sqlite_db

    app.dependency_overrides[get_db_session] = _override_get_db_session
    app.dependency_overrides[get_db_session_optional] = _override_get_db_session

    with TestClient(app) as client:
        yield client

    app.dependency_overrides.pop(get_db_session, None)
    app.dependency_overrides.pop(get_db_session_optional, None)
    reset_firebase_verifier_override()


def test_firebase_token_auto_provisions_verified_user_and_workspace(test_client, sqlite_db):
    """
    1. A valid Firebase ID token with email_verified=True automatically provisions
    a new user and personal workspace upon calling /api/v1/auth/me.
    2. Subsequent calls are idempotent.
    """
    fb_uid = "fb_uid_alice_123"
    fb_email = "alice.researcher@example.com"
    token = "mock-verified-token-alice"

    def mock_verifier(raw_token):
        if raw_token == token:
            return {
                "uid": fb_uid,
                "email": fb_email,
                "email_verified": True,
                "name": "Alice Researcher",
            }
        raise ValueError("Invalid token")

    set_firebase_verifier_override(mock_verifier)

    # First request: auto-provisions user + workspace
    resp1 = test_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert data1["email"] == fb_email
    assert data1["full_name"] == "Alice Researcher"
    assert data1["firebase_uid"] == fb_uid
    assert data1["email_verified"] is True
    user_id = data1["id"]

    # Verify workspace was created in DB
    ws_membership = (
        sqlite_db.query(WorkspaceMembershipModel)
        .filter(WorkspaceMembershipModel.user_id == uuid.UUID(user_id))
        .first()
    )
    assert ws_membership is not None
    assert ws_membership.role == "owner"

    ws = sqlite_db.query(WorkspaceModel).filter(WorkspaceModel.id == ws_membership.workspace_id).first()
    assert ws is not None
    assert "Alice" in ws.name

    # Second request: idempotent resolution of the same user
    resp2 = test_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["id"] == user_id
    assert data2["firebase_uid"] == fb_uid


def test_firebase_unverified_email_blocked_with_403(test_client, sqlite_db):
    """An unverified email address (email_verified=False) must be blocked with HTTP 403 Forbidden."""
    unverified_token = "mock-unverified-token-bob"

    def mock_verifier(raw_token):
        if raw_token == unverified_token:
            return {
                "uid": "fb_bob_unverified",
                "email": "bob.unverified@example.com",
                "email_verified": False,
                "name": "Bob Unverified",
            }
        raise ValueError("Invalid token")

    set_firebase_verifier_override(mock_verifier)

    resp = test_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {unverified_token}"})
    assert resp.status_code == 403
    assert "Email verification required" in resp.json()["detail"]

    # Ensure no user was created in the database
    users_count = sqlite_db.query(UserModel).filter(UserModel.email == "bob.unverified@example.com").count()
    assert users_count == 0


def test_firebase_invalid_or_forged_token_rejected_with_401(test_client):
    """An invalid, forged, or expired Firebase ID token must be rejected with HTTP 401."""
    def mock_verifier(raw_token):
        raise ValueError("Firebase ID token verification failed: signature invalid.")

    set_firebase_verifier_override(mock_verifier)

    resp = test_client.get("/api/v1/auth/me", headers={"Authorization": "Bearer forged-token-xyz"})
    assert resp.status_code == 401


def test_firebase_collision_with_unlinked_legacy_account_returns_409(test_client, sqlite_db):
    """
    CRITICAL SECURITY CHECK:
    If a legacy account exists with matching email but no firebase_uid, the server must NEVER
    automatically merge it. It must return HTTP 409 Conflict.
    """
    legacy_email = "carol.legacy@example.com"
    legacy_user = UserModel(
        id=uuid.uuid4(),
        email=legacy_email,
        hashed_password=hash_password("OldPassword123!"),
        full_name="Carol Legacy",
        firebase_uid=None,
        email_verified=False,
        is_active=True,
    )
    sqlite_db.add(legacy_user)
    sqlite_db.commit()

    token = "mock-verified-token-carol"
    def mock_verifier(raw_token):
        if raw_token == token:
            return {
                "uid": "fb_carol_new_uid",
                "email": legacy_email,
                "email_verified": True,
                "name": "Carol",
            }
        raise ValueError("Invalid")

    set_firebase_verifier_override(mock_verifier)

    # Calling /me must return HTTP 409 Conflict instructing to link the account
    resp = test_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 409
    assert "link your existing account" in resp.json()["detail"].lower()

    # Verify legacy user was NOT modified
    sqlite_db.refresh(legacy_user)
    assert legacy_user.firebase_uid is None


def test_firebase_link_legacy_account_success(test_client, sqlite_db):
    """
    Explicit account linking via POST /api/v1/auth/link-legacy:
    1. User presents verified Firebase token + correct legacy password.
    2. Legacy account is safely linked (firebase_uid and email_verified updated).
    3. Subsequent calls to /me resolve to the legacy user.
    """
    legacy_email = "david.link@example.com"
    legacy_user = UserModel(
        id=uuid.uuid4(),
        email=legacy_email,
        hashed_password=hash_password("SecretLegacyPass99!"),
        full_name="David Link",
        firebase_uid=None,
        email_verified=False,
        is_active=True,
    )
    sqlite_db.add(legacy_user)
    sqlite_db.commit()

    fb_uid = "fb_david_uid_456"
    token = "mock-token-david"

    def mock_verifier(raw_token):
        if raw_token == token:
            return {
                "uid": fb_uid,
                "email": legacy_email,
                "email_verified": True,
                "name": "David",
            }
        raise ValueError("Invalid")

    set_firebase_verifier_override(mock_verifier)

    # Link with correct password
    link_resp = test_client.post(
        "/api/v1/auth/link-legacy",
        headers={"Authorization": f"Bearer {token}"},
        json={"password": "SecretLegacyPass99!"},
    )
    assert link_resp.status_code == 200
    link_data = link_resp.json()
    assert link_data["id"] == str(legacy_user.id)
    assert link_data["firebase_uid"] == fb_uid
    assert link_data["email_verified"] is True

    # Database state updated
    sqlite_db.refresh(legacy_user)
    assert legacy_user.firebase_uid == fb_uid
    assert legacy_user.email_verified is True

    # Subsequent /me call succeeds directly without 409
    me_resp = test_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_resp.status_code == 200
    assert me_resp.json()["id"] == str(legacy_user.id)


def test_firebase_link_legacy_account_wrong_password_fails(test_client, sqlite_db):
    """Attempting to link with an incorrect password returns HTTP 401 and does not link."""
    legacy_email = "eve.fail@example.com"
    legacy_user = UserModel(
        id=uuid.uuid4(),
        email=legacy_email,
        hashed_password=hash_password("CorrectPassword123!"),
        full_name="Eve Fail",
        firebase_uid=None,
        email_verified=False,
        is_active=True,
    )
    sqlite_db.add(legacy_user)
    sqlite_db.commit()

    token = "mock-token-eve"
    def mock_verifier(raw_token):
        if raw_token == token:
            return {
                "uid": "fb_eve_uid",
                "email": legacy_email,
                "email_verified": True,
            }
        raise ValueError("Invalid")

    set_firebase_verifier_override(mock_verifier)

    # Submit wrong password
    link_resp = test_client.post(
        "/api/v1/auth/link-legacy",
        headers={"Authorization": f"Bearer {token}"},
        json={"password": "TotallyWrongPassword!"},
    )
    assert link_resp.status_code == 401
    assert "Incorrect password" in link_resp.json()["detail"]

    # Verify account remained unlinked
    sqlite_db.refresh(legacy_user)
    assert legacy_user.firebase_uid is None


def test_firebase_link_legacy_unverified_email_fails_403(test_client, sqlite_db):
    """Attempting to link with an unverified Firebase email is rejected with HTTP 403."""
    legacy_email = "frank.unverified@example.com"
    legacy_user = UserModel(
        id=uuid.uuid4(),
        email=legacy_email,
        hashed_password=hash_password("CorrectPass!"),
        firebase_uid=None,
        email_verified=False,
    )
    sqlite_db.add(legacy_user)
    sqlite_db.commit()

    token = "mock-token-frank-unverified"
    def mock_verifier(raw_token):
        if raw_token == token:
            return {
                "uid": "fb_frank_uid",
                "email": legacy_email,
                "email_verified": False,
            }
        raise ValueError("Invalid")

    set_firebase_verifier_override(mock_verifier)

    link_resp = test_client.post(
        "/api/v1/auth/link-legacy",
        headers={"Authorization": f"Bearer {token}"},
        json={"password": "CorrectPass!"},
    )
    assert link_resp.status_code == 403
    assert "Email verification required" in link_resp.json()["detail"]


def test_legacy_internal_jwt_and_guest_isolation(test_client, sqlite_db):
    """
    1. Signed internal JWT tokens continue to work on /api/v1/auth/me (backward compatibility).
    2. Guest tokens are rejected on /api/v1/auth/me with HTTP 401.
    """
    # 1. Internal registered user JWT
    user = UserModel(
        id=uuid.uuid4(),
        email="grace.jwt@example.com",
        hashed_password=hash_password("Pass123!"),
        full_name="Grace JWT",
        is_active=True,
    )
    sqlite_db.add(user)
    sqlite_db.commit()

    settings = get_settings()
    registered_jwt, _ = create_access_token(
        user_id=user.id,
        email=user.email,
        settings=settings,
        expires_delta=timedelta(hours=1),
    )

    resp_registered = test_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {registered_jwt}"})
    assert resp_registered.status_code == 200
    assert resp_registered.json()["id"] == str(user.id)

    # 2. Guest token cannot access /auth/me
    secret_key = settings.get_auth_secret_key
    now = datetime.now(timezone.utc)
    guest_payload = {
        "sub": "guest-session-12345",
        "role": "guest",
        "is_guest": True,
        "exp": int((now + timedelta(hours=1)).timestamp()),
        "iat": int(now.timestamp()),
    }
    guest_jwt = jwt.encode(guest_payload, secret_key, algorithm=settings.auth_algorithm)

    resp_guest = test_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {guest_jwt}"})
    assert resp_guest.status_code == 401
    assert "Guest sessions cannot access registered user resources" in resp_guest.json()["detail"]
