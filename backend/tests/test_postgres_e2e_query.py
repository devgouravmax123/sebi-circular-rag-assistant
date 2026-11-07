"""
Unit and integration tests for Task 8.4 — End-to-End Query Using Persisted PostgreSQL Data.
Validates collection resolution, PostgreSQL repository integration, pgvector dense search,
AnswerSafetyGate excerpt verification, controlled abstention, query history persistence,
and multi-tenant isolation.
"""
import uuid
import pytest
import numpy as np
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.document import ChunkRecord, DocumentRecord
from app.schemas.retrieval import RetrievalFilter, RetrievalResult, RetrievalResponse
from app.schemas.query import QueryResponse
from app.schemas.safety_gate import AnswerDisposition
from app.retrieval.dense import PostgresDenseIndex
from app.retrieval.service import RetrievalService
from app.models.auth import UserModel, WorkspaceModel, WorkspaceMembershipModel
from app.models.collection import CollectionModel
from app.api.deps import get_current_user_optional, get_db_session_optional
from app.api.v1.query import get_retrieval_service, get_safety_gate


@pytest.fixture
def mock_embedder():
    embedder = MagicMock()
    embedder.model_name = "BAAI/bge-small-en-v1.5"
    embedder.embed_query.return_value = np.ones(384, dtype=np.float32) / np.sqrt(384)
    return embedder


@pytest.fixture
def sample_pg_chunks_and_docs():
    doc1 = DocumentRecord(
        document_id="doc_pg_test_001",
        title="SEBI Cyber Security Framework Guideline",
        circular_number="CIR/2026/01",
        issue_date="2026-01-15",
        source_url="https://www.sebi.gov.in/circulars/test.pdf",
        sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        pdf_path="data/raw/test.pdf",
        collection_id="col_pg_test",
        domain="CSCRF",
        regulator="SEBI",
    )
    chunk1 = ChunkRecord(
        chunk_id="chunk_pg_001",
        document_id="doc_pg_test_001",
        text="All regulated entities must conduct vulnerability assessments on a quarterly basis.",
        page_start=1,
        page_end=1,
        is_ocr=False,
        metadata={"collection_id": "col_pg_test"},
    )
    chunk2 = ChunkRecord(
        chunk_id="chunk_pg_002",
        document_id="doc_pg_test_001",
        text="Incident reporting must be completed within 6 hours of detection to the designated authority.",
        page_start=2,
        page_end=2,
        is_ocr=False,
        metadata={"collection_id": "col_pg_test"},
    )
    return [doc1], [chunk1, chunk2]


# ==============================================================================
# 1. PostgresDenseIndex Unit Tests
# ==============================================================================

def test_postgres_dense_index_exact_cosine_search(mock_embedder, sample_pg_chunks_and_docs):
    docs, chunks = sample_pg_chunks_and_docs
    mock_chunk_repo = MagicMock()
    mock_chunk_repo.search_similar_chunks.return_value = [
        ("chunk_pg_001", 0.95),
        ("chunk_pg_002", 0.72),
    ]

    index = PostgresDenseIndex(
        chunk_repo=mock_chunk_repo,
        collection_id="col_pg_test",
        embedder=mock_embedder,
        chunks=chunks,
        documents={d.document_id: d for d in docs},
    )

    response = index.search(query="vulnerability assessment frequency", top_k=2)

    assert response.total_results == 2
    assert response.results[0].chunk_id == "chunk_pg_001"
    assert response.results[0].score == 0.95
    assert response.results[0].document_id == "doc_pg_test_001"
    assert response.results[0].title == "SEBI Cyber Security Framework Guideline"
    assert response.results[1].chunk_id == "chunk_pg_002"

    mock_chunk_repo.search_similar_chunks.assert_called_once()
    call_args = mock_chunk_repo.search_similar_chunks.call_args[1]
    assert call_args["collection_id"] == "col_pg_test"
    assert call_args["query_vector"].shape == (384,)


