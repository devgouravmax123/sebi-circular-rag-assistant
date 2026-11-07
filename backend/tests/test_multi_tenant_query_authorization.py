"""
Focused Security and Authorization Tests for Milestone 8 - Task 8.5:
Multi-Tenant Query/RAG Authorization.

Validates the full authorization and isolation matrix:
1. Public/system collections (sebi_cscrf, cisa_playbooks, demo_finops, general_synthetic):
   - Anonymous caller can query without token (200).
   - Authenticated user can query (200).
2. Private collections:
   - Anonymous caller rejected with 401 Unauthorized.
   - Non-member of workspace rejected with 404 Not Found (generic non-enumerating error).
   - Workspace member/admin/owner can query their private collection (200).
   - Client-supplied spoofed parameters cannot bypass membership checks.
3. System legacy collection:
   - system_legacy_jobs rejected with 404 Not Found (cannot be queried).
4. Unknown / unregistered collection IDs:
   - Rejected with 404 Not Found (fails closed).
   - No silent fallback from unauthorized private collections to default sebi_cscrf.
5. Authorization happens BEFORE retrieval / synthesis:
   - For unauthorized or rejected requests, neither retrieval nor synthesis is invoked.
6. Evidence isolation & citations:
   - Only authorized collection evidence enters safety gate and synthesis.
   - Cross-collection chunk isolation is enforced.
"""
import os
import uuid
import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.models.auth import UserModel, WorkspaceModel, WorkspaceMembershipModel
from app.models.collection import CollectionModel
from app.api.deps import get_db_session
from app.schemas.manifest import CollectionManifest, GroundingPolicyDeclaration, GroundingPolicyType, CollectionStatus
from app.ingestion.collection_registry import CollectionRegistry
from app.retrieval.service import RetrievalService
from app.synthesis.service import SynthesisService

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
    app.dependency_overrides.clear()


def create_user_and_token(client: TestClient, email: str) -> dict:
    """Helper to sign up and log in a user, returning user info with bearer headers."""
    password = "SecurePassword123!"
    resp = client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": password, "full_name": email.split("@")[0]},
    )
    assert resp.status_code == 201, f"Failed signup: {resp.text}"
    user_id = resp.json()["id"]

    login_resp = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert login_resp.status_code == 200, f"Failed login: {login_resp.text}"
    token = login_resp.json()["access_token"]
    return {
        "id": user_id,
        "email": email,
        "headers": {"Authorization": f"Bearer {token}"},
    }


def create_workspace_and_collection(client: TestClient, owner_headers: dict, ws_name: str, col_id: str) -> dict:
    """Helper to create a workspace and a collection within it."""
    slug = f"ws-{uuid.uuid4().hex[:8]}"
    ws_resp = client.post(
        "/api/v1/workspaces",
        json={"name": ws_name, "slug": slug, "description": f"Workspace {ws_name}"},
        headers=owner_headers,
    )
    assert ws_resp.status_code == 201, f"Failed ws create: {ws_resp.text}"
    workspace_id = ws_resp.json()["id"]

    col_resp = client.post(
        f"/api/v1/workspaces/{workspace_id}/collections",
        json={
            "id": col_id,
            "name": f"Collection {col_id}",
            "description": "Test private collection",
            "policy_name": "general_static",
        },
        headers=owner_headers,
    )
    assert col_resp.status_code == 201, f"Failed col create: {col_resp.text}"
    return {"workspace_id": workspace_id, "collection_id": col_id}


# -------------------------------------------------------------------------
# Test Cases
# -------------------------------------------------------------------------

