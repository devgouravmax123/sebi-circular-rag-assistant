"""
Comprehensive Tenant Isolation and Multi-Tenant Authorization Test Matrix (Milestone 8 - Task 8.5).

Validates strict tenant isolation across:
1. API workspace & collection RBAC (positive & negative, fail-closed 404, 401, 403)
2. IDOR / identifier substitution (workspace_id, collection_id, document_id, job_id, query_history_id)
3. Ingestion isolation (cross-tenant collection submission, duplicate document_id, idempotency key collision)
4. Retrieval & query scope isolation (distinct content markers, pgvector search scoping, controlled abstention)
5. Direct persistence-layer repository isolation (documents, chunks, ingestion jobs, query history)
"""
import uuid
import pytest
import numpy as np
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from app.main import app
from app.models.auth import UserModel, WorkspaceModel, WorkspaceMembershipModel
from app.models.collection import CollectionModel
from app.models.query_history import QueryHistoryModel
from app.persistence.models import RegulatoryDocumentModel, IngestionJobModel, DocumentVersionModel, ChunkModel
from app.schemas.document import ChunkRecord, DocumentRecord
from app.schemas.retrieval import RetrievalResult, RetrievalResponse
from app.schemas.safety_gate import AnswerDisposition
from app.persistence.postgres import (
    PostgresDocumentRepository,
    PostgresChunkRepository,
    PostgresQueryHistoryRepository,
    PostgresIngestionJobRepository,
)
from app.persistence.interfaces import PersistenceError
from app.retrieval.dense import PostgresDenseIndex
from app.retrieval.service import RetrievalService
from app.api.deps import (
    get_db_session,
    get_current_user,
    get_db_session_optional,
    get_current_user_optional,
)
from app.api.v1.query import get_retrieval_service, get_safety_gate


# ==============================================================================
# Deterministic Multi-Tenant Test Fixtures
# ==============================================================================