def test_postgres_dense_index_dimension_check(mock_embedder, sample_pg_chunks_and_docs):
    docs, chunks = sample_pg_chunks_and_docs
    mock_chunk_repo = MagicMock()

    index = PostgresDenseIndex(
        chunk_repo=mock_chunk_repo,
        collection_id="col_pg_test",
        embedder=mock_embedder,
        chunks=chunks,
        documents={d.document_id: d for d in docs},
    )

    # Invalid dimension query vector
    invalid_vec = np.ones(128, dtype=np.float32)
    with pytest.raises(ValueError, match="must be 384 dimensions"):
        index.search(query="test", query_vector=invalid_vec)


def test_postgres_dense_index_metadata_filtering(mock_embedder, sample_pg_chunks_and_docs):
    docs, chunks = sample_pg_chunks_and_docs
    mock_chunk_repo = MagicMock()
    mock_chunk_repo.search_similar_chunks.return_value = [
        ("chunk_pg_001", 0.95),
        ("chunk_pg_002", 0.88),
    ]

    index = PostgresDenseIndex(
        chunk_repo=mock_chunk_repo,
        collection_id="col_pg_test",
        embedder=mock_embedder,
        chunks=chunks,
        documents={d.document_id: d for d in docs},
    )

    # Filter requiring a different document_id
    filters = RetrievalFilter(document_id="non_matching_doc")
    response = index.search(query="vulnerability", top_k=5, filters=filters)
    assert response.total_results == 0


# ==============================================================================
# 2. RetrievalService PostgreSQL Loading & Hybrid Fusion
# ==============================================================================

def test_retrieval_service_postgres_loading_and_hybrid(mock_embedder, sample_pg_chunks_and_docs):
    docs, chunks = sample_pg_chunks_and_docs

    mock_doc_repo = MagicMock()
    mock_doc_repo.list_documents.return_value = docs

    mock_chunk_repo = MagicMock()
    mock_chunk_repo.list_all_chunks.return_value = chunks
    mock_chunk_repo.search_similar_chunks.return_value = [("chunk_pg_001", 0.92)]

    mock_factory = MagicMock()
    mock_factory.get_document_repository.return_value = mock_doc_repo
    mock_factory.create_document_repository.return_value = mock_doc_repo
    mock_factory.get_chunk_repository.return_value = mock_chunk_repo
    mock_factory.create_chunk_repository.return_value = mock_chunk_repo

    service = RetrievalService(
        dense_embedder=mock_embedder,
        repository_factory=mock_factory,
    )

    # Load corpus for col_pg_test
    d_count, c_count = service.load_and_index_corpus(collection_id="col_pg_test")
    assert d_count == 1
    assert c_count == 2
    assert service.has_collection_data("col_pg_test") is True

    # Search hybrid
    result = service.search_hybrid(
        query="vulnerability assessments",
        top_k=2,
        collection_id="col_pg_test",
    )
    assert result.total_results > 0
    top_chunk = result.results[0]
    assert top_chunk.chunk_id == "chunk_pg_001"
    assert "quarterly basis" in top_chunk.text


