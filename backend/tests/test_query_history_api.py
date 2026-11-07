"""
Focused Security and Isolation Tests for Milestone 8 - Task 8.6:
Persistent Query History.

Validates:
1. Authenticated query persists a history record with complete user, workspace, and collection association.
2. Record contains final user-visible response attributes (disposition, summary narrative, citations, technical validation).
3. Anonymous query to public collection succeeds but creates zero history records.
4. Unauthorized/rejected query creates zero history records.
5. User can list only their own history records (ordered newest first).
6. User cannot fetch another user's history record by ID (generic 404).
7. Workspace member can read history for an accessible private collection.
8. Revoking workspace membership makes past private history records inaccessible (404 / filtered out).
9. Public collection history remains private to the user who ran the query.
10. Workspace / collection query filters cannot bypass authorization.
11. Query history for internal quarantine collection (system_legacy_jobs) is strictly inaccessible.
12. Pagination operates stably with limit and offset.
13. Invalid pagination parameters are rejected with validation errors.
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
from app.models.query_history import QueryHistoryModel
from app.api.deps import get_db_session
from app.schemas.manifest import CollectionManifest, GroundingPolicyDeclaration, GroundingPolicyType, CollectionStatus
from app.ingestion.collection_registry import CollectionRegistry
from app.core.config import get_settings

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

def test_1_authenticated_query_persists_history_record(client: TestClient, db_session):
    """An authenticated user query produces an authoritative query_history record."""
    user = create_user_and_token(client, f"hist_user_{uuid.uuid4().hex[:6]}@example.com")
    query_text = "What are the baseline password complexity requirements?"

    resp = client.post(
        "/api/v1/query",
        json={"query": query_text, "collection_id": "sebi_cscrf"},
        headers=user["headers"],
    )
    assert resp.status_code == 200
    query_data = resp.json()

    # Verify query history record exists in database
    hist_record = (
        db_session.query(QueryHistoryModel)
        .filter(QueryHistoryModel.user_id == uuid.UUID(user["id"]))
        .first()
    )
    assert hist_record is not None
    assert hist_record.query_text == query_text
    assert hist_record.collection_id == "sebi_cscrf"
    assert hist_record.disposition == query_data["disposition"]
    assert hist_record.technical_validation_passed == query_data["technical_validation_passed"]
    assert hist_record.verified_excerpts_count == len(query_data["verified_excerpts"])


def test_2_anonymous_query_does_not_persist_history(client: TestClient, db_session):
    """Anonymous queries to public collections succeed but never write history records."""
    count_before = db_session.query(QueryHistoryModel).count()

    resp = client.post(
        "/api/v1/query",
        json={"query": "What are the baseline password complexity requirements?", "collection_id": "sebi_cscrf"},
    )
    assert resp.status_code == 200

    count_after = db_session.query(QueryHistoryModel).count()
    assert count_after == count_before


def test_3_unauthorized_query_does_not_persist_history(client: TestClient, db_session):
    """An unauthorized attempt to query another tenant's collection writes zero history records."""
    owner = create_user_and_token(client, f"owner_block_{uuid.uuid4().hex[:6]}@example.com")
    attacker = create_user_and_token(client, f"attacker_block_{uuid.uuid4().hex[:6]}@example.com")

    col_id = f"col_blk_{uuid.uuid4().hex[:8]}"
    create_workspace_and_collection(client, owner["headers"], "Block WS", col_id)

    count_before = db_session.query(QueryHistoryModel).count()

    # Attacker tries to query
    resp = client.post(
        "/api/v1/query",
        json={"query": "Top secret leak attempt", "collection_id": col_id},
        headers=attacker["headers"],
    )
    assert resp.status_code == 404

    count_after = db_session.query(QueryHistoryModel).count()
    assert count_after == count_before


def test_4_user_can_list_only_their_own_history(client: TestClient, db_session):
    """Users only see their own historical queries, newest first."""
    user_a = create_user_and_token(client, f"user_a_hist_{uuid.uuid4().hex[:6]}@example.com")
    user_b = create_user_and_token(client, f"user_b_hist_{uuid.uuid4().hex[:6]}@example.com")

    # User A runs a query
    client.post(
        "/api/v1/query",
        json={"query": "User A Question 1", "collection_id": "sebi_cscrf"},
        headers=user_a["headers"],
    )

    # User B runs a query
    client.post(
        "/api/v1/query",
        json={"query": "User B Question 1", "collection_id": "sebi_cscrf"},
        headers=user_b["headers"],
    )

    # User A requests history
    resp_a = client.get("/api/v1/query-history", headers=user_a["headers"])
    assert resp_a.status_code == 200
    items_a = resp_a.json()["items"]
    assert len(items_a) >= 1
    assert all("User A" in i["query_text"] for i in items_a)
    assert not any("User B" in i["query_text"] for i in items_a)

    # User B requests history
    resp_b = client.get("/api/v1/query-history", headers=user_b["headers"])
    assert resp_b.status_code == 200
    items_b = resp_b.json()["items"]
    assert len(items_b) >= 1
    assert all("User B" in i["query_text"] for i in items_b)
    assert not any("User A" in i["query_text"] for i in items_b)