@pytest.fixture
def tenant_matrix():
    """Generates two completely independent workspaces/tenants with distinct entities."""
    # Tenant Alpha
    ws_a_id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    user_a_owner_id = uuid.UUID("a1111111-1111-1111-1111-111111111111")
    user_a_member_id = uuid.UUID("a2222222-2222-2222-2222-222222222222")
    col_a_id = "col_alpha_private"
    doc_a_id = "doc_alpha_001"
    job_a_id = uuid.UUID("a3333333-3333-3333-3333-333333333333")
    history_a_id = uuid.UUID("a4444444-4444-4444-4444-444444444444")
    content_marker_a = "TENANT_ALPHA_MARKER_9918: Alpha security compliance mandates encrypted storage."

    # Tenant Beta
    ws_b_id = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    user_b_owner_id = uuid.UUID("b1111111-1111-1111-1111-111111111111")
    col_b_id = "col_beta_private"
    doc_b_id = "doc_beta_001"
    job_b_id = uuid.UUID("b3333333-3333-3333-3333-333333333333")
    history_b_id = uuid.UUID("b4444444-4444-4444-4444-444444444444")
    content_marker_b = "TENANT_BETA_MARKER_8842: Beta trading operations require multi-factor authorization."

    # User Models
    user_a_owner = UserModel(
        id=user_a_owner_id,
        email="owner_a@alpha.corp",
        hashed_password="hash",
        full_name="Alpha Owner",
        is_active=True,
    )
    user_a_member = UserModel(
        id=user_a_member_id,
        email="member_a@alpha.corp",
        hashed_password="hash",
        full_name="Alpha Member",
        is_active=True,
    )
    user_b_owner = UserModel(
        id=user_b_owner_id,
        email="owner_b@beta.corp",
        hashed_password="hash",
        full_name="Beta Owner",
        is_active=True,
    )

    # Workspace Models
    ws_a = WorkspaceModel(
        id=ws_a_id,
        name="Workspace Alpha",
        slug="workspace-alpha",
        is_system=False,
        owner_id=user_a_owner_id,
    )
    ws_b = WorkspaceModel(
        id=ws_b_id,
        name="Workspace Beta",
        slug="workspace-beta",
        is_system=False,
        owner_id=user_b_owner_id,
    )

    # Memberships
    mem_a_owner = WorkspaceMembershipModel(
        workspace_id=ws_a_id,
        user_id=user_a_owner_id,
        role="owner",
    )
    mem_a_member = WorkspaceMembershipModel(
        workspace_id=ws_a_id,
        user_id=user_a_member_id,
        role="member",
    )
    mem_b_owner = WorkspaceMembershipModel(
        workspace_id=ws_b_id,
        user_id=user_b_owner_id,
        role="owner",
    )

    # Collections
    now = datetime.now(timezone.utc)
    col_a = CollectionModel(
        id=col_a_id,
        workspace_id=ws_a_id,
        name="Alpha Private Collection",
        description="Private to Workspace Alpha",
        policy_name="general_static",
        enforce_regulatory_graph=False,
        is_system=False,
        is_public=False,
        created_at=now,
        updated_at=now,
    )
    col_b = CollectionModel(
        id=col_b_id,
        workspace_id=ws_b_id,
        name="Beta Private Collection",
        description="Private to Workspace Beta",
        policy_name="general_static",
        enforce_regulatory_graph=False,
        is_system=False,
        is_public=False,
        created_at=now,
        updated_at=now,
    )

    # Documents
    doc_a = RegulatoryDocumentModel(
        id=doc_a_id,
        workspace_id=ws_a_id,
        collection_id=col_a_id,
        title="Alpha Compliance Framework",
        circular_number="ALPHA/2026/01",
        domain_id="SEBI_CSCRF",
    )
    doc_b = RegulatoryDocumentModel(
        id=doc_b_id,
        workspace_id=ws_b_id,
        collection_id=col_b_id,
        title="Beta Trading Rules",
        circular_number="BETA/2026/01",
        domain_id="SEBI_CSCRF",
    )

    # Ingestion Jobs
    job_a = IngestionJobModel(
        id=job_a_id,
        workspace_id=ws_a_id,
        collection_id=col_a_id,
        document_id=doc_a_id,
        source_url="https://alpha.corp/doc.pdf",
        idempotency_key="idemp_alpha_001",
        status="completed",
        stage="finished",
    )
    job_b = IngestionJobModel(
        id=job_b_id,
        workspace_id=ws_b_id,
        collection_id=col_b_id,
        document_id=doc_b_id,
        source_url="https://beta.corp/doc.pdf",
        idempotency_key="idemp_beta_001",
        status="completed",
        stage="finished",
    )

    return {
        "ws_a_id": ws_a_id,
        "user_a_owner": user_a_owner,
        "user_a_member": user_a_member,
        "ws_a": ws_a,
        "mem_a_owner": mem_a_owner,
        "mem_a_member": mem_a_member,
        "col_a": col_a,
        "doc_a": doc_a,
        "job_a": job_a,
        "history_a_id": history_a_id,
        "content_marker_a": content_marker_a,
        "ws_b_id": ws_b_id,
        "user_b_owner": user_b_owner,
        "ws_b": ws_b,
        "mem_b_owner": mem_b_owner,
        "col_b": col_b,
        "doc_b": doc_b,
        "job_b": job_b,
        "history_b_id": history_b_id,
        "content_marker_b": content_marker_b,
    }


# ==============================================================================
# 1. API Workspace & Collection RBAC Tests
# ==============================================================================

def test_workspace_a_member_can_list_own_collections(tenant_matrix):
    """Positive: User A (owner) lists collections belonging to Workspace A."""
    client = TestClient(app)
    t = tenant_matrix
    mock_db = MagicMock()

    # require_workspace_reader queries:
    # 1. WorkspaceModel by id
    # 2. WorkspaceMembershipModel by (workspace_id, user_id)
    # 3. CollectionModel list by workspace_id
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        t["ws_a"],
        t["mem_a_owner"],
    ]
    mock_db.query.return_value.filter.return_value.all.return_value = [t["col_a"]]
    mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [t["col_a"]]

    app.dependency_overrides[get_current_user] = lambda: t["user_a_owner"]
    app.dependency_overrides[get_db_session] = lambda: mock_db

    try:
        resp = client.get(f"/api/v1/workspaces/{t['ws_a_id']}/collections")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == "col_alpha_private"
    finally:
        app.dependency_overrides.clear()