def test_retrieval_service_postgres_reload_if_stale(mock_embedder, sample_pg_chunks_and_docs):
    docs, chunks = sample_pg_chunks_and_docs

    mock_doc_repo = MagicMock()
    mock_doc_repo.list_documents.return_value = docs

    mock_chunk_repo = MagicMock()
    mock_chunk_repo.list_all_chunks.return_value = list(chunks)
    mock_chunk_repo.search_similar_chunks.return_value = [("chunk_pg_001", 0.90)]

    mock_factory = MagicMock()
    mock_factory.get_document_repository.return_value = mock_doc_repo
    mock_factory.create_document_repository.return_value = mock_doc_repo
    mock_factory.get_chunk_repository.return_value = mock_chunk_repo
    mock_factory.create_chunk_repository.return_value = mock_chunk_repo

    service = RetrievalService(
        dense_embedder=mock_embedder,
        repository_factory=mock_factory,
    )
    service.load_and_index_corpus("col_pg_test")
    service.load_and_index_dense(collection_id="col_pg_test")

    # Initial state: no change
    assert service.reload_if_stale("col_pg_test") is False

    # Simulate ingestion of a new chunk in PostgreSQL
    new_chunk = ChunkRecord(
        chunk_id="chunk_pg_003",
        document_id="doc_pg_test_001",
        text="Third party risk management requirements.",
        page_start=3,
        page_end=3,
        is_ocr=False,
    )
    mock_chunk_repo.list_all_chunks.return_value = list(chunks) + [new_chunk]

    # Stale detection triggers reload
    reloaded = service.reload_if_stale("col_pg_test")
    assert reloaded is True
    assert len(service.get_chunks("col_pg_test")) == 3


# ==============================================================================
# 3. Query API Authorization & Multi-Tenant Scoping
# ==============================================================================

def test_query_rejection_unknown_collection():
    client = TestClient(app)
    response = client.post(
        "/api/v1/query",
        json={"query": "test query", "collection_id": "non_existent_collection_xyz"},
    )
    assert response.status_code == 404
    assert "not registered" in response.json()["detail"].lower() or "not found" in response.json()["detail"].lower()


def test_query_private_collection_unauthenticated():
    client = TestClient(app)
    mock_db = MagicMock()
    mock_col = CollectionModel(
        id="private_tenant_col",
        workspace_id=uuid.uuid4(),
        name="Private Tenant Collection",
        policy_name="general_static",
        is_system=False,
        is_public=False,
    )
    mock_db.query.return_value.filter.return_value.first.return_value = mock_col

    app.dependency_overrides[get_db_session_optional] = lambda: mock_db
    app.dependency_overrides[get_current_user_optional] = lambda: None

    try:
        response = client.post(
            "/api/v1/query",
            json={"query": "test query", "collection_id": "private_tenant_col"},
        )
        assert response.status_code == 401
        assert "authentication required" in response.json()["detail"].lower()
    finally:
        app.dependency_overrides.pop(get_db_session_optional, None)
        app.dependency_overrides.pop(get_current_user_optional, None)


def test_query_private_collection_cross_tenant_rejection():
    client = TestClient(app)
    target_ws = uuid.uuid4()
    other_ws = uuid.uuid4()

    mock_user = UserModel(
        id=uuid.uuid4(),
        email="tenant_b@example.com",
        hashed_password="hash",
        full_name="Tenant B",
    )
    mock_col = CollectionModel(
        id="private_tenant_col",
        workspace_id=target_ws,
        name="Tenant A Private Collection",
        policy_name="general_static",
        is_system=False,
        is_public=False,
    )

    mock_db = MagicMock()
    # First query for CollectionModel
    # Second query for WorkspaceMembershipModel returns None (no membership in target_ws)
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        mock_col,
        None,  # No membership in target_ws
    ]

    app.dependency_overrides[get_db_session_optional] = lambda: mock_db
    app.dependency_overrides[get_current_user_optional] = lambda: mock_user

    try:
        response = client.post(
            "/api/v1/query",
            json={"query": "test query", "collection_id": "private_tenant_col"},
        )
        # Returns 404 to avoid tenant collection enumeration
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()
    finally:
        app.dependency_overrides.pop(get_db_session_optional, None)
        app.dependency_overrides.pop(get_current_user_optional, None)


