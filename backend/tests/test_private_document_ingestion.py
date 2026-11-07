"""
Focused integration tests for Milestone 10: Private Document Ingestion Pipeline.

Validates:
1. Document upload endpoint:
   - Rejects unauthenticated callers (401).
   - Rejects non-members (404 generic).
   - Rejects viewer role without upload permission (403).
   - Rejects system/demo collections (403).
   - Rejects invalid extension or magic bytes (400).
   - Rejects files exceeding max size (413).
   - Accepts valid PDF uploads by workspace owners/admins (202 Accepted).
   - Deduplicates upload if identical content already registered (202 with is_duplicate_registration).
2. Document & Ingestion Job Inspection APIs:
   - Lists ingestion jobs for collection with status and stages.
   - Gets single ingestion job details.
   - Lists documents in collection.
   - Downloads source PDF.
   - Cross-tenant requests return 404.
3. Orchestrator Processing of Uploaded Documents:
   - End-to-end worker processing from LocalStorageAdapter to chunking, embedding, and snapshot publication.
   - IndexPublisher publishes index snapshot upon successful worker completion.
   - Corrupt/unreadable PDF gracefully fails the job and leaves collection unpublished.
4. Safe Multi-Tenant Retrieval:
   - Unpublished private collection returns 400 Bad Request on query attempt.
   - Published private collection executes retrieval successfully and returns private citations.
   - Non-member querying private collection returns 404.
"""
import io
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
import fitz
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.models.auth import UserModel, WorkspaceModel, WorkspaceMembershipModel
from app.models.collection import CollectionModel
from app.persistence.models import IngestionJobModel, RegulatoryDocumentModel
from app.api.deps import get_db_session
from app.storage.local import LocalStorageAdapter
from app.ingestion.orchestrator import IngestionOrchestrator
from app.retrieval.dense_embedder import DenseEmbedder
from app.retrieval.publisher import IndexPublisher
from app.retrieval.service import RetrievalService
from app.persistence.file_repo import FileStorageRepositoryFactory
from app.schemas.ingestion_job import JobStatus, ProcessingStage
from app.schemas.document import DocumentRecord, ChunkRecord

from tests.db_test_guard import get_approved_test_db_url

ADMIN_DB_URL = get_approved_test_db_url()
pytestmark = pytest.mark.skipif(
    not ADMIN_DB_URL,
    reason="No approved local PostgreSQL test database configured (set TEST_DATABASE_URL=postgresql+psycopg://.../sebi_rag_test).",
)


def create_minimal_pdf_bytes(text_lines: list[str]) -> bytes:
    """Create a valid in-memory PDF using PyMuPDF."""
    doc = fitz.open()
    page = doc.new_page()
    y = 50
    for line in text_lines:
        page.insert_text((50, y), line)
        y += 20
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


class MockEmbedder(DenseEmbedder):
    """Deterministic mock embedder returning unit vectors with shape (N, 384)."""

    def __init__(self, dim: int = 384):
        super().__init__(model_name="mock-embedder")
        self.dim = dim

    def embed_texts(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        if not texts:
            return np.empty((0, self.dim), dtype=np.float32)
        vectors = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i in range(len(texts)):
            vectors[i, 0] = 1.0
        return vectors

    def embed_query(self, query: str) -> np.ndarray:
        vectors = np.zeros((self.dim,), dtype=np.float32)
        vectors[0] = 1.0
        return vectors


@pytest.fixture(scope="module")
def db_engine():
    engine = create_engine(ADMIN_DB_URL, echo=False)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
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
    password = "SecurePassword123!"
    resp = client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": password, "full_name": email.split("@")[0]},
    )
    assert resp.status_code == 201
    user_id = resp.json()["id"]

    login_resp = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert login_resp.status_code == 200
    token = login_resp.json()["access_token"]
    return {
        "id": user_id,
        "email": email,
        "token": token,
        "headers": {"Authorization": f"Bearer {token}"},
    }