def test_cross_workspace_collection_list_fails_closed(tenant_matrix):
    """Negative: User A attempts to list collections belonging to Workspace B -> 404 (non-enumerating)."""
    client = TestClient(app)
    t = tenant_matrix
    mock_db = MagicMock()

    # Workspace B exists, but User A has NO membership in Workspace B
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        t["ws_b"],
        None,  # No membership for User A in Workspace B
    ]

    app.dependency_overrides[get_current_user] = lambda: t["user_a_owner"]
    app.dependency_overrides[get_db_session] = lambda: mock_db

    try:
        resp = client.get(f"/api/v1/workspaces/{t['ws_b_id']}/collections")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Workspace not found."
    finally:
        app.dependency_overrides.clear()


def test_cross_workspace_direct_collection_get_fails_closed(tenant_matrix):
    """Negative: User A attempts to directly lookup private Collection B -> 404 (non-enumerating)."""
    client = TestClient(app)
    t = tenant_matrix
    mock_db = MagicMock()

    # 1. CollectionModel found (col_b)
    # 2. WorkspaceMembershipModel for User A in col_b.workspace_id returns None
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        t["col_b"],
        None,  # User A not in Workspace B
    ]

    app.dependency_overrides[get_current_user] = lambda: t["user_a_owner"]
    app.dependency_overrides[get_db_session] = lambda: mock_db

    try:
        resp = client.get(f"/api/v1/collections/{t['col_b'].id}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Collection not found."
    finally:
        app.dependency_overrides.clear()


def test_insufficient_role_for_admin_actions(tenant_matrix):
    """Negative: User with 'member' role attempts admin-only collection creation -> 403 Forbidden."""
    client = TestClient(app)
    t = tenant_matrix
    mock_db = MagicMock()

    # Workspace A found, membership role is 'member' (not owner/admin)
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        t["ws_a"],
        t["mem_a_member"],
    ]

    app.dependency_overrides[get_current_user] = lambda: t["user_a_member"]
    app.dependency_overrides[get_db_session] = lambda: mock_db

    try:
        payload = {
            "id": "new_col",
            "name": "New Collection",
            "policy_name": "general_static",
        }
        resp = client.post(f"/api/v1/workspaces/{t['ws_a_id']}/collections", json=payload)
        assert resp.status_code == 403
        assert "sufficient permissions" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


# ==============================================================================
# 2. IDOR & ID Substitution Checks
# ==============================================================================

def test_idor_workspace_id_substitution(tenant_matrix):
    """IDOR: User A provides Workspace B's ID in path with Collection A's ID -> 404."""
    client = TestClient(app)
    t = tenant_matrix
    mock_db = MagicMock()

    # User A has no membership in Workspace B
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        t["ws_b"],
        None,
    ]

    app.dependency_overrides[get_current_user] = lambda: t["user_a_owner"]
    app.dependency_overrides[get_db_session] = lambda: mock_db

    try:
        resp = client.get(f"/api/v1/workspaces/{t['ws_b_id']}/collections/{t['col_a'].id}/documents")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_idor_job_id_cross_workspace_read(tenant_matrix):
    """IDOR: User A attempts to read Ingestion Job B using Workspace A's path -> 404."""
    client = TestClient(app)
    t = tenant_matrix
    mock_db = MagicMock()

    # 1. Workspace A found
    # 2. Membership A found
    # 3. Collection A found in Workspace A
    # 4. Job query filtered by (job_id == job_b.id AND workspace_id == ws_a_id) returns None
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        t["ws_a"],
        t["mem_a_owner"],
        t["col_a"],
        None,  # Job B does not belong to Workspace A
    ]

    app.dependency_overrides[get_current_user] = lambda: t["user_a_owner"]
    app.dependency_overrides[get_db_session] = lambda: mock_db

    try:
        resp = client.get(f"/api/v1/workspaces/{t['ws_a_id']}/collections/{t['col_a'].id}/ingestion-jobs/{t['job_b'].id}")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