def test_query_unpublished_collection_rejection():
    client = TestClient(app)
    ws_id = uuid.uuid4()
    mock_user = UserModel(
        id=uuid.uuid4(),
        email="tenant_a@example.com",
        hashed_password="hash",
        full_name="Tenant A",
    )
    mock_col = CollectionModel(
        id="unpublished_col",
        workspace_id=ws_id,
        name="Unpublished Collection",
        policy_name="general_static",
        is_system=False,
        is_public=True,
    )
    membership = WorkspaceMembershipModel(
        workspace_id=ws_id,
        user_id=mock_user.id,
        role="member",
    )

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        mock_col,
        membership,
    ]

    # Service with no documents in unpublished_col
    mock_retrieval = MagicMock()
    mock_retrieval.has_collection_data.return_value = False
    mock_retrieval.get_active_snapshot_dir.return_value = None

    app.dependency_overrides[get_db_session_optional] = lambda: mock_db
    app.dependency_overrides[get_current_user_optional] = lambda: mock_user
    app.dependency_overrides[get_retrieval_service] = lambda: mock_retrieval

    try:
        response = client.post(
            "/api/v1/query",
            json={"query": "test query", "collection_id": "unpublished_col"},
        )
        assert response.status_code == 400
        assert "unpublished" in response.json()["detail"].lower()
    finally:
        app.dependency_overrides.pop(get_db_session_optional, None)
        app.dependency_overrides.pop(get_current_user_optional, None)
        app.dependency_overrides.pop(get_retrieval_service, None)


# ==============================================================================
# 4. End-to-End Query with Mocked PostgreSQL Persistence
# ==============================================================================

def test_query_e2e_answerable_persisted_postgres(mock_embedder, sample_pg_chunks_and_docs):
    docs, chunks = sample_pg_chunks_and_docs
    ws_id = uuid.uuid4()
    user_id = uuid.uuid4()

    mock_user = UserModel(
        id=user_id,
        email="operator@example.com",
        hashed_password="hash",
        full_name="Test Operator",
    )
    mock_col = CollectionModel(
        id="col_pg_test",
        workspace_id=ws_id,
        name="SEBI CSCRF Test Collection",
        policy_name="general_static",
        is_system=False,
        is_public=True,
    )
    membership = WorkspaceMembershipModel(
        workspace_id=ws_id,
        user_id=user_id,
        role="admin",
    )

    mock_doc_repo = MagicMock()
    mock_doc_repo.list_documents.return_value = docs

    mock_chunk_repo = MagicMock()
    mock_chunk_repo.list_all_chunks.return_value = chunks
    mock_chunk_repo.search_similar_chunks.return_value = [("chunk_pg_001", 0.95)]

    mock_history_repo = MagicMock()
    mock_history_entry = MagicMock()
    mock_history_entry.id = uuid.uuid4()
    mock_history_repo.create_entry.return_value = mock_history_entry

    mock_factory = MagicMock()
    mock_factory.get_document_repository.return_value = mock_doc_repo
    mock_factory.create_document_repository.return_value = mock_doc_repo
    mock_factory.get_chunk_repository.return_value = mock_chunk_repo
    mock_factory.create_chunk_repository.return_value = mock_chunk_repo
    mock_factory.get_query_history_repository.return_value = mock_history_repo
    mock_factory.create_query_history_repository.return_value = mock_history_repo

    service = RetrievalService(
        dense_embedder=mock_embedder,
        repository_factory=mock_factory,
    )
    service.load_and_index_corpus("col_pg_test")
    service.load_and_index_dense(collection_id="col_pg_test")

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        mock_col,
        membership,
    ]

    client = TestClient(app)
    app.dependency_overrides[get_retrieval_service] = lambda: service
    app.dependency_overrides[get_db_session_optional] = lambda: mock_db
    app.dependency_overrides[get_current_user_optional] = lambda: mock_user

    try:
        response = client.post(
            "/api/v1/query",
            json={
                "query": "vulnerability assessment quarterly basis",
                "collection_id": "col_pg_test",
                "top_k": 3,
            },
        )
        assert response.status_code == 200
        data = response.json()

        assert data["disposition"] in [
            AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS.value,
            AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS.value,
        ]
        assert data["technical_validation_passed"] is True
        assert len(data["verified_excerpts"]) > 0

        verified = data["verified_excerpts"][0]
        assert verified["source_chunk_id"] == "chunk_pg_001"
        assert "quarterly basis" in verified["excerpt_text"]
        assert verified["circular_number"] == "CIR/2026/01"
        assert verified["title"] == "SEBI Cyber Security Framework Guideline"

        # Verify query history repository was called
        mock_history_repo.create_entry.assert_called_once()
        h_args = mock_history_repo.create_entry.call_args[1]
        assert h_args["user_id"] == user_id
        assert h_args["collection_id"] == "col_pg_test"
        assert h_args["disposition"] in [
            AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS.value,
            AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS.value,
        ]
        assert h_args["technical_validation_passed"] is True
    finally:
        app.dependency_overrides.pop(get_retrieval_service, None)
        app.dependency_overrides.pop(get_db_session_optional, None)
        app.dependency_overrides.pop(get_current_user_optional, None)


