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


def test_firebase_with_simulated_postgres_trigger(test_client, sqlite_db):
    """
    Simulates PostgreSQL trigger trg_maintain_workspace_owner_membership on public.workspaces.
    When a workspace is created, the trigger immediately inserts (ws.id, ws.owner_id, 'owner')
    into workspace_memberships.
    Verifies that deps.ensure_user_personal_workspace does NOT attempt a redundant insert
    and does NOT crash with IntegrityError.
    """
    from sqlalchemy import event

    # Attach event listener to simulate the PostgreSQL trigger on WorkspaceModel
    def trigger_listener(mapper, connection, target):
        if not target.is_system and target.owner_id is not None:
            connection.execute(
                WorkspaceMembershipModel.__table__.insert().values(
                    id=uuid.uuid4(),
                    workspace_id=target.id,
                    user_id=target.owner_id,
                    role="owner",
                    created_at=datetime.now(timezone.utc),
                )
            )

    event.listen(WorkspaceModel, "after_insert", trigger_listener)

    try:
        fb_uid = "fb_uid_postgres_sim_user"
        fb_email = "simulated.postgres@example.com"
        token = "mock-verified-token-sim"

        def mock_verifier(raw_token):
            if raw_token == token:
                return {
                    "uid": fb_uid,
                    "email": fb_email,
                    "email_verified": True,
                    "name": "Simulated PG User",
                }
            raise ValueError("Invalid")

        set_firebase_verifier_override(mock_verifier)

        # GET /api/v1/auth/me should succeed with 200 without throwing UniqueViolation
        resp = test_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == fb_email
        user_id = uuid.UUID(data["id"])

        # Exactly 1 membership should exist
        memberships = (
            sqlite_db.query(WorkspaceMembershipModel)
            .filter(WorkspaceMembershipModel.user_id == user_id)
            .all()
        )
        assert len(memberships) == 1
        assert memberships[0].role == "owner"

        # Subsequent call should also succeed idempotently
        resp2 = test_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp2.status_code == 200
        assert len(sqlite_db.query(WorkspaceMembershipModel).filter(WorkspaceMembershipModel.user_id == user_id).all()) == 1

    finally:
        event.remove(WorkspaceModel, "after_insert", trigger_listener)


def test_repeated_auth_me_produces_no_duplicate_memberships(test_client, sqlite_db):
    """
    Repeated requests to GET /api/v1/auth/me for the same Firebase user must be
    strictly idempotent and must never create duplicate workspace memberships.
    """
    fb_uid = "fb_uid_repeated_tester"
    fb_email = "repeated.tester@example.com"
    token = "mock-verified-token-repeated"

    def mock_verifier(raw_token):
        if raw_token == token:
            return {
                "uid": fb_uid,
                "email": fb_email,
                "email_verified": True,
                "name": "Repeated Tester",
            }
        raise ValueError("Invalid")

    set_firebase_verifier_override(mock_verifier)

    # Call 5 times consecutively
    for _ in range(5):
        resp = test_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200

    user = sqlite_db.query(UserModel).filter(UserModel.firebase_uid == fb_uid).first()
    assert user is not None
    memberships = sqlite_db.query(WorkspaceMembershipModel).filter(WorkspaceMembershipModel.user_id == user.id).all()
    assert len(memberships) == 1