def test_idor_document_download_cross_workspace_read(tenant_matrix):
    """IDOR: User A attempts to download Document B using Workspace A's path -> 404."""
    client = TestClient(app)
    t = tenant_matrix
    mock_db = MagicMock()

    # 1. Workspace A found
    # 2. Membership A found
    # 3. Collection A found
    # 4. Document query filtered by (doc_id == doc_b.id AND workspace_id == ws_a_id) returns None
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        t["ws_a"],
        t["mem_a_owner"],
        t["col_a"],
        None,  # Document B does not belong to Workspace A
    ]

    app.dependency_overrides[get_current_user] = lambda: t["user_a_owner"]
    app.dependency_overrides[get_db_session] = lambda: mock_db

    try:
        resp = client.get(f"/api/v1/workspaces/{t['ws_a_id']}/collections/{t['col_a'].id}/documents/{t['doc_b'].id}/download")
        assert resp.status_code == 404
        assert "document not found in collection" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


def test_idor_query_history_cross_user_access(tenant_matrix):
    """IDOR: User B attempts to read User A's query history entry -> 404."""
    client = TestClient(app)
    t = tenant_matrix
    mock_db = MagicMock()

    # Query filtered by (id == history_a_id AND user_id == user_b.id) returns None
    mock_db.query.return_value.filter.return_value.first.return_value = None

    app.dependency_overrides[get_current_user] = lambda: t["user_b_owner"]
    app.dependency_overrides[get_db_session] = lambda: mock_db

    try:
        resp = client.get(f"/api/v1/query-history/{t['history_a_id']}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Query history record not found."
    finally:
        app.dependency_overrides.clear()


# ==============================================================================
# 3. Ingestion Isolation Tests
# ==============================================================================

def test_cannot_create_job_in_unauthorized_collection(tenant_matrix):
    """Ingestion: User A cannot create an ingestion job targeting Collection B in Workspace A -> 404."""
    client = TestClient(app)
    t = tenant_matrix
    mock_db = MagicMock()

    # Workspace A found, membership found, but Collection B is NOT in Workspace A
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        t["ws_a"],
        t["mem_a_owner"],
        None,  # Collection B not found in Workspace A
    ]

    app.dependency_overrides[get_current_user] = lambda: t["user_a_owner"]
    app.dependency_overrides[get_db_session] = lambda: mock_db

    try:
        payload = {
            "source_url": "https://alpha.corp/new_doc.pdf",
            "idempotency_key": "new_key_123",
        }
        resp = client.post(
            f"/api/v1/workspaces/{t['ws_a_id']}/collections/{t['col_b'].id}/ingestion-jobs",
            json=payload,
        )
        assert resp.status_code == 404
        assert "collection not found in workspace" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


def test_cross_tenant_idempotency_key_collision_rejected(tenant_matrix):
    """Ingestion: Using an idempotency key already claimed by Tenant B returns 409 Conflict."""
    client = TestClient(app)
    t = tenant_matrix
    mock_db = MagicMock()

    # 1. Workspace A found
    # 2. Membership found
    # 3. Collection A found
    # 4. IngestionJobModel found by idempotency_key, but belongs to Workspace B!
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        t["ws_a"],
        t["mem_a_owner"],
        t["col_a"],
        t["job_b"],  # Existing job belongs to Workspace B
    ]

    app.dependency_overrides[get_current_user] = lambda: t["user_a_owner"]
    app.dependency_overrides[get_db_session] = lambda: mock_db

    try:
        payload = {
            "source_url": "https://alpha.corp/attempt.pdf",
            "idempotency_key": "idemp_beta_001",
        }
        resp = client.post(
            f"/api/v1/workspaces/{t['ws_a_id']}/collections/{t['col_a'].id}/ingestion-jobs",
            json=payload,
        )
        assert resp.status_code == 409
        assert "idempotency key conflict" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