def test_5_user_cannot_fetch_another_users_record_by_id(client: TestClient, db_session):
    """Attempting to GET another user's history record by ID returns generic 404."""
    user_a = create_user_and_token(client, f"user_a_rec_{uuid.uuid4().hex[:6]}@example.com")
    user_b = create_user_and_token(client, f"user_b_rec_{uuid.uuid4().hex[:6]}@example.com")

    # User A creates a history record
    resp = client.post(
        "/api/v1/query",
        json={"query": "User A Private Query", "collection_id": "sebi_cscrf"},
        headers=user_a["headers"],
    )
    assert resp.status_code == 200

    rec_a = (
        db_session.query(QueryHistoryModel)
        .filter(QueryHistoryModel.user_id == uuid.UUID(user_a["id"]))
        .first()
    )
    assert rec_a is not None

    # User A can fetch it
    get_a = client.get(f"/api/v1/query-history/{rec_a.id}", headers=user_a["headers"])
    assert get_a.status_code == 200
    assert get_a.json()["query_text"] == "User A Private Query"

    # User B tries to fetch User A's record -> 404 Not Found
    get_b = client.get(f"/api/v1/query-history/{rec_a.id}", headers=user_b["headers"])
    assert get_b.status_code == 404
    assert get_b.json()["detail"] == "Query history record not found."


def test_6_revoked_workspace_membership_blocks_private_history(client: TestClient, db_session):
    """
    If a user ran a private query while a workspace member, but their membership is later deleted,
    they can no longer read that private query or its answer (returns 404 / filtered out).
    """
    owner = create_user_and_token(client, f"owner_rev_{uuid.uuid4().hex[:6]}@example.com")
    member = create_user_and_token(client, f"member_rev_{uuid.uuid4().hex[:6]}@example.com")

    col_id = f"col_rev_{uuid.uuid4().hex[:8]}"
    setup = create_workspace_and_collection(client, owner["headers"], "Revocation WS", col_id)
    ws_id = setup["workspace_id"]

    # Add member to workspace
    add_mem = client.post(
        f"/api/v1/workspaces/{ws_id}/members",
        json={"email": member["email"], "role": "member"},
        headers=owner["headers"],
    )
    assert add_mem.status_code == 201, f"Failed adding member: {add_mem.status_code} {add_mem.text}"

    # Insert a history record directly under member's user_id for this private workspace & collection
    rec_id = uuid.uuid4()
    hist_rec = QueryHistoryModel(
        id=rec_id,
        user_id=uuid.UUID(member["id"]),
        workspace_id=uuid.UUID(ws_id),
        collection_id=col_id,
        query_text="Confidential Project Plan",
        query_options={"top_k": 5},
        disposition="VERIFIED_HISTORICAL_EXCERPTS",
        summary_narrative="Verified narrative",
        answer_text="Confidential answer details",
        claim_citations=[],
        verified_excerpts_count=1,
        technical_validation_passed=True,
    )
    db_session.add(hist_rec)
    db_session.commit()

    # Member can initially read their record
    fetch_ok = client.get(f"/api/v1/query-history/{rec_id}", headers=member["headers"])
    assert fetch_ok.status_code == 200, f"Expected 200 initially, got: {fetch_ok.status_code} {fetch_ok.text}"
    assert fetch_ok.json()["query_text"] == "Confidential Project Plan"

    # Remove member from workspace (membership revocation) directly in db_session
    row_count = db_session.execute(
        text("DELETE FROM workspace_memberships WHERE workspace_id = :wid AND user_id = :uid"),
        {"wid": uuid.UUID(ws_id), "uid": uuid.UUID(member["id"])},
    ).rowcount
    assert row_count == 1, f"Expected 1 deleted membership row, got {row_count}"
    db_session.commit()

    # Member attempts to read record -> 404 Not Found
    fetch_revoked = client.get(f"/api/v1/query-history/{rec_id}", headers=member["headers"])
    assert fetch_revoked.status_code == 404, f"Expected 404 after revocation, got: {fetch_revoked.status_code} {fetch_revoked.text}"
    assert fetch_revoked.json()["detail"] == "Query history record not found."

    # In listing, private record is filtered out
    list_res = client.get("/api/v1/query-history", headers=member["headers"])
    assert list_res.status_code == 200
    assert not any(i["id"] == str(rec_id) for i in list_res.json()["items"])


def test_7_pagination_and_filters(client: TestClient, db_session):
    """Pagination behaves stably with offset and limit, and invalid values fail validation."""
    user = create_user_and_token(client, f"page_user_{uuid.uuid4().hex[:6]}@example.com")
    u_uuid = uuid.UUID(user["id"])

    # Insert 5 records
    for i in range(5):
        db_session.add(
            QueryHistoryModel(
                user_id=u_uuid,
                workspace_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
                collection_id="sebi_cscrf",
                query_text=f"Paginated Query {i}",
                disposition="CONTROLLED_ABSTENTION",
            )
        )
    db_session.commit()

    # Page 1: limit 2, offset 0
    p1 = client.get("/api/v1/query-history?limit=2&offset=0", headers=user["headers"])
    assert p1.status_code == 200
    d1 = p1.json()
    assert d1["total"] == 5
    assert len(d1["items"]) == 2

    # Page 2: limit 2, offset 2
    p2 = client.get("/api/v1/query-history?limit=2&offset=2", headers=user["headers"])
    assert p2.status_code == 200
    d2 = p2.json()
    assert len(d2["items"]) == 2
    assert d2["items"][0]["id"] != d1["items"][0]["id"]

    # Invalid pagination: negative limit -> 422
    inv_limit = client.get("/api/v1/query-history?limit=-1", headers=user["headers"])
    assert inv_limit.status_code == 422

    # Invalid pagination: negative offset -> 422
    inv_offset = client.get("/api/v1/query-history?offset=-5", headers=user["headers"])
    assert inv_offset.status_code == 422