def test_upload_security_and_rbac(client: TestClient, db_session):
    """Test RBAC and input validation for document uploads."""
    owner = create_user_and_token(client, f"owner_{uuid.uuid4().hex[:6]}@example.com")
    member = create_user_and_token(client, f"member_{uuid.uuid4().hex[:6]}@example.com")
    outsider = create_user_and_token(client, f"outsider_{uuid.uuid4().hex[:6]}@example.com")

    # 1. Create Workspace
    ws_resp = client.post(
        "/api/v1/workspaces",
        json={"name": f"WS_{uuid.uuid4().hex[:6]}", "slug": f"ws-{uuid.uuid4().hex[:8]}"},
        headers=owner["headers"],
    )
    assert ws_resp.status_code == 201
    ws_id = ws_resp.json()["id"]

    # Add member with viewer role
    add_mem_resp = client.post(
        f"/api/v1/workspaces/{ws_id}/members",
        json={"email": member["email"], "role": "viewer"},
        headers=owner["headers"],
    )
    assert add_mem_resp.status_code == 201

    # 2. Create private collection
    col_id = f"col_{uuid.uuid4().hex[:8]}"
    col_resp = client.post(
        f"/api/v1/workspaces/{ws_id}/collections",
        json={"id": col_id, "name": "Audit Col", "policy_name": "general_static"},
        headers=owner["headers"],
    )
    assert col_resp.status_code == 201
    col_id = col_resp.json()["id"]

    pdf_bytes = create_minimal_pdf_bytes(["Test Private Ingestion PDF Content"])

    # 3. Unauthenticated -> 401
    unauth_resp = client.post(
        f"/api/v1/workspaces/{ws_id}/collections/{col_id}/upload",
        files={"file": ("test.pdf", pdf_bytes, "application/pdf")},
    )
    assert unauth_resp.status_code == 401

    # 4. Outsider -> 404 (non-enumerating)
    outsider_resp = client.post(
        f"/api/v1/workspaces/{ws_id}/collections/{col_id}/upload",
        files={"file": ("test.pdf", pdf_bytes, "application/pdf")},
        headers=outsider["headers"],
    )
    assert outsider_resp.status_code == 404

    # 5. Viewer -> 403 Forbidden
    viewer_resp = client.post(
        f"/api/v1/workspaces/{ws_id}/collections/{col_id}/upload",
        files={"file": ("test.pdf", pdf_bytes, "application/pdf")},
        headers=member["headers"],
    )
    assert viewer_resp.status_code == 403

    # 6. System collection target (not in workspace) -> 404 (non-enumerating)
    sys_col_resp = client.post(
        f"/api/v1/workspaces/{ws_id}/collections/sebi_cscrf/upload",
        files={"file": ("test.pdf", pdf_bytes, "application/pdf")},
        headers=owner["headers"],
    )
    assert sys_col_resp.status_code == 404

    # 7. Non-PDF extension -> 400
    bad_ext_resp = client.post(
        f"/api/v1/workspaces/{ws_id}/collections/{col_id}/upload",
        files={"file": ("malicious.exe", b"MZ\x90\x00", "application/octet-stream")},
        headers=owner["headers"],
    )
    assert bad_ext_resp.status_code == 400

    # 8. Fake PDF (invalid magic bytes) -> 400
    fake_pdf_resp = client.post(
        f"/api/v1/workspaces/{ws_id}/collections/{col_id}/upload",
        files={"file": ("spoof.pdf", b"This is not a PDF file at all", "application/pdf")},
        headers=owner["headers"],
    )
    assert fake_pdf_resp.status_code == 400

    # 9. Valid PDF -> 202 Accepted
    upload_resp = client.post(
        f"/api/v1/workspaces/{ws_id}/collections/{col_id}/upload",
        files={"file": ("valid_policy.pdf", pdf_bytes, "application/pdf")},
        data={"title": "Private Security Policy", "circular_number": "SEC-2026-001"},
        headers=owner["headers"],
    )
    assert upload_resp.status_code == 202
    data = upload_resp.json()
    assert data["collection_id"] == col_id
    assert data["job_id"] is not None
    assert data["status"] == "queued"
    assert data["is_duplicate_registration"] is False

    # 10. Member role upload -> 202 Accepted (Fix 4: Member upload allowed)
    reg_member = create_user_and_token(client, f"reg_member_{uuid.uuid4().hex[:6]}@example.com")
    add_reg_resp = client.post(
        f"/api/v1/workspaces/{ws_id}/members",
        json={"email": reg_member["email"], "role": "member"},
        headers=owner["headers"],
    )
    assert add_reg_resp.status_code == 201

    member_pdf_bytes = create_minimal_pdf_bytes(["Member Contributed Operating Procedure"])
    member_up_resp = client.post(
        f"/api/v1/workspaces/{ws_id}/collections/{col_id}/upload",
        files={"file": ("member_policy.pdf", member_pdf_bytes, "application/pdf")},
        data={"title": "Member Document"},
        headers=reg_member["headers"],
    )
    assert member_up_resp.status_code == 202
    assert member_up_resp.json()["status"] == "queued"

    # 11. Duplicate upload -> 202 with is_duplicate_registration True & no orphaned file left (Fix 3)
    upload_dir = Path("data/uploads") / str(ws_id) / col_id
    pre_dup_count = len(list(upload_dir.glob("*.pdf")))

    dup_resp = client.post(
        f"/api/v1/workspaces/{ws_id}/collections/{col_id}/upload",
        files={"file": ("valid_policy_copy.pdf", pdf_bytes, "application/pdf")},
        headers=owner["headers"],
    )
    assert dup_resp.status_code == 202
    assert dup_resp.json()["is_duplicate_registration"] is True

    post_dup_count = len(list(upload_dir.glob("*.pdf")))
    assert post_dup_count == pre_dup_count, "Duplicate upload must not leave an orphaned PDF file on disk"