def test_cross_tenant_document_id_collision_rejected(tenant_matrix):
    """Ingestion: Attempting to create a job referencing a document_id owned by another tenant returns 409 Conflict."""
    client = TestClient(app)
    t = tenant_matrix
    mock_db = MagicMock()

    # 1. Workspace A found
    # 2. Membership found
    # 3. Collection A found
    # 4. No existing job by idempotency key
    # 5. Document query for document_id returns Document B (owned by Workspace B)
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        t["ws_a"],
        t["mem_a_owner"],
        t["col_a"],
        None,  # No existing job
        t["doc_b"],  # Document already exists in Workspace B!
    ]

    app.dependency_overrides[get_current_user] = lambda: t["user_a_owner"]
    app.dependency_overrides[get_db_session] = lambda: mock_db

    try:
        payload = {
            "source_url": "https://alpha.corp/attempt.pdf",
            "idempotency_key": "unique_key_999",
            "document_id": t["doc_b"].id,
        }
        resp = client.post(
            f"/api/v1/workspaces/{t['ws_a_id']}/collections/{t['col_a'].id}/ingestion-jobs",
            json=payload,
        )
        assert resp.status_code == 409
        assert "already belongs to another collection or workspace" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


# ==============================================================================
# 4. Retrieval, Search & Grounding Isolation Tests
# ==============================================================================

def test_query_cross_tenant_content_marker_isolation(tenant_matrix):
    """
    Search Isolation:
    User A queries Collection A using search terms matching Tenant B's unique content marker.
    Verifies that zero evidence from Collection B enters retrieval or synthesis.
    """
    client = TestClient(app)
    t = tenant_matrix

    # Chunk A has content marker A
    chunk_a = ChunkRecord(
        chunk_id="chunk_alpha_001",
        document_id=t["doc_a"].id,
        collection_id=t["col_a"].id,
        text=t["content_marker_a"],
        page_start=1,
        page_end=1,
        is_ocr=False,
        metadata={"collection_id": t["col_a"].id},
    )

    doc_rec_a = DocumentRecord(
        document_id=t["doc_a"].id,
        title=t["doc_a"].title,
        circular_number=t["doc_a"].circular_number,
        issue_date="2026-01-01",
        source_url="https://alpha.corp/doc.pdf",
        sha256="alpha_hash_123",
        pdf_path="data/raw/alpha.pdf",
        collection_id=t["col_a"].id,
        domain="CSCRF",
        regulator="SEBI",
    )

    # Mock DB session for query authorization:
    # 1. Collection lookup returns col_a
    # 2. Membership lookup returns mem_a_owner
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        t["col_a"],
        t["mem_a_owner"],
    ]

    # Mock embedder
    mock_embedder = MagicMock()
    mock_embedder.model_name = "BAAI/bge-small-en-v1.5"
    mock_embedder.embed_query.return_value = np.ones(384, dtype=np.float32) / np.sqrt(384)

    # Mock repository factory returning only collection A data
    mock_doc_repo = MagicMock()
    mock_doc_repo.list_documents.return_value = [doc_rec_a]
    mock_chunk_repo = MagicMock()
    mock_chunk_repo.list_all_chunks.return_value = [chunk_a]
    # Cosine search returns 0 results for Beta marker query against collection A
    mock_chunk_repo.search_similar_chunks.return_value = []

    mock_factory = MagicMock()
    mock_factory.get_document_repository.return_value = mock_doc_repo
    mock_factory.get_chunk_repository.return_value = mock_chunk_repo
    mock_factory.get_query_history_repository.return_value = MagicMock()

    retrieval_service = RetrievalService(
        dense_embedder=mock_embedder,
        repository_factory=mock_factory,
    )
    retrieval_service.load_and_index_corpus(t["col_a"].id)
    retrieval_service.load_and_index_dense(t["col_a"].id)

    app.dependency_overrides[get_db_session_optional] = lambda: mock_db
    app.dependency_overrides[get_current_user_optional] = lambda: t["user_a_owner"]
    app.dependency_overrides[get_retrieval_service] = lambda: retrieval_service

    try:
        # Query with Beta marker keywords
        resp = client.post(
            "/api/v1/query",
            json={
                "query": "What are the rules for TENANT_BETA_MARKER_8842 multi-factor authorization?",
                "collection_id": t["col_a"].id,
            },
        )
        assert resp.status_code == 200
        data = resp.json()

        # Must abstain or return zero Beta citations
        assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION.value
        assert len(data["verified_excerpts"]) == 0
        # Assert Beta content marker is NOT leaked in narrative or citations
        assert "TENANT_BETA_MARKER_8842" not in str(data["verified_excerpts"])
        assert "multi-factor authorization" not in str(data["verified_excerpts"])
    finally:
        app.dependency_overrides.clear()