def test_existing_membership_role_preservation(test_client, sqlite_db):
    """
    If a user already belongs to a workspace with role 'viewer' (e.g. invited member),
    logging in and calling /api/v1/auth/me MUST preserve their existing role
    and NOT overwrite it with 'owner' or create duplicate memberships.
    """
    # Create an owner user and a workspace
    owner = UserModel(
        id=uuid.uuid4(),
        email="corp.owner@example.com",
        full_name="Corp Owner",
        is_active=True,
    )
    shared_ws = WorkspaceModel(
        id=uuid.uuid4(),
        name="Team Workspace",
        slug="team-workspace-custom",
        is_system=False,
        owner_id=owner.id,
    )
    sqlite_db.add_all([owner, shared_ws])
    sqlite_db.commit()

    # Pre-register viewer user
    viewer_email = "viewer.user@example.com"
    viewer_fb_uid = "fb_viewer_uid_999"
    viewer = UserModel(
        id=uuid.uuid4(),
        email=viewer_email,
        firebase_uid=viewer_fb_uid,
        full_name="Viewer User",
        email_verified=True,
        is_active=True,
    )
    membership = WorkspaceMembershipModel(
        id=uuid.uuid4(),
        workspace_id=shared_ws.id,
        user_id=viewer.id,
        role="viewer",  # Explicitly non-owner
    )
    sqlite_db.add_all([viewer, membership])
    sqlite_db.commit()

    token = "mock-token-viewer"
    def mock_verifier(raw_token):
        if raw_token == token:
            return {
                "uid": viewer_fb_uid,
                "email": viewer_email,
                "email_verified": True,
                "name": "Viewer User",
            }
        raise ValueError("Invalid")

    set_firebase_verifier_override(mock_verifier)

    resp = test_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200

    # Ensure role was PRESERVED as viewer, not changed to owner
    memberships = sqlite_db.query(WorkspaceMembershipModel).filter(WorkspaceMembershipModel.user_id == viewer.id).all()
    assert len(memberships) == 1
    assert memberships[0].role == "viewer"


def test_concurrent_workspace_provisioning(sqlite_db):
    """
    Directly tests ensure_user_personal_workspace under simulated concurrent conditions
    to ensure savepoint rollback (nested transaction) handles conflict cleanly.
    """
    from app.api.deps import ensure_user_personal_workspace

    user = UserModel(
        id=uuid.uuid4(),
        email="concurrent.user@example.com",
        full_name="Concurrent User",
        email_verified=True,
        is_active=True,
    )
    sqlite_db.add(user)
    sqlite_db.commit()

    # Call ensure_user_personal_workspace twice
    ws1 = ensure_user_personal_workspace(user, sqlite_db)
    sqlite_db.commit()

    ws2 = ensure_user_personal_workspace(user, sqlite_db)
    sqlite_db.commit()

    assert ws1.id == ws2.id
    memberships = sqlite_db.query(WorkspaceMembershipModel).filter(WorkspaceMembershipModel.user_id == user.id).all()
    assert len(memberships) == 1


def test_cors_headers_on_auth_errors(test_client, sqlite_db):
    """
    Verifies that CORS headers (access-control-allow-origin) are present on expected
    authentication errors (401, 403, 409) when originating from the production Vercel frontend.
    This prevents the browser from discarding error details and showing 'Failed to fetch'.
    """
    origin_header = {"Origin": "https://sebi-circular-rag-assistant.vercel.app"}

    # 1. 401 Unauthorized (Invalid token)
    def mock_failing_verifier(raw_token):
        raise ValueError("Invalid token")
    set_firebase_verifier_override(mock_failing_verifier)

    resp_401 = test_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer bad-token", **origin_header},
    )
    assert resp_401.status_code == 401
    assert resp_401.headers.get("access-control-allow-origin") == "https://sebi-circular-rag-assistant.vercel.app"

    # 2. 403 Forbidden (Unverified email)
    def mock_unverified_verifier(raw_token):
        return {
            "uid": "fb_unverified_cors",
            "email": "cors.unverified@example.com",
            "email_verified": False,
        }
    set_firebase_verifier_override(mock_unverified_verifier)

    resp_403 = test_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer unverified-cors-token", **origin_header},
    )
    assert resp_403.status_code == 403
    assert resp_403.headers.get("access-control-allow-origin") == "https://sebi-circular-rag-assistant.vercel.app"

    # 3. 409 Conflict (Legacy account collision)
    legacy_user = UserModel(
        id=uuid.uuid4(),
        email="cors.legacy@example.com",
        hashed_password=hash_password("Pass123!"),
        firebase_uid=None,
        email_verified=True,
    )
    sqlite_db.add(legacy_user)
    sqlite_db.commit()

    def mock_legacy_verifier(raw_token):
        return {
            "uid": "fb_cors_legacy_collision",
            "email": "cors.legacy@example.com",
            "email_verified": True,
        }
    set_firebase_verifier_override(mock_legacy_verifier)

    resp_409 = test_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer legacy-collision-token", **origin_header},
    )
    assert resp_409.status_code == 409
    assert resp_409.headers.get("access-control-allow-origin") == "https://sebi-circular-rag-assistant.vercel.app"