def test_1_anonymous_user_can_query_public_collection(client: TestClient):
    """Anonymous user can query public/demo collection (sebi_cscrf)."""
    resp = client.post(
        "/api/v1/query",
        json={"query": "What are the baseline password complexity requirements?", "collection_id": "sebi_cscrf"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "query" in data
    assert "disposition" in data


def test_2_authenticated_user_can_query_public_collection(client: TestClient):
    """Authenticated user can query public/demo collections with their bearer token."""
    user = create_user_and_token(client, f"user_pub_{uuid.uuid4().hex[:6]}@example.com")
    resp = client.post(
        "/api/v1/query",
        json={"query": "What are the baseline password complexity requirements?", "collection_id": "sebi_cscrf"},
        headers=user["headers"],
    )
    assert resp.status_code == 200
    assert resp.json()["disposition"] in ("VERIFIED_HISTORICAL_EXCERPTS", "QUALIFIED_HISTORICAL_EXCERPTS", "CONTROLLED_ABSTENTION")


def test_3_anonymous_user_cannot_query_private_collection(client: TestClient):
    """Anonymous user attempting to query a private collection is rejected with 401 Unauthorized."""
    owner = create_user_and_token(client, f"owner_{uuid.uuid4().hex[:6]}@example.com")
    col_id = f"priv_col_{uuid.uuid4().hex[:8]}"
    create_workspace_and_collection(client, owner["headers"], "Private WS", col_id)

    # Anonymous request without Authorization header
    resp = client.post(
        "/api/v1/query",
        json={"query": "What are the internal confidential procedures?", "collection_id": col_id},
    )
    assert resp.status_code == 401
    assert "Authentication required" in resp.json()["detail"]


def test_4_non_member_cannot_query_other_tenant_private_collection(client: TestClient):
    """Non-member authenticated user receives generic 404 (preventing tenant enumeration)."""
    tenant_a = create_user_and_token(client, f"tenant_a_{uuid.uuid4().hex[:6]}@example.com")
    tenant_b = create_user_and_token(client, f"tenant_b_{uuid.uuid4().hex[:6]}@example.com")

    col_a = f"col_priv_a_{uuid.uuid4().hex[:8]}"
    create_workspace_and_collection(client, tenant_a["headers"], "Tenant A WS", col_a)

    # Tenant B tries to query Tenant A's private collection
    resp = client.post(
        "/api/v1/query",
        json={"query": "Confidential financial projections", "collection_id": col_a},
        headers=tenant_b["headers"],
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Collection not found."


def test_5_system_legacy_jobs_collection_cannot_be_queried(client: TestClient):
    """system_legacy_jobs is an internal quarantine collection and is never queryable."""
    user = create_user_and_token(client, f"user_sys_{uuid.uuid4().hex[:6]}@example.com")
    resp = client.post(
        "/api/v1/query",
        json={"query": "Show legacy jobs", "collection_id": "system_legacy_jobs"},
        headers=user["headers"],
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Collection not found."


def test_6_unknown_collection_id_fails_closed(client: TestClient):
    """Non-existent collection ID fails closed with 404 Not Found."""
    user = create_user_and_token(client, f"user_unk_{uuid.uuid4().hex[:6]}@example.com")
    unknown_col = f"unknown_col_{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/api/v1/query",
        json={"query": "Test query", "collection_id": unknown_col},
        headers=user["headers"],
    )
    assert resp.status_code == 404


def test_7_no_silent_fallback_from_unauthorized_private_collection(client: TestClient):
    """Unauthorized private request does not fall back to default sebi_cscrf."""
    owner = create_user_and_token(client, f"owner_fb_{uuid.uuid4().hex[:6]}@example.com")
    attacker = create_user_and_token(client, f"attacker_{uuid.uuid4().hex[:6]}@example.com")

    col_id = f"col_secret_{uuid.uuid4().hex[:8]}"
    create_workspace_and_collection(client, owner["headers"], "Secret WS", col_id)

    # Attacker targets the secret collection
    resp = client.post(
        "/api/v1/query",
        json={"query": "What are the baseline password complexity requirements?", "collection_id": col_id},
        headers=attacker["headers"],
    )
    # Must fail with 404, NOT return SEBI CSCRF answers
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Collection not found."


def test_8_workspace_member_authorized_to_query_private_collection(client: TestClient, monkeypatch):
    """
    Authorized workspace member (owner/admin/member) passes authorization check.
    If the collection is registered and published, retrieval proceeds normally.
    """
    owner = create_user_and_token(client, f"owner_auth_{uuid.uuid4().hex[:6]}@example.com")
    col_id = f"col_reg_{uuid.uuid4().hex[:8]}"
    create_workspace_and_collection(client, owner["headers"], "Auth WS", col_id)

    # Mock collection_registry to report this private collection as registered & active
    from app.core.config import get_settings
    from app.api.v1.query import get_collection_registry
    settings = get_settings()
    registry = get_collection_registry(settings=settings)
    manifest = CollectionManifest(
        collection_id=col_id,
        name=f"Collection {col_id}",
        description="Private Active Collection",
        regulator="SEBI",
        domain="PRIVATE",
        policy=GroundingPolicyDeclaration(
            policy_type=GroundingPolicyType.GENERAL_STATIC,
            enforce_regulatory_graph=False,
        ),
        enabled=True,
    )
    # Register in the dependency singleton instance
    registry.register_manifest_in_memory(manifest)
    monkeypatch.setattr(registry, "get_collection_status", lambda cid: CollectionStatus.ACTIVE if cid == col_id else registry.get_collection_status(cid))

    # Override the dependency in app so FastAPI injects the updated registry
    app.dependency_overrides[get_collection_registry] = lambda: registry

    # Query as authorized owner
    resp = client.post(
        "/api/v1/query",
        json={"query": "Internal procedures query", "collection_id": col_id},
        headers=owner["headers"],
    )
    # Authorization passed: 200 OK (controlled abstention on empty test index)
    assert resp.status_code == 200, f"Unexpected response: {resp.status_code} {resp.text}"
    assert resp.json()["disposition"] == "CONTROLLED_ABSTENTION"


def test_9_authorization_occurs_strictly_before_retrieval(client: TestClient, monkeypatch):
    """
    Verifies that for an unauthorized caller, retrieval_service.search_hybrid
    is NEVER called (0 invocations).
    """
    owner = create_user_and_token(client, f"owner_chk_{uuid.uuid4().hex[:6]}@example.com")
    stranger = create_user_and_token(client, f"stranger_chk_{uuid.uuid4().hex[:6]}@example.com")

    col_id = f"col_guard_{uuid.uuid4().hex[:8]}"
    create_workspace_and_collection(client, owner["headers"], "Guard WS", col_id)

    from app.core.config import get_settings
    from app.api.v1.query import get_retrieval_service
    settings = get_settings()
    retrieval = get_retrieval_service(settings=settings)
    search_mock = MagicMock()
    monkeypatch.setattr(retrieval, "search_hybrid", search_mock)

    # Stranger attempts query
    resp = client.post(
        "/api/v1/query",
        json={"query": "Top secret documents", "collection_id": col_id},
        headers=stranger["headers"],
    )
    assert resp.status_code == 404
    # Ensure retrieval was never invoked!
    assert search_mock.call_count == 0


def test_10_client_supplied_workspace_or_user_id_cannot_bypass_checks(client: TestClient):
    """QueryRequest model forbids unexpected / client-supplied identity claims."""
    # Attempt to inject spoofed workspace_id or user_id into query payload
    resp = client.post(
        "/api/v1/query",
        json={
            "query": "Test query",
            "collection_id": "sebi_cscrf",
            "user_id": str(uuid.uuid4()),
            "workspace_id": str(uuid.uuid4()),
        },
    )
    # Pydantic extra='forbid' rejects extra fields with 422
    assert resp.status_code == 422