# ==============================================================================
# 5. Direct Persistence-Layer Isolation Tests
# ==============================================================================

def test_postgres_document_repo_workspace_scoping(tenant_matrix):
    """Persistence: PostgresDocumentRepository enforces workspace_id filtering on get and list."""
    t = tenant_matrix
    mock_session = MagicMock()
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__enter__.return_value = mock_session

    repo = PostgresDocumentRepository(session_factory=mock_session_factory)

    # get_document: when queried with workspace_a_id for doc_b, session returns None
    mock_session.execute.return_value.scalar_one_or_none.return_value = None
    result = repo.get_document(t["doc_b"].id, workspace_id=t["ws_a_id"])
    assert result is None

    # Verify query had where(workspace_id == ws_a_id)
    call_arg = mock_session.execute.call_args[0][0]
    assert "regulatory_documents.workspace_id = :workspace_id_1" in str(call_arg)


def test_postgres_chunk_repo_search_collection_scoping(tenant_matrix):
    """Persistence: PostgresChunkRepository search_similar_chunks binds collection_id filter."""
    t = tenant_matrix
    mock_session = MagicMock()
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__enter__.return_value = mock_session

    repo = PostgresChunkRepository(session_factory=mock_session_factory)

    mock_session.execute.return_value.fetchall.return_value = []
    vec = np.ones(384, dtype=np.float32) / np.sqrt(384)

    results = repo.search_similar_chunks(collection_id=t["col_a"].id, query_vector=vec, top_k=5)
    assert results == []

    # Check that SQL text statement was executed with collection_id bound
    call_args = mock_session.execute.call_args
    sql_text = str(call_args[0][0])
    params = call_args[1] if len(call_args) > 1 and call_args[1] else call_args[0][1]

    assert "WHERE rd.collection_id = :collection_id" in sql_text
    assert params["collection_id"] == t["col_a"].id


def test_postgres_query_history_repo_user_and_workspace_scoping(tenant_matrix):
    """Persistence: PostgresQueryHistoryRepository enforces user_id and workspace_id on read/delete."""
    t = tenant_matrix
    mock_session = MagicMock()
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__enter__.return_value = mock_session

    repo = PostgresQueryHistoryRepository(session_factory=mock_session_factory)

    # get_entry for user_b on history_a returns None
    mock_session.execute.return_value.scalar_one_or_none.return_value = None
    res = repo.get_entry(history_id=t["history_a_id"], user_id=t["user_b_owner"].id)
    assert res is None

    call_arg = mock_session.execute.call_args[0][0]
    assert "query_history.user_id = :user_id_1" in str(call_arg)

    # delete_entry for user_b on history_a returns False (no row locked)
    mock_session.execute.return_value.scalar_one_or_none.return_value = None
    deleted = repo.delete_entry(history_id=t["history_a_id"], user_id=t["user_b_owner"].id)
    assert deleted is False


def test_postgres_ingestion_job_repo_workspace_scoping(tenant_matrix):
    """Persistence: PostgresIngestionJobRepository retry_job and cancel_job enforce workspace_id."""
    t = tenant_matrix
    mock_session = MagicMock()
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__enter__.return_value = mock_session

    repo = PostgresIngestionJobRepository(session_factory=mock_session_factory)

    # retry_job with wrong workspace_id fails closed with PersistenceError ("not found")
    mock_session.execute.return_value.scalar_one_or_none.return_value = None

    with pytest.raises(PersistenceError, match="not found"):
        repo.retry_job(job_id=t["job_a"].id, workspace_id=t["ws_b_id"])

    with pytest.raises(PersistenceError, match="not found"):
        repo.cancel_job(job_id=t["job_a"].id, workspace_id=t["ws_b_id"])