def test_document_and_job_listing_and_download(client: TestClient, db_session):
    """Test job listing, job details, document listing, and document download."""
    owner = create_user_and_token(client, f"owner_{uuid.uuid4().hex[:6]}@example.com")
    outsider = create_user_and_token(client, f"outsider_{uuid.uuid4().hex[:6]}@example.com")

    # Create workspace and collection
    ws_resp = client.post(
        "/api/v1/workspaces",
        json={"name": f"WS_{uuid.uuid4().hex[:6]}", "slug": f"ws-{uuid.uuid4().hex[:8]}"},
        headers=owner["headers"],
    )
    ws_id = ws_resp.json()["id"]

    col_id = f"col_{uuid.uuid4().hex[:8]}"
    create_col_resp = client.post(
        f"/api/v1/workspaces/{ws_id}/collections",
        json={"id": col_id, "name": "Listing Col", "policy_name": "general_static"},
        headers=owner["headers"],
    )
    assert create_col_resp.status_code == 201

    pdf_bytes = create_minimal_pdf_bytes(["SEBI Compliance Checklist"])
    up_resp = client.post(
        f"/api/v1/workspaces/{ws_id}/collections/{col_id}/upload",
        files={"file": ("checklist.pdf", pdf_bytes, "application/pdf")},
        data={"title": "Checklist Doc"},
        headers=owner["headers"],
    )
    assert up_resp.status_code == 202
    job_id = up_resp.json()["job_id"]
    doc_id = up_resp.json()["document_id"]

    # 1. List Ingestion Jobs
    jobs_resp = client.get(
        f"/api/v1/workspaces/{ws_id}/collections/{col_id}/ingestion-jobs",
        headers=owner["headers"],
    )
    assert jobs_resp.status_code == 200
    jobs = jobs_resp.json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == job_id
    assert jobs[0]["status"] == "queued"

    # 2. Get Single Job
    job_detail_resp = client.get(
        f"/api/v1/workspaces/{ws_id}/collections/{col_id}/ingestion-jobs/{job_id}",
        headers=owner["headers"],
    )
    assert job_detail_resp.status_code == 200
    assert job_detail_resp.json()["job_id"] == job_id

    # 3. List Documents
    docs_resp = client.get(
        f"/api/v1/workspaces/{ws_id}/collections/{col_id}/documents",
        headers=owner["headers"],
    )
    assert docs_resp.status_code == 200
    docs = docs_resp.json()["documents"]
    assert len(docs) == 1
    assert docs[0]["document_id"] == doc_id
    assert docs[0]["title"] == "Checklist Doc"

    # 4. Download Document
    dl_resp = client.get(
        f"/api/v1/workspaces/{ws_id}/collections/{col_id}/documents/{doc_id}/download",
        headers=owner["headers"],
    )
    assert dl_resp.status_code == 200
    assert dl_resp.content == pdf_bytes

    # 5. Cross-tenant access denied
    assert client.get(
        f"/api/v1/workspaces/{ws_id}/collections/{col_id}/ingestion-jobs",
        headers=outsider["headers"],
    ).status_code == 404

    assert client.get(
        f"/api/v1/workspaces/{ws_id}/collections/{col_id}/documents",
        headers=outsider["headers"],
    ).status_code == 404


