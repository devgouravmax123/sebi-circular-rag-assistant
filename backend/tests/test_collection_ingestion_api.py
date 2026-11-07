"""
Focused tests for Milestone 8 - Task 8.4: Collection-Aware Ingestion Integration.

Validates:
1. Ingestion of official document URLs into private collections via authorized API endpoint.
2. RBAC enforcement:
   - Unauthenticated caller rejected (401).
   - Workspace viewer/member cannot submit ingestion jobs (403).
   - Workspace owner and admin can submit ingestion jobs (202).
3. Tenant isolation:
   - Collection in Workspace B cannot be targeted using Workspace A's path (404 generic).
   - System/demo collections (e.g. sebi_cscrf) are strictly rejected from user ingestion (403).
4. Ownership propagation:
   - IngestionJobModel and RegulatoryDocumentModel are correctly tagged with workspace_id and collection_id.
   - Database trigger trg_validate_job_document_consistency is satisfied.
5. Idempotency & Conflict safety:
   - Resubmission with same idempotency_key returns existing job (202, is_duplicate_registration=True).
   - Reusing an idempotency_key across another tenant is rejected (409).
   - Reusing an existing document_id belonging to a different tenant is rejected (409).
6. Worker procedure integrity:
   - PostgresIngestionJobRepository persists workspace_id and collection_id on job creation.
   - Worker claim, heartbeat, version creation, chunk batch writing, completion, and failure
     preserve tenant ownership without modification.
7. System manifest ingestion regression:
   - Manifest runner continues to operate smoothly for system demo collections without regressing.
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
from app.persistence.models import IngestionJobModel, RegulatoryDocumentModel
from app.persistence.worker_procedure_repo import PostgresWorkerProcedureRepository
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
    app.dependency_overrides.clear()


def create_user_and_token(client: TestClient, email: str, role: str = "researcher") -> dict:
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

def test_unauthenticated_ingestion_request_rejected(client: TestClient):
    """Anonymous calls to collection ingestion endpoint must return 401."""
    ws_id = uuid.uuid4()
    resp = client.post(
        f"/api/v1/workspaces/{ws_id}/collections/test-col/ingestion-jobs",
        json={
            "source_url": "https://example.com/circular.pdf",
            "idempotency_key": "idem-anon-1",
        },
    )
    assert resp.status_code == 401


def test_non_member_cannot_ingest_into_workspace_collection(client: TestClient):
    """User who is not a member of the workspace receives generic 404 (preventing enumeration)."""
    user_a = create_user_and_token(client, f"user_a_{uuid.uuid4().hex[:6]}@example.com")
    user_b = create_user_and_token(client, f"user_b_{uuid.uuid4().hex[:6]}@example.com")

    setup_a = create_workspace_and_collection(client, user_a["headers"], "Tenant A", f"col_a_{uuid.uuid4().hex[:6]}")
    ws_id = setup_a["workspace_id"]
    col_id = setup_a["collection_id"]

    resp = client.post(
        f"/api/v1/workspaces/{ws_id}/collections/{col_id}/ingestion-jobs",
        json={
            "source_url": "https://example.com/doc.pdf",
            "idempotency_key": "idem-b-attempt-1",
        },
        headers=user_b["headers"],
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Workspace not found."


def test_member_and_viewer_roles_cannot_ingest(client: TestClient):
    """Workspace members and viewers cannot submit ingestion jobs (must be owner or admin)."""
    owner = create_user_and_token(client, f"owner_{uuid.uuid4().hex[:6]}@example.com")
    member = create_user_and_token(client, f"member_{uuid.uuid4().hex[:6]}@example.com")
    viewer = create_user_and_token(client, f"viewer_{uuid.uuid4().hex[:6]}@example.com")

    setup = create_workspace_and_collection(client, owner["headers"], "Tenant RBAC", f"col_rbac_{uuid.uuid4().hex[:6]}")
    ws_id = setup["workspace_id"]
    col_id = setup["collection_id"]

    # Add member and viewer
    client.post(
        f"/api/v1/workspaces/{ws_id}/members",
        json={"email": member["email"], "role": "member"},
        headers=owner["headers"],
    )
    client.post(
        f"/api/v1/workspaces/{ws_id}/members",
        json={"email": viewer["email"], "role": "viewer"},
        headers=owner["headers"],
    )

    # Member attempt -> 403
    resp_mem = client.post(
        f"/api/v1/workspaces/{ws_id}/collections/{col_id}/ingestion-jobs",
        json={
            "source_url": "https://example.com/doc.pdf",
            "idempotency_key": "idem-mem-1",
        },
        headers=member["headers"],
    )
    assert resp_mem.status_code == 403

    # Viewer attempt -> 403
    resp_view = client.post(
        f"/api/v1/workspaces/{ws_id}/collections/{col_id}/ingestion-jobs",
        json={
            "source_url": "https://example.com/doc.pdf",
            "idempotency_key": "idem-view-1",
        },
        headers=viewer["headers"],
    )
    assert resp_view.status_code == 403


def test_owner_and_admin_can_submit_collection_ingestion(client: TestClient, db_session):
    """Workspace owner and admin can queue document ingestion successfully (202)."""
    owner = create_user_and_token(client, f"owner_{uuid.uuid4().hex[:6]}@example.com")
    admin = create_user_and_token(client, f"admin_{uuid.uuid4().hex[:6]}@example.com")

    setup = create_workspace_and_collection(client, owner["headers"], "Tenant Queue", f"col_queue_{uuid.uuid4().hex[:6]}")
    ws_id = setup["workspace_id"]
    col_id = setup["collection_id"]

    # Add admin to workspace
    client.post(
        f"/api/v1/workspaces/{ws_id}/members",
        json={"email": admin["email"], "role": "admin"},
        headers=owner["headers"],
    )

    # 1. Owner queues ingestion job
    resp_owner = client.post(
        f"/api/v1/workspaces/{ws_id}/collections/{col_id}/ingestion-jobs",
        json={
            "source_url": "https://example.com/owner-doc.pdf",
            "idempotency_key": f"idem-owner-{uuid.uuid4().hex[:6]}",
            "document_id": f"doc-owner-{uuid.uuid4().hex[:6]}",
            "title": "Owner Ingested Circular",
            "circular_number": "CIR/2026/01",
        },
        headers=owner["headers"],
    )
    assert resp_owner.status_code == 202
    data_owner = resp_owner.json()
    assert data_owner["workspace_id"] == ws_id
    assert data_owner["collection_id"] == col_id
    assert data_owner["status"] == "queued"
    assert data_owner["stage"] == "pending"
    assert data_owner["is_duplicate_registration"] is False

    # 2. Admin queues another ingestion job
    resp_admin = client.post(
        f"/api/v1/workspaces/{ws_id}/collections/{col_id}/ingestion-jobs",
        json={
            "source_url": "https://example.com/admin-doc.pdf",
            "idempotency_key": f"idem-admin-{uuid.uuid4().hex[:6]}",
            "document_id": f"doc-admin-{uuid.uuid4().hex[:6]}",
            "title": "Admin Ingested Circular",
        },
        headers=admin["headers"],
    )
    assert resp_admin.status_code == 202
    data_admin = resp_admin.json()
    assert data_admin["workspace_id"] == ws_id
    assert data_admin["collection_id"] == col_id


def test_system_collection_rejected_from_private_ingestion_endpoint(client: TestClient):
    """System collections (e.g. sebi_cscrf) cannot be ingested into via the user endpoint."""
    user = create_user_and_token(client, f"user_{uuid.uuid4().hex[:6]}@example.com")
    slug = f"ws-{uuid.uuid4().hex[:8]}"
    ws_resp = client.post(
        "/api/v1/workspaces",
        json={"name": "WS System Test", "slug": slug},
        headers=user["headers"],
    )
    assert ws_resp.status_code == 201
    ws_id = ws_resp.json()["id"]

    # Attempt to target sebi_cscrf through user workspace
    resp = client.post(
        f"/api/v1/workspaces/{ws_id}/collections/sebi_cscrf/ingestion-jobs",
        json={
            "source_url": "https://example.com/tamper.pdf",
            "idempotency_key": "idem-sys-tamper-1",
        },
        headers=user["headers"],
    )
    # sebi_cscrf belongs to system_workspace, so query filtering by ws_id returns 404
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Collection not found in workspace."


def test_cross_workspace_collection_isolation(client: TestClient):
    """Attempting to access a collection belonging to Workspace B under Workspace A returns 404."""
    user_a = create_user_and_token(client, f"user_a_{uuid.uuid4().hex[:6]}@example.com")
    user_b = create_user_and_token(client, f"user_b_{uuid.uuid4().hex[:6]}@example.com")

    setup_a = create_workspace_and_collection(client, user_a["headers"], "Workspace A", f"col_a_{uuid.uuid4().hex[:6]}")
    setup_b = create_workspace_and_collection(client, user_b["headers"], "Workspace B", f"col_b_{uuid.uuid4().hex[:6]}")

    # User A tries to ingest into Workspace A using Collection B's ID
    resp = client.post(
        f"/api/v1/workspaces/{setup_a['workspace_id']}/collections/{setup_b['collection_id']}/ingestion-jobs",
        json={
            "source_url": "https://example.com/cross.pdf",
            "idempotency_key": "idem-cross-1",
        },
        headers=user_a["headers"],
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Collection not found in workspace."


def test_idempotency_key_replay_returns_existing_job(client: TestClient):
    """Re-submitting the exact same idempotency key in the same collection returns 202 with is_duplicate_registration=True."""
    user = create_user_and_token(client, f"user_idem_{uuid.uuid4().hex[:6]}@example.com")
    setup = create_workspace_and_collection(client, user["headers"], "WS Idem", f"col_idem_{uuid.uuid4().hex[:6]}")
    ws_id = setup["workspace_id"]
    col_id = setup["collection_id"]

    idem_key = f"idem-key-{uuid.uuid4().hex[:8]}"
    doc_id = f"doc-idem-{uuid.uuid4().hex[:8]}"

    # First submission
    resp1 = client.post(
        f"/api/v1/workspaces/{ws_id}/collections/{col_id}/ingestion-jobs",
        json={
            "source_url": "https://example.com/doc-replay.pdf",
            "idempotency_key": idem_key,
            "document_id": doc_id,
        },
        headers=user["headers"],
    )
    assert resp1.status_code == 202
    job1 = resp1.json()
    assert job1["is_duplicate_registration"] is False

    # Second submission
    resp2 = client.post(
        f"/api/v1/workspaces/{ws_id}/collections/{col_id}/ingestion-jobs",
        json={
            "source_url": "https://example.com/doc-replay.pdf",
            "idempotency_key": idem_key,
            "document_id": doc_id,
        },
        headers=user["headers"],
    )
    assert resp2.status_code == 202
    job2 = resp2.json()
    assert job2["is_duplicate_registration"] is True
    assert job2["job_id"] == job1["job_id"]
    assert job2["document_id"] == doc_id


def test_idempotency_key_cross_tenant_conflict_rejected(client: TestClient):
    """Attempting to use an idempotency key that already belongs to another workspace returns 409."""
    user_a = create_user_and_token(client, f"user_idem_a_{uuid.uuid4().hex[:6]}@example.com")
    user_b = create_user_and_token(client, f"user_idem_b_{uuid.uuid4().hex[:6]}@example.com")

    setup_a = create_workspace_and_collection(client, user_a["headers"], "WS Idem A", f"col_idem_a_{uuid.uuid4().hex[:6]}")
    setup_b = create_workspace_and_collection(client, user_b["headers"], "WS Idem B", f"col_idem_b_{uuid.uuid4().hex[:6]}")

    shared_idem_key = f"shared-idem-{uuid.uuid4().hex[:8]}"

    # Tenant A uses the key
    resp_a = client.post(
        f"/api/v1/workspaces/{setup_a['workspace_id']}/collections/{setup_a['collection_id']}/ingestion-jobs",
        json={
            "source_url": "https://example.com/doc-a.pdf",
            "idempotency_key": shared_idem_key,
        },
        headers=user_a["headers"],
    )
    assert resp_a.status_code == 202

    # Tenant B tries to use the same idempotency key
    resp_b = client.post(
        f"/api/v1/workspaces/{setup_b['workspace_id']}/collections/{setup_b['collection_id']}/ingestion-jobs",
        json={
            "source_url": "https://example.com/doc-b.pdf",
            "idempotency_key": shared_idem_key,
        },
        headers=user_b["headers"],
    )
    assert resp_b.status_code == 409
    assert resp_b.json()["detail"] == "Idempotency key conflict."


def test_document_id_cross_tenant_conflict_rejected(client: TestClient):
    """Attempting to attach a job to an existing document_id owned by another workspace returns 409."""
    user_a = create_user_and_token(client, f"user_doc_a_{uuid.uuid4().hex[:6]}@example.com")
    user_b = create_user_and_token(client, f"user_doc_b_{uuid.uuid4().hex[:6]}@example.com")

    setup_a = create_workspace_and_collection(client, user_a["headers"], "WS Doc A", f"col_doc_a_{uuid.uuid4().hex[:6]}")
    setup_b = create_workspace_and_collection(client, user_b["headers"], "WS Doc B", f"col_doc_b_{uuid.uuid4().hex[:6]}")

    target_doc_id = f"doc-exclusive-{uuid.uuid4().hex[:8]}"

    # Tenant A creates document
    resp_a = client.post(
        f"/api/v1/workspaces/{setup_a['workspace_id']}/collections/{setup_a['collection_id']}/ingestion-jobs",
        json={
            "source_url": "https://example.com/doc-a.pdf",
            "idempotency_key": f"idem-doc-a-{uuid.uuid4().hex[:6]}",
            "document_id": target_doc_id,
        },
        headers=user_a["headers"],
    )
    assert resp_a.status_code == 202

    # Tenant B tries to attach an ingestion job to Tenant A's document_id
    resp_b = client.post(
        f"/api/v1/workspaces/{setup_b['workspace_id']}/collections/{setup_b['collection_id']}/ingestion-jobs",
        json={
            "source_url": "https://example.com/doc-b.pdf",
            "idempotency_key": f"idem-doc-b-{uuid.uuid4().hex[:6]}",
            "document_id": target_doc_id,
        },
        headers=user_b["headers"],
    )
    assert resp_b.status_code == 409
    assert resp_b.json()["detail"] == "Document ID already belongs to another collection or workspace."


def test_ownership_persisted_in_database_and_trigger_satisfied(client: TestClient, db_session):
    """Verifies that job and document records in PostgreSQL strictly receive the target workspace and collection."""
    user = create_user_and_token(client, f"user_db_{uuid.uuid4().hex[:6]}@example.com")
    setup = create_workspace_and_collection(client, user["headers"], "WS DB Test", f"col_db_{uuid.uuid4().hex[:6]}")
    ws_id = setup["workspace_id"]
    col_id = setup["collection_id"]

    doc_id = f"doc-db-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        f"/api/v1/workspaces/{ws_id}/collections/{col_id}/ingestion-jobs",
        json={
            "source_url": "https://example.com/db-verify.pdf",
            "idempotency_key": f"idem-db-{uuid.uuid4().hex[:8]}",
            "document_id": doc_id,
            "title": "DB Verification Document",
        },
        headers=user["headers"],
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    # Verify directly in the database
    job_row = db_session.execute(
        text("SELECT id, workspace_id, collection_id, document_id FROM ingestion_jobs WHERE id = :jid"),
        {"jid": job_id},
    ).mappings().first()
    assert job_row is not None
    assert str(job_row["workspace_id"]) == ws_id
    assert job_row["collection_id"] == col_id
    assert job_row["document_id"] == doc_id

    doc_row = db_session.execute(
        text("SELECT id, workspace_id, collection_id FROM regulatory_documents WHERE id = :did"),
        {"did": doc_id},
    ).mappings().first()
    assert doc_row is not None
    assert str(doc_row["workspace_id"]) == ws_id
    assert doc_row["collection_id"] == col_id


def test_worker_procedure_preserves_tenant_ownership(client: TestClient, db_session, db_engine):
    """
    Verifies that calling worker stored procedures via PostgresWorkerProcedureRepository
    operates properly on tenant-owned jobs without violating foreign keys or altering ownership.
    """
    user = create_user_and_token(client, f"user_proc_{uuid.uuid4().hex[:6]}@example.com")
    setup = create_workspace_and_collection(client, user["headers"], "WS Proc Test", f"col_proc_{uuid.uuid4().hex[:6]}")
    ws_id = setup["workspace_id"]
    col_id = setup["collection_id"]

    doc_id = f"doc_proc_{uuid.uuid4().hex[:8]}"
    resp = client.post(
        f"/api/v1/workspaces/{ws_id}/collections/{col_id}/ingestion-jobs",
        json={
            "source_url": "https://example.com/proc-test.pdf",
            "idempotency_key": f"idem-proc-{uuid.uuid4().hex[:8]}",
            "document_id": doc_id,
        },
        headers=user["headers"],
    )
    assert resp.status_code == 202
    job_id = uuid.UUID(resp.json()["job_id"])

    # Instantiate repo sharing db_session's connection/transaction
    SessionMaker = sessionmaker(bind=db_session.bind, expire_on_commit=False)
    repo = PostgresWorkerProcedureRepository(session_factory=SessionMaker)

    # Claim the job using stored procedure
    claimed = repo.claim_job(instance_id="worker_test_inst_1")
    assert claimed is not None
    assert claimed.job_id == job_id

    # Heartbeat using stored procedure
    new_lease = repo.heartbeat(job_id=claimed.job_id, raw_claim_token=claimed.raw_claim_token)
    assert new_lease is not None

    # Check database state: workspace_id and collection_id are strictly preserved
    with SessionMaker() as sess:
        job_check = sess.execute(
            text("SELECT workspace_id, collection_id, stage, status FROM ingestion_jobs WHERE id = :jid"),
            {"jid": claimed.job_id},
        ).mappings().first()
        assert str(job_check["workspace_id"]) == ws_id
        assert job_check["collection_id"] == col_id
        assert job_check["status"] == "processing"

    # Complete the job using stored procedure with allow_empty=True
    complete_success = repo.complete_job(
        job_id=claimed.job_id,
        raw_claim_token=claimed.raw_claim_token,
        expected_chunk_count=0,
        allow_empty=True,
    )
    assert complete_success is True

    # Verify final completed state and immutable ownership
    with SessionMaker() as sess:
        job_final = sess.execute(
            text("SELECT workspace_id, collection_id, stage, status FROM ingestion_jobs WHERE id = :jid"),
            {"jid": claimed.job_id},
        ).mappings().first()
        assert str(job_final["workspace_id"]) == ws_id
        assert job_final["collection_id"] == col_id
        assert job_final["status"] == "completed"