def test_query_e2e_unanswerable_controlled_abstention(mock_embedder, sample_pg_chunks_and_docs):
    docs, chunks = sample_pg_chunks_and_docs
    ws_id = uuid.uuid4()
    user_id = uuid.uuid4()

    mock_user = UserModel(
        id=user_id,
        email="operator@example.com",
        hashed_password="hash",
        full_name="Test Operator",
    )
    mock_col = CollectionModel(
        id="col_pg_test",
        workspace_id=ws_id,
        name="SEBI CSCRF Test Collection",
        policy_name="general_static",
        is_system=False,
        is_public=True,
    )
    membership = WorkspaceMembershipModel(
        workspace_id=ws_id,
        user_id=user_id,
        role="admin",
    )

    mock_doc_repo = MagicMock()
    mock_doc_repo.list_documents.return_value = docs

    mock_chunk_repo = MagicMock()
    mock_chunk_repo.list_all_chunks.return_value = chunks
    # Return zero similar chunks for unrelated query
    mock_chunk_repo.search_similar_chunks.return_value = []

    mock_history_repo = MagicMock()
    mock_factory = MagicMock()
    mock_factory.get_document_repository.return_value = mock_doc_repo
    mock_factory.create_document_repository.return_value = mock_doc_repo
    mock_factory.get_chunk_repository.return_value = mock_chunk_repo
    mock_factory.create_chunk_repository.return_value = mock_chunk_repo
    mock_factory.get_query_history_repository.return_value = mock_history_repo
    mock_factory.create_query_history_repository.return_value = mock_history_repo

    service = RetrievalService(
        dense_embedder=mock_embedder,
        repository_factory=mock_factory,
    )
    service.load_and_index_corpus("col_pg_test")
    service.load_and_index_dense(collection_id="col_pg_test")

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        mock_col,
        membership,
    ]

    client = TestClient(app)
    app.dependency_overrides[get_retrieval_service] = lambda: service
    app.dependency_overrides[get_db_session_optional] = lambda: mock_db
    app.dependency_overrides[get_current_user_optional] = lambda: mock_user

    try:
        response = client.post(
            "/api/v1/query",
            json={
                "query": "unrelated quantum teleportation astrophysics",
                "collection_id": "col_pg_test",
                "top_k": 3,
            },
        )
        assert response.status_code == 200
        data = response.json()

        assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION.value
        assert data["technical_validation_passed"] is False
        assert len(data["verified_excerpts"]) == 0
        assert data["abstention_reason"] is not None
        assert "abstention" in data["summary_narrative"].lower()

        # Query history is still persisted for auditability
        mock_history_repo.create_entry.assert_called_once()
        h_args = mock_history_repo.create_entry.call_args[1]
        assert h_args["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION.value
    finally:
        app.dependency_overrides.pop(get_retrieval_service, None)
        app.dependency_overrides.pop(get_db_session_optional, None)
        app.dependency_overrides.pop(get_current_user_optional, None)