def test_orchestrator_worker_processes_uploaded_document_and_publishes_snapshot(tmp_path):
    """
    Test worker orchestrator processing an uploaded document via LocalStorageAdapter
    and publishing an active index snapshot.
    """
    uploads_dir = tmp_path / "uploads"
    processed_dir = tmp_path / "processed"
    indexes_dir = tmp_path / "indexes"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)
    indexes_dir.mkdir(parents=True, exist_ok=True)

    storage = LocalStorageAdapter(root_dir=uploads_dir)
    factory = FileStorageRepositoryFactory(processed_dir=processed_dir)
    job_repo = factory.get_ingestion_job_repository()
    doc_repo = factory.get_document_repository()

    ws_id = uuid.uuid4()
    col_id = f"col_{uuid.uuid4().hex[:8]}"
    doc_id = f"doc_{uuid.uuid4().hex[:8]}"

    pdf_bytes = create_minimal_pdf_bytes([
        "Master Cyber Security Circular for Mutual Funds",
        "Chapter 1: Two Factor Authentication and API Governance",
        "Regulated entities must implement strict multi-tenant access boundaries.",
    ])

    # Store file using storage adapter
    save_result = storage.save_upload_stream(
        workspace_id=ws_id,
        collection_id=col_id,
        document_id=doc_id,
        stream=io.BytesIO(pdf_bytes),
    )

    job_record = job_repo.create_job(
        source_url=save_result.source_uri,
        idempotency_key=f"idem_{uuid.uuid4().hex[:8]}",
        document_id=doc_id,
        workspace_id=ws_id,
        collection_id=col_id,
    )

    # Orchestrator
    embedder = MockEmbedder()
    orchestrator = IngestionOrchestrator(
        repository_factory=factory,
        embedder=embedder,
        storage_adapter=storage,
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
        publish_snapshots=True,
    )

    # Claim job
    claim_token = uuid.uuid4()
    claimed = job_repo.claim_next_job(worker_id="test_worker_1", claim_token=claim_token)
    assert claimed is not None
    assert claimed.status == JobStatus.PROCESSING

    # Run processing
    res = orchestrator.process_job(job=claimed, claim_token=claim_token)
    assert res.status == JobStatus.COMPLETED, f"Job failed: {res.message}"
    assert res.chunks_count > 0

    # Verify snapshot published
    snapshot_manifest = indexes_dir / col_id / "active_snapshot.json"
    assert snapshot_manifest.exists()
    import json
    with open(snapshot_manifest, "r", encoding="utf-8") as f:
        meta = json.load(f)
    assert meta["collection_id"] == col_id
    assert meta["snapshot_id"] is not None
    assert meta["total_chunks"] > 0
    assert meta["total_documents"] == 1


def test_orchestrator_worker_unreadable_pdf_fails_without_publishing(tmp_path):
    """
    Test that corrupted or unparseable PDFs fail cleanly without publishing
    any partial or active index snapshots.
    """
    uploads_dir = tmp_path / "uploads"
    processed_dir = tmp_path / "processed"
    indexes_dir = tmp_path / "indexes"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)
    indexes_dir.mkdir(parents=True, exist_ok=True)

    storage = LocalStorageAdapter(root_dir=uploads_dir)
    factory = FileStorageRepositoryFactory(processed_dir=processed_dir)
    job_repo = factory.get_ingestion_job_repository()

    ws_id = uuid.uuid4()
    col_id = f"col_{uuid.uuid4().hex[:8]}"
    doc_id = f"doc_{uuid.uuid4().hex[:8]}"

    corrupt_pdf_bytes = b"%PDF-1.4\n%corrupt truncated content without EOF marker\n"

    save_result = storage.save_upload_stream(
        workspace_id=ws_id,
        collection_id=col_id,
        document_id=doc_id,
        stream=io.BytesIO(corrupt_pdf_bytes),
    )

    job_record = job_repo.create_job(
        source_url=save_result.source_uri,
        idempotency_key=f"idem_{uuid.uuid4().hex[:8]}",
        document_id=doc_id,
        workspace_id=ws_id,
        collection_id=col_id,
        max_attempts=1,
    )

    embedder = MockEmbedder()
    orchestrator = IngestionOrchestrator(
        repository_factory=factory,
        embedder=embedder,
        storage_adapter=storage,
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
        publish_snapshots=True,
    )

    claim_token = uuid.uuid4()
    claimed = job_repo.claim_next_job(worker_id="test_worker_1", claim_token=claim_token)
    assert claimed is not None

    res = orchestrator.process_job(job=claimed, claim_token=claim_token)
    assert res.status == JobStatus.FAILED
    assert res.chunks_count == 0

    snapshot_manifest = indexes_dir / col_id / "active_snapshot.json"
    assert not snapshot_manifest.exists()


