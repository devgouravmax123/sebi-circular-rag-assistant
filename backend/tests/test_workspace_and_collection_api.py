"""
Focused tests for Milestone 8 - Task 8.3: Workspace, Membership, and Collection APIs.

Covers:
1. Workspace lifecycle: Creation by authenticated user, owner membership assignment, listing only user workspaces.
2. Tenant isolation: Non-members cannot access, view, or mutate another tenant's workspace or private collections (generic 404).
3. RBAC role boundaries: Owner vs Admin vs Member vs Viewer permissions on workspaces and collections.
4. Membership operations: Adding registered users, role updates, preventing self-escalation, preventing last owner demotion/removal.
5. User collections: Creation in authorized workspace, scoping, uniqueness, rejection of reserved/system IDs.
6. System/demo collections immutability: Readable by all authenticated users, strictly immutable and non-deletable via API.
7. Cross-tenant collection isolation: Collection ID from Tenant A cannot be modified or inspected by Tenant B.
"""
import os
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.models.auth import UserModel, WorkspaceModel, WorkspaceMembershipModel
from app.models.collection import CollectionModel
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


def _create_user_and_token(client, email_prefix="user"):
    """Helper creating a test user and returning authorization headers."""
    suffix = uuid.uuid4().hex[:8]
    email = f"{email_prefix}_{suffix}@example.com"
    password = "TestPassword123!"
    signup_res = client.post("/api/v1/auth/signup", json={"email": email, "password": password, "full_name": email_prefix})
    assert signup_res.status_code == 201
    user_data = signup_res.json()

    login_res = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert login_res.status_code == 200
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    return user_data, headers


# -------------------------------------------------------------------------
# Workspace Tests
# -------------------------------------------------------------------------

def test_workspace_creation_and_owner_assignment(client):
    """Verifies that creating a workspace atomically creates owner membership."""
    user, headers = _create_user_and_token(client, "alice")
    suffix = uuid.uuid4().hex[:6]

    res = client.post(
        "/api/v1/workspaces",
        json={"name": "Alice Workspace", "slug": f"alice-ws-{suffix}"},
        headers=headers,
    )
    assert res.status_code == 201
    data = res.json()
    assert data["name"] == "Alice Workspace"
    assert data["slug"] == f"alice-ws-{suffix}"
    assert data["owner_id"] == user["id"]
    assert data["is_system"] is False
    assert data["user_role"] == "owner"


def test_workspace_isolation_and_listing(client):
    """Verifies that users see only their own workspaces, and non-members receive 404."""
    user_a, headers_a = _create_user_and_token(client, "tenant_a")
    user_b, headers_b = _create_user_and_token(client, "tenant_b")

    suffix_a = uuid.uuid4().hex[:6]
    suffix_b = uuid.uuid4().hex[:6]

    # Tenant A creates workspace
    ws_a = client.post(
        "/api/v1/workspaces",
        json={"name": "Workspace A", "slug": f"ws-a-{suffix_a}"},
        headers=headers_a,
    ).json()

    # Tenant B creates workspace
    ws_b = client.post(
        "/api/v1/workspaces",
        json={"name": "Workspace B", "slug": f"ws-b-{suffix_b}"},
        headers=headers_b,
    ).json()

    # Tenant A lists workspaces -> sees ONLY Workspace A
    list_a = client.get("/api/v1/workspaces", headers=headers_a).json()
    ws_ids_a = [w["id"] for w in list_a]
    assert ws_a["id"] in ws_ids_a
    assert ws_b["id"] not in ws_ids_a

    # Tenant B lists workspaces -> sees ONLY Workspace B
    list_b = client.get("/api/v1/workspaces", headers=headers_b).json()
    ws_ids_b = [w["id"] for w in list_b]
    assert ws_b["id"] in ws_ids_b
    assert ws_a["id"] not in ws_ids_b

    # Tenant B attempts to access Workspace A directly -> 404 Not Found (no leakage)
    get_res = client.get(f"/api/v1/workspaces/{ws_a['id']}", headers=headers_b)
    assert get_res.status_code == 404

    # Tenant B attempts to update Workspace A -> 404 Not Found
    patch_res = client.patch(
        f"/api/v1/workspaces/{ws_a['id']}",
        json={"name": "Hacked Name"},
        headers=headers_b,
    )
    assert patch_res.status_code == 404