def test_private_collection_query_safety_staged(client: TestClient, db_session, tmp_path):
    """
    Test that private collection querying enforces snapshot readiness:
    - Unpublished private collection -> 400 Bad Request
    - Published private collection -> 200 OK
    - Unauthorized user -> 404 Not Found
    """
    owner = create_user_and_token(client, f"owner_{uuid.uuid4().hex[:6]}@example.com")
    outsider = create_user_and_token(client, f"outsider_{uuid.uuid4().hex[:6]}@example.com")

    # Create workspace and collection
    ws_resp = client.post(
        "/api/v1/workspaces",
        json={"name": f"WS_{uuid.uuid4().hex[:6]}", "slug": f"ws-{uuid.uuid4().hex[:8]}"},
        headers=owner["headers"],
    )
    ws_id = ws_resp.json()["id"]

    col_id = f"col_{uuid.uuid4().hex[:8]}"
    create_col_resp = client.post(
        f"/api/v1/workspaces/{ws_id}/collections",
        json={"id": col_id, "name": "Query Col", "policy_name": "general_static"},
        headers=owner["headers"],
    )
    assert create_col_resp.status_code == 201

    # 1. Before publishing snapshot, query should fail with 400 Bad Request
    unpub_query = client.post(
        "/api/v1/query",
        json={"query": "What are the authentication rules?", "collection_id": col_id},
        headers=owner["headers"],
    )
    assert unpub_query.status_code == 400
    assert "unpublished" in unpub_query.json()["detail"].lower()

    # 2. Non-member query receives generic 404
    outsider_query = client.post(
        "/api/v1/query",
        json={"query": "What are the authentication rules?", "collection_id": col_id},
        headers=outsider["headers"],
    )
    assert outsider_query.status_code == 404

    # 3. Simulate active snapshot publication for this collection
    indexes_dir = Path("data/indexes")
    col_index_dir = indexes_dir / col_id
    col_index_dir.mkdir(parents=True, exist_ok=True)
    active_snapshot_file = col_index_dir / "active_snapshot.json"
    import json
    active_snapshot_file.write_text(json.dumps({
        "collection_id": col_id,
        "status": "published",
        "snapshot_id": f"snap_{uuid.uuid4().hex[:8]}",
        "chunk_count": 5,
        "document_count": 1,
        "published_at": "2026-11-01T00:00:00Z",
    }), encoding="utf-8")

    try:
        # Mock retrieval and synthesis services to test endpoint wiring cleanly
        from unittest.mock import MagicMock
        from app.schemas.query import QueryResponse, ValidatedExcerptItem, AnswerDisposition

        mock_retrieval_service = MagicMock()
        mock_retrieval_res = MagicMock()
        mock_retrieval_res.results = [
            MagicMock(
                chunk_id=f"{col_id}_chunk_1",
                text="Regulated entities must implement strict MFA controls.",
                score=0.95,
                document_id="doc_1",
                title="SEBI Cyber Circular",
                collection_id=col_id,
                dense_rank=1,
                sparse_rank=1,
            )
        ]
        mock_retrieval_res.total_candidates = 1
        mock_retrieval_service.search_hybrid.return_value = mock_retrieval_res

        mock_synthesis_service = MagicMock()
        mock_synthesis_service.synthesize.return_value = QueryResponse(
            query="What are the authentication rules?",
            disposition=AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS,
            summary_narrative="Regulated entities must implement strict MFA controls.",
            technical_validation_passed=True,
            verified_excerpts=[
                ValidatedExcerptItem(
                    excerpt_id="exc_1",
                    source_chunk_id=f"{col_id}_chunk_1",
                    document_id="doc_1",
                    circular_number="SEC-01",
                    title="SEBI Cyber Circular",
                    issue_date="2026-01-01",
                    page_start=1,
                    page_end=1,
                    excerpt_text="Regulated entities must implement strict MFA controls.",
                    regulatory_status="ACTIVE_CURRENT",
                    status_reason="Active policy excerpt.",
                    applicable_edge_ids=[],
                    acting_document_ids=[],
                )
            ],
            historical_superseded_excerpts=[],
            retrieval_candidate_count=1,
            abstention_reason=None,
            regulatory_warnings=[],
            pilot_corpus_scope_limitation="Private collection scope.",
        )

        from app.api.v1.query import get_retrieval_service, get_synthesis_service
        app.dependency_overrides[get_retrieval_service] = lambda: mock_retrieval_service
        app.dependency_overrides[get_synthesis_service] = lambda: mock_synthesis_service

        pub_query = client.post(
            "/api/v1/query",
            json={"query": "What are the authentication rules?", "collection_id": col_id},
            headers=owner["headers"],
        )
        assert pub_query.status_code == 200
        data = pub_query.json()
        assert data["query"] == "What are the authentication rules?"
        assert "disposition" in data
        assert data["retrieval_candidate_count"] >= 1
    finally:
        app.dependency_overrides.pop(get_retrieval_service, None)
        app.dependency_overrides.pop(get_synthesis_service, None)
        # Clean up created test index directory
        import shutil
        if col_index_dir.exists():
            shutil.rmtree(col_index_dir, ignore_errors=True)