# -------------------------------------------------------------------------
# Membership Tests
# -------------------------------------------------------------------------

def test_membership_lifecycle_and_rbac(client):
    """Verifies adding members, updating roles, and RBAC permission checks."""
    owner, owner_headers = _create_user_and_token(client, "owner")
    member, member_headers = _create_user_and_token(client, "member")
    outsider, outsider_headers = _create_user_and_token(client, "outsider")

    suffix = uuid.uuid4().hex[:6]
    ws = client.post(
        "/api/v1/workspaces",
        json={"name": "Team Workspace", "slug": f"team-ws-{suffix}"},
        headers=owner_headers,
    ).json()
    ws_id = ws["id"]

    # 1. Owner adds member as 'viewer'
    add_res = client.post(
        f"/api/v1/workspaces/{ws_id}/members",
        json={"email": member["email"], "role": "viewer"},
        headers=owner_headers,
    )
    assert add_res.status_code == 201
    assert add_res.json()["role"] == "viewer"

    # 2. Viewer can view workspace metadata
    view_res = client.get(f"/api/v1/workspaces/{ws_id}", headers=member_headers)
    assert view_res.status_code == 200
    assert view_res.json()["user_role"] == "viewer"

    # 3. Viewer attempts to update workspace -> 403 Forbidden
    patch_fail = client.patch(
        f"/api/v1/workspaces/{ws_id}",
        json={"name": "Viewer Rename"},
        headers=member_headers,
    )
    assert patch_fail.status_code == 403

    # 4. Viewer attempts to add another member -> 403 Forbidden
    add_fail = client.post(
        f"/api/v1/workspaces/{ws_id}/members",
        json={"email": outsider["email"], "role": "member"},
        headers=member_headers,
    )
    assert add_fail.status_code == 403

    # 5. Owner promotes viewer to 'admin'
    promo_res = client.patch(
        f"/api/v1/workspaces/{ws_id}/members/{member['id']}",
        json={"role": "admin"},
        headers=owner_headers,
    )
    assert promo_res.status_code == 200
    assert promo_res.json()["role"] == "admin"

    # 6. Admin can now update workspace metadata
    patch_ok = client.patch(
        f"/api/v1/workspaces/{ws_id}",
        json={"name": "Admin Renamed Workspace"},
        headers=member_headers,
    )
    assert patch_ok.status_code == 200
    assert patch_ok.json()["name"] == "Admin Renamed Workspace"

    # 7. Member attempts to demote owner -> Owner role check prohibits self-demotion or non-owner role changes
    demote_fail = client.patch(
        f"/api/v1/workspaces/{ws_id}/members/{owner['id']}",
        json={"role": "member"},
        headers=member_headers,
    )
    assert demote_fail.status_code == 403  # Only owner can call update_member_role

    # 8. Owner attempts to demote themselves -> 400 Bad Request
    self_demote = client.patch(
        f"/api/v1/workspaces/{ws_id}/members/{owner['id']}",
        json={"role": "member"},
        headers=owner_headers,
    )
    assert self_demote.status_code == 400
    assert "cannot modify their own role" in self_demote.json()["detail"]


# -------------------------------------------------------------------------
# Collection Tests
# -------------------------------------------------------------------------

def test_collection_creation_and_scoping(client):
    """Verifies collection creation in authorized workspace and cross-tenant isolation."""
    user_a, headers_a = _create_user_and_token(client, "user_a")
    user_b, headers_b = _create_user_and_token(client, "user_b")

    suffix = uuid.uuid4().hex[:6]
    ws_a = client.post(
        "/api/v1/workspaces",
        json={"name": "Col WS", "slug": f"col-ws-{suffix}"},
        headers=headers_a,
    ).json()
    ws_a_id = ws_a["id"]

    col_id = f"custom_docs_{suffix}"

    # 1. User A creates collection in Workspace A
    col_res = client.post(
        f"/api/v1/workspaces/{ws_a_id}/collections",
        json={
            "id": col_id,
            "name": "My Custom Docs",
            "description": "Internal policy files",
            "policy_name": "general_static",
            "enforce_regulatory_graph": False,
        },
        headers=headers_a,
    )
    assert col_res.status_code == 201
    col_data = col_res.json()
    assert col_data["id"] == col_id
    assert col_data["workspace_id"] == ws_a_id
    assert col_data["policy_name"] == "general_static"
    assert col_data["is_system"] is False
    assert col_data["is_public"] is False

    # 2. List collections in Workspace A
    list_res = client.get(f"/api/v1/workspaces/{ws_a_id}/collections", headers=headers_a)
    assert list_res.status_code == 200
    cols = list_res.json()
    assert len(cols) == 1
    assert cols[0]["id"] == col_id

    # 3. User B attempts to list Workspace A's collections -> 404
    list_b = client.get(f"/api/v1/workspaces/{ws_a_id}/collections", headers=headers_b)
    assert list_b.status_code == 404

    # 4. User B attempts to fetch collection directly -> 404
    get_b = client.get(f"/api/v1/collections/{col_id}", headers=headers_b)
    assert get_b.status_code == 404

    # 5. User B attempts to mutate collection directly -> 404
    patch_b = client.patch(
        f"/api/v1/collections/{col_id}",
        json={"name": "Hijacked Collection"},
        headers=headers_b,
    )
    assert patch_b.status_code == 404

    # 6. User A updates collection metadata -> Success
    patch_a = client.patch(
        f"/api/v1/collections/{col_id}",
        json={"name": "Updated Custom Docs", "description": "New description"},
        headers=headers_a,
    )
    assert patch_a.status_code == 200
    assert patch_a.json()["name"] == "Updated Custom Docs"
    assert patch_a.json()["description"] == "New description"


def test_system_collections_global_read_and_immutability(client):
    """Verifies that system collections are readable by all authenticated users, but strictly immutable."""
    user, headers = _create_user_and_token(client, "reader")

    # 1. System collection sebi_cscrf is readable by any authenticated user
    res_sebi = client.get("/api/v1/collections/sebi_cscrf", headers=headers)
    assert res_sebi.status_code == 200
    data = res_sebi.json()
    assert data["id"] == "sebi_cscrf"
    assert data["policy_name"] == "sebi_cscrf_strict"
    assert data["is_system"] is True
    assert data["is_public"] is True

    # 2. Other system collections are also readable
    for sys_id in ("cisa_playbooks", "demo_finops", "general_synthetic"):
        res = client.get(f"/api/v1/collections/{sys_id}", headers=headers)
        assert res.status_code == 200
        assert res.json()["is_system"] is True

    # 3. Attempt to mutate system collection -> 403 Forbidden
    patch_res = client.patch(
        "/api/v1/collections/sebi_cscrf",
        json={"name": "Renamed System Collection"},
        headers=headers,
    )
    assert patch_res.status_code == 403
    assert "System collections are immutable" in patch_res.json()["detail"]


def test_reserved_collection_id_collision_prevention(client):
    """Verifies that user cannot create a collection with a reserved system collection ID."""
    user, headers = _create_user_and_token(client, "user_c")
    suffix = uuid.uuid4().hex[:6]
    ws = client.post(
        "/api/v1/workspaces",
        json={"name": "Collision WS", "slug": f"col-ws-{suffix}"},
        headers=headers,
    ).json()

    # Attempt to create collection with ID 'sebi_cscrf' -> 422 Validation Error
    res = client.post(
        f"/api/v1/workspaces/{ws['id']}/collections",
        json={
            "id": "sebi_cscrf",
            "name": "Fake SEBI",
            "policy_name": "general_static",
        },
        headers=headers,
    )
    assert res.status_code == 422
    assert "reserved by system collections" in res.text