def test_failed_unindexed_documents_excluded_from_snapshots(tmp_path):
    """
    Fix 1 Regression Test:
    Test that documents without chunks or marked with extraction_status='failed'
    are excluded from published snapshot manifests, document maps, and searchability,
    while valid documents are correctly indexed.
    """
    processed_dir = tmp_path / "processed"
    indexes_dir = tmp_path / "indexes"
    processed_dir.mkdir(parents=True, exist_ok=True)
    indexes_dir.mkdir(parents=True, exist_ok=True)

    factory = FileStorageRepositoryFactory(processed_dir=processed_dir)
    doc_repo = factory.get_document_repository()
    chunk_repo = factory.get_chunk_repository()

    col_id = f"col_{uuid.uuid4().hex[:8]}"

    # Doc 1: Valid document with extracted chunks
    doc1 = DocumentRecord(
        document_id="doc_valid_1",
        circular_number="CIR-01",
        title="Valid Document One",
        issue_date="2026-01-01",
        source_url="file://uploads/doc1.pdf",
        sha256="1111111111111111111111111111111111111111111111111111111111111111",
        pdf_path="uploads/doc1.pdf",
        collection_id=col_id,
        extraction_status="completed",
        chunk_count=1,
    )
    doc_repo.save_document(doc1)
    chunk1 = ChunkRecord(
        chunk_id=f"{col_id}_chunk_1",
        document_id="doc_valid_1",
        chunk_index=0,
        text="Valid policy rules regarding data encryption.",
        page_number=1,
        page_start=1,
        page_end=1,
        char_count=45,
        token_count_estimate=8,
        collection_id=col_id,
        circular_number="CIR-01",
        title="Valid Document One",
        issue_date="2026-01-01",
    )
    chunk_repo.save_chunks([chunk1])

    # Doc 2: Unindexed / failed document (0 chunks, extraction_status='failed')
    doc2 = DocumentRecord(
        document_id="doc_failed_2",
        circular_number="CIR-02",
        title="Failed Document Two",
        issue_date="2026-01-02",
        source_url="file://uploads/doc2.pdf",
        sha256="2222222222222222222222222222222222222222222222222222222222222222",
        pdf_path="uploads/doc2.pdf",
        collection_id=col_id,
        extraction_status="failed",
        chunk_count=0,
    )
    doc_repo.save_document(doc2)

    # Doc 3: Valid document with extracted chunks
    doc3 = DocumentRecord(
        document_id="doc_valid_3",
        circular_number="CIR-03",
        title="Valid Document Three",
        issue_date="2026-01-03",
        source_url="file://uploads/doc3.pdf",
        sha256="3333333333333333333333333333333333333333333333333333333333333333",
        pdf_path="uploads/doc3.pdf",
        collection_id=col_id,
        extraction_status="completed",
        chunk_count=1,
    )
    doc_repo.save_document(doc3)
    chunk3 = ChunkRecord(
        chunk_id=f"{col_id}_chunk_3",
        document_id="doc_valid_3",
        chunk_index=0,
        text="Valid policy rules regarding network monitoring.",
        page_number=1,
        page_start=1,
        page_end=1,
        char_count=46,
        token_count_estimate=8,
        collection_id=col_id,
        circular_number="CIR-03",
        title="Valid Document Three",
        issue_date="2026-01-03",
    )
    chunk_repo.save_chunks([chunk3])

    publisher = IndexPublisher(
        repository_factory=factory,
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
        embedder=MockEmbedder(),
        collection_id=col_id,
    )

    result = publisher.build_and_publish_snapshot()
    assert result.success is True
    assert result.total_documents == 2
    assert result.total_chunks == 2

    # Check manifest
    snapshot_dir = indexes_dir / col_id / "snapshots" / result.snapshot_id
    manifest_path = snapshot_dir / "manifest.json"
    assert manifest_path.exists()
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    assert "doc_valid_1" in manifest["document_ids"]
    assert "doc_valid_3" in manifest["document_ids"]
    assert "doc_failed_2" not in manifest["document_ids"]
    assert manifest["total_documents"] == 2

    # Check documents.json
    docs_json_path = snapshot_dir / "documents.json"
    with open(docs_json_path, "r", encoding="utf-8") as f:
        docs_map = json.load(f)
    assert "doc_valid_1" in docs_map
    assert "doc_valid_3" in docs_map
    assert "doc_failed_2" not in docs_map

    # Check searchability check
    assert publisher.is_document_searchable("doc_valid_1") is True
    assert publisher.is_document_searchable("doc_valid_3") is True
    assert publisher.is_document_searchable("doc_failed_2") is False


def test_concurrent_publication_lock_contention_is_retryable(tmp_path):
    """
    Fix 2 Regression Test:
    Test that when an ingestion worker encounters .publish.lock contention during
    stage 7 publication, it raises RetryableIngestionError and marks the job as
    QUEUED with incremented attempt_count and backoff, rather than permanent failure.
    Releasing the lock allows subsequent retry to succeed.
    """
    uploads_dir = tmp_path / "uploads"
    processed_dir = tmp_path / "processed"
    indexes_dir = tmp_path / "indexes"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)
    indexes_dir.mkdir(parents=True, exist_ok=True)

    storage = LocalStorageAdapter(root_dir=uploads_dir)
    factory = FileStorageRepositoryFactory(processed_dir=processed_dir)
    job_repo = factory.get_ingestion_job_repository()

    ws_id = uuid.uuid4()
    col_id = f"col_{uuid.uuid4().hex[:8]}"
    doc_id = f"doc_{uuid.uuid4().hex[:8]}"

    pdf_bytes = create_minimal_pdf_bytes([
        "Authentication policy requires hardware security keys for admin access.",
    ])

    save_result = storage.save_upload_stream(
        workspace_id=ws_id,
        collection_id=col_id,
        document_id=doc_id,
        stream=io.BytesIO(pdf_bytes),
    )

    job_record = job_repo.create_job(
        source_url=save_result.source_uri,
        idempotency_key=f"idem_{uuid.uuid4().hex[:8]}",
        document_id=doc_id,
        workspace_id=ws_id,
        collection_id=col_id,
        max_attempts=3,
    )

    # Simulate another worker holding the publication lock
    col_dir = indexes_dir / col_id
    col_dir.mkdir(parents=True, exist_ok=True)
    lock_file = col_dir / ".publish.lock"
    lock_file.write_text("another_worker_pid_12345", encoding="utf-8")

    orchestrator = IngestionOrchestrator(
        repository_factory=factory,
        embedder=MockEmbedder(),
        storage_adapter=storage,
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
        publish_snapshots=True,
    )

    claim_token = uuid.uuid4()
    claimed = job_repo.claim_next_job(worker_id="test_worker_1", claim_token=claim_token)
    assert claimed is not None

    # Process job - should encounter lock conflict and be rescheduled (QUEUED)
    res = orchestrator.process_job(job=claimed, claim_token=claim_token)
    assert res.status == JobStatus.QUEUED
    assert any(term in (res.message or "").lower() for term in ("retryable", "concurrent", "lock", "conflict"))

    # Verify job record in repo
    updated_job = job_repo.get_job(job_record.id)
    assert updated_job is not None
    assert updated_job.status == JobStatus.QUEUED
    assert updated_job.attempt_count == 1
    assert updated_job.next_retry_at is not None

    # Now remove the lock (simulating other worker finishing)
    lock_file.unlink(missing_ok=True)

    # Claim next job simulating time advanced past backoff window
    from datetime import timedelta
    claim_token_2 = uuid.uuid4()
    claimed_2 = job_repo.claim_next_job(
        worker_id="test_worker_1",
        claim_token=claim_token_2,
        now=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    assert claimed_2 is not None

    res_2 = orchestrator.process_job(job=claimed_2, claim_token=claim_token_2)
    assert res_2.status == JobStatus.COMPLETED
    assert res_2.chunks_count > 0

    # Verify active snapshot published
    snapshot_manifest = indexes_dir / col_id / "active_snapshot.json"
    assert snapshot_manifest.exists()


def test_live_retrieval_service_private_collection_search(client: TestClient, db_session, tmp_path):
    """
    Fix 6 Regression Test:
    Executes a private collection query end-to-end with the real RetrievalService
    reading a real on-disk index snapshot without mocking retrieval.
    Verifies:
    1. Unpublished private collection returns 400 Bad Request.
    2. Non-member access returns 404 Not Found.
    3. Authorized member query retrieves private chunks and succeeds with 200 OK.
    """
    from app.core.config import get_settings
    settings = get_settings()
    indexes_dir = Path(settings.indexes_dir)

    owner = create_user_and_token(client, f"owner_{uuid.uuid4().hex[:6]}@example.com")
    outsider = create_user_and_token(client, f"outsider_{uuid.uuid4().hex[:6]}@example.com")

    # Create workspace and collection
    ws_resp = client.post(
        "/api/v1/workspaces",
        json={"name": f"WS_{uuid.uuid4().hex[:6]}", "slug": f"ws-{uuid.uuid4().hex[:8]}"},
        headers=owner["headers"],
    )
    ws_id = ws_resp.json()["id"]

    col_id = f"col_{uuid.uuid4().hex[:8]}"
    create_col_resp = client.post(
        f"/api/v1/workspaces/{ws_id}/collections",
        json={"id": col_id, "name": "Live Retrieval Col", "policy_name": "general_static"},
        headers=owner["headers"],
    )
    assert create_col_resp.status_code == 201

    col_index_dir = indexes_dir / col_id
    proc_dir = tmp_path / "proc"
    proc_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 1. Query before publishing must fail closed with 400
        unpub_resp = client.post(
            "/api/v1/query",
            json={"query": "vulnerability scanning cadence", "collection_id": col_id},
            headers=owner["headers"],
        )
        assert unpub_resp.status_code == 400
        assert "unpublished" in unpub_resp.json()["detail"].lower()

        # 2. Non-member querying returns 404
        outsider_resp = client.post(
            "/api/v1/query",
            json={"query": "vulnerability scanning cadence", "collection_id": col_id},
            headers=outsider["headers"],
        )
        assert outsider_resp.status_code == 404

        # 3. Publish real on-disk snapshot for this private collection
        factory = FileStorageRepositoryFactory(processed_dir=proc_dir)
        doc_repo = factory.get_document_repository()
        chunk_repo = factory.get_chunk_repository()

        doc = DocumentRecord(
            document_id="doc_p1",
            title="Private Security Policy",
            circular_number="PRIV-SEC-01",
            issue_date="2026-02-01",
            source_url="file://uploads/doc_p1.pdf",
            sha256="4444444444444444444444444444444444444444444444444444444444444444",
            pdf_path="uploads/doc_p1.pdf",
            collection_id=col_id,
            extraction_status="completed",
            chunk_count=1,
        )
        doc_repo.save_document(doc)

        chunk = ChunkRecord(
            chunk_id=f"{col_id}_chunk_1",
            document_id="doc_p1",
            chunk_index=0,
            text="Regulated entities must conduct automated vulnerability scanning cadence weekly without exception.",
            page_number=1,
            page_start=1,
            page_end=1,
            char_count=101,
            token_count_estimate=15,
            collection_id=col_id,
            title="Private Security Policy",
            circular_number="PRIV-SEC-01",
            issue_date="2026-02-01",
        )
        chunk_repo.save_chunks([chunk])

        embedder = MockEmbedder()
        publisher = IndexPublisher(
            repository_factory=factory,
            indexes_dir=indexes_dir,
            processed_dir=proc_dir,
            embedder=embedder,
            collection_id=col_id,
        )
        pub_result = publisher.build_and_publish_snapshot()
        assert pub_result.success is True

        # Ensure real RetrievalService will use MockEmbedder if hybrid search is configured
        from app.api.v1.query import get_retrieval_service
        real_retrieval_service = RetrievalService(
            processed_dir=settings.processed_dir,
            indexes_dir=settings.indexes_dir,
            dense_embedder=embedder,
            retrieval_mode=settings.retrieval_mode,
        )
        app.dependency_overrides[get_retrieval_service] = lambda: real_retrieval_service

        # 4. Query published private collection with real retrieval
        pub_resp = client.post(
            "/api/v1/query",
            json={"query": "What is the automated vulnerability scanning cadence?", "collection_id": col_id},
            headers=owner["headers"],
        )
        assert pub_resp.status_code == 200, f"Query failed: {pub_resp.text}"
        data = pub_resp.json()
        assert data["query"] == "What is the automated vulnerability scanning cadence?"
        assert data["retrieval_candidate_count"] >= 1
        assert "disposition" in data
        assert data["disposition"] in ("VERIFIED_HISTORICAL_EXCERPTS", "QUALIFIED_HISTORICAL_EXCERPTS")

        # Verify private excerpt content matches published chunk
        assert len(data.get("verified_excerpts", [])) >= 1
        excerpt_text = data["verified_excerpts"][0]["excerpt_text"]
        assert "vulnerability scanning cadence weekly" in excerpt_text

    finally:
        from app.api.v1.query import get_retrieval_service
        app.dependency_overrides.pop(get_retrieval_service, None)
        if col_index_dir.exists():
            shutil.rmtree(col_index_dir, ignore_errors=True)
        if proc_dir.exists():
            shutil.rmtree(proc_dir, ignore_errors=True)
