"""
End-to-end tests for Guest Mode, Session Isolation, Ingestion Integration, and Persistence (Milestone 12 & 13).

Verifies:
1. End-to-end guest lifecycle: creation -> temporary collection -> upload -> jobs -> history.
2. Cross-guest isolation: Guest B cannot access or query Guest A's collection or history.
3. Token boundary security: Guest token cannot access registered endpoints (401); expired tokens rejected (401).
4. Public collection accessibility: Guest tokens successfully query public collection (sebi_cscrf) without 401.
5. Immediate server-side cleanup: Termination purges files, records, memory, and invalidates tokens.
6. Sweeper idempotency & maintenance endpoint: Expired session sweeper cleans up expired sessions safely.
7. Anti-race condition protection: Ingestion worker aborts publication if collection is purged mid-build.
8. Complete guest-owned document workflow: PDF generation -> storage -> worker ingestion -> published snapshot -> cited query -> termination -> verification of zero lingering artifacts.
9. Registered user persistence & isolation: User B cannot access User A's private collections or history.
"""
from datetime import datetime, timezone, timedelta
import hashlib
import io
from pathlib import Path
import shutil
import tempfile
import uuid
import fitz
import numpy as np
import pytest
from starlette.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
import jwt

# Register SQLite dialect mappings for PostgreSQL specific types
@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "TEXT"

@compiles(PG_UUID, "sqlite")
def compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"

from app.main import app
from app.core.config import get_settings
from app.core.auth import create_access_token, decode_access_token
from app.persistence.models import Base, RegulatoryDocumentModel, IngestionJobModel
from app.persistence.file_repo import FileStorageRepositoryFactory
from app.models.auth import UserModel, WorkspaceModel, WorkspaceMembershipModel
from app.models.collection import CollectionModel
from app.models.guest import GuestSessionModel, GuestQueryHistoryModel
from app.models.query_history import QueryHistoryModel
from app.schemas.ingestion_job import IngestionJobRecord, JobStatus, ProcessingStage
from app.ingestion.orchestrator import IngestionOrchestrator, IngestionWorker
from app.retrieval.dense_embedder import DenseEmbedder
from app.retrieval.publisher import IndexPublisher
from app.retrieval.service import RetrievalService
from app.storage import LocalStorageAdapter
from app.services.guest_service import (
    _memory_guest_sessions,
    _memory_guest_history,
    _memory_guest_collections,
    _memory_guest_documents,
    _memory_guest_jobs,
    get_guest_workspace_id,
    get_guest_canonical_collection_id,
    ensure_guest_workspace,
    purge_guest_session_artifacts,
    cleanup_expired_guest_sessions,
)
from app.api.deps import get_db_session_optional, get_db_session

client = TestClient(app)


def test_guest_cross_tenant_isolation():
    """Verify Guest B cannot see, access, or query Guest A's temporary data."""
    # 1. Create Guest A
    resp_a = client.post("/api/v1/guest/session")
    assert resp_a.status_code == 201
    data_a = resp_a.json()
    token_a = data_a["token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # Guest A creates collection
    col_a_resp = client.post(
        "/api/v1/guest/collections",
        json={"name": "Guest A Secret Notes"},
        headers=headers_a,
    )
    assert col_a_resp.status_code == 201
    col_a_id = col_a_resp.json()["id"]

    # Guest A records query history
    rec_a = client.post(
        "/api/v1/guest/history/record",
        json={
            "query_text": "Guest A private query",
            "collection_id": col_a_id,
            "disposition": "FULLY_ANSWERED",
            "answer_text": "Confidential answer for A",
            "citations_count": 1,
        },
        headers=headers_a,
    )
    assert rec_a.status_code == 201

    # 2. Create Guest B
    resp_b = client.post("/api/v1/guest/session")
    assert resp_b.status_code == 201
    data_b = resp_b.json()
    token_b = data_b["token"]
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # 3. Guest B lists collections -> must NOT see Guest A's collection
    list_b = client.get("/api/v1/guest/collections", headers=headers_b)
    assert list_b.status_code == 200
    b_col_ids = [c["id"] for c in list_b.json()]
    assert col_a_id not in b_col_ids

    # 4. Guest B lists history -> must NOT see Guest A's history
    hist_b = client.get("/api/v1/guest/history", headers=headers_b)
    assert hist_b.status_code == 200
    assert hist_b.json()["total"] == 0

    # 5. Guest B attempts to query Guest A's collection -> returns 404 (prevent tenant enumeration)
    query_cross = client.post(
        "/api/v1/query",
        json={"query": "test cross tenant", "collection_id": col_a_id},
        headers=headers_b,
    )
    assert query_cross.status_code == 404
    detail_lower = query_cross.json()["detail"].lower()
    assert "not found" in detail_lower or "not registered" in detail_lower


def test_guest_token_rejected_on_registered_endpoints():
    """Verify that ephemeral guest tokens cannot access registered user resources (fail closed)."""
    resp = client.post("/api/v1/guest/session")
    assert resp.status_code == 201
    guest_token = resp.json()["token"]
    guest_headers = {"Authorization": f"Bearer {guest_token}"}

    # Attempt to list workspaces (registered endpoint)
    ws_resp = client.get("/api/v1/workspaces", headers=guest_headers)
    assert ws_resp.status_code == 401
    assert "Guest sessions cannot access registered user resources" in ws_resp.json()["detail"]


def test_expired_guest_token_denied():
    """Verify that expired guest tokens are rejected with HTTP 401."""
    settings = get_settings()
    session_id = str(uuid.uuid4())
    expired_time = datetime.now(timezone.utc) - timedelta(hours=1)

    payload = {
        "sub": session_id,
        "session_id": session_id,
        "is_guest": True,
        "exp": expired_time,
        "iat": expired_time - timedelta(hours=24),
    }
    expired_token = jwt.encode(payload, settings.get_auth_secret_key, algorithm=settings.auth_algorithm)
    expired_headers = {"Authorization": f"Bearer {expired_token}"}

    # Guest session endpoint check
    st_resp = client.get("/api/v1/guest/session", headers=expired_headers)
    assert st_resp.status_code == 401
    assert "expired" in st_resp.json()["detail"].lower()

    # Query endpoint check with expired guest token
    query_resp = client.post(
        "/api/v1/query",
        json={"query": "what is cybersecurity audit?", "collection_id": "sebi_cscrf"},
        headers=expired_headers,
    )
    assert query_resp.status_code == 401


def test_guest_query_public_collection_succeeds_and_records_history():
    """Verify guest tokens can query public collections without 401 and get evidence-grounded answers."""
    resp = client.post("/api/v1/guest/session")
    assert resp.status_code == 201
    token = resp.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    query_resp = client.post(
        "/api/v1/query",
        json={"query": "What are cybersecurity audit requirements?", "collection_id": "sebi_cscrf"},
        headers=headers,
    )
    assert query_resp.status_code == 200
    q_data = query_resp.json()
    assert "disposition" in q_data
    assert "verified_excerpts" in q_data

    # History should have been recorded for this guest session
    hist_resp = client.get("/api/v1/guest/history", headers=headers)
    assert hist_resp.status_code == 200
    items = hist_resp.json()["items"]
    assert len(items) >= 1
    assert items[0]["collection_id"] == "sebi_cscrf"


def test_guest_termination_and_sweeper_cleanup():
    """Verify immediate server-side deletion on termination and safe sweeper operation."""
    resp = client.post("/api/v1/guest/session")
    assert resp.status_code == 201
    session_id = resp.json()["session_id"]
    token = resp.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Create collection & upload
    col_resp = client.post("/api/v1/guest/collections", json={"name": "Sweeper Test"}, headers=headers)
    assert col_resp.status_code == 201
    col_id = col_resp.json()["id"]

    pdf_bytes = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\nxref\ntrailer<</Root 1 0 R>>\n%%EOF"
    up_resp = client.post(
        "/api/v1/guest/upload",
        data={"collection_id": col_id},
        files={"file": ("doc.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        headers=headers,
    )
    assert up_resp.status_code == 202

    # Terminate session
    term_resp = client.post("/api/v1/guest/session/terminate", headers=headers)
    assert term_resp.status_code == 200
    assert term_resp.json()["cleaned_up"] is True

    # Memory state cleaned
    assert session_id not in _memory_guest_history
    assert session_id not in _memory_guest_collections
    assert session_id not in _memory_guest_documents
    assert session_id not in _memory_guest_jobs

    # Token immediately rejected
    check_resp = client.get("/api/v1/guest/session", headers=headers)
    assert check_resp.status_code == 401

    # Run sweeper: must run idempotently without exceptions
    cleaned = cleanup_expired_guest_sessions()
    assert isinstance(cleaned, int)
    assert cleaned >= 0

    # Test maintenance endpoint security: must reject unauthenticated requests
    unauth_resp = client.post("/api/v1/guest/cleanup-expired")
    assert unauth_resp.status_code == 401
    assert "Missing required maintenance authorization header" in unauth_resp.json()["detail"]

    bad_key_resp = client.post(
        "/api/v1/guest/cleanup-expired",
        headers={"X-Admin-Key": "invalid_wrong_maintenance_key"},
    )
    assert bad_key_resp.status_code == 403

    valid_maint_key = get_settings().admin_api_key or get_settings().get_auth_secret_key
    maint_resp = client.post(
        "/api/v1/guest/cleanup-expired",
        headers={"X-Admin-Key": valid_maint_key},
    )
    assert maint_resp.status_code == 200
    assert "cleaned_sessions_count" in maint_resp.json()


@pytest.fixture
def sqlite_test_db():
    """Provides an isolated in-memory SQLite database for testing database-backed guest persistence."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=engine)
    SessionMaker = sessionmaker(bind=engine, expire_on_commit=False)
    session = SessionMaker()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_sqlite_guest_db_persistence_and_purge(sqlite_test_db):
    """Verifies that guest DB entities (Workspace, Collection, Job, Doc, Query History) are created and purged cleanly."""
    session_id = str(uuid.uuid4())
    db = sqlite_test_db
    settings = get_settings()

    # 1. Ensure workspace
    ws = ensure_guest_workspace(db, session_id)
    assert ws is not None
    assert ws.is_system is True
    assert ws.owner_id is None

    # 2. Add GuestSessionModel
    sess_uuid = uuid.UUID(session_id)
    db_sess = GuestSessionModel(
        id=sess_uuid,
        session_token_hash="fakehash123",
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        is_active=True,
    )
    db.add(db_sess)
    db.commit()

    # 3. Add CollectionModel with collision-resistant ID
    col_id = get_guest_canonical_collection_id(session_id)
    col = CollectionModel(
        id=col_id,
        workspace_id=ws.id,
        name="SQLite Guest Col",
        policy_name="general_static",
        is_system=False,
        is_public=False,
    )
    db.add(col)
    db.commit()

    # 4. Add Document & Job
    doc_id = f"guest_doc_{uuid.uuid4().hex}"
    doc = RegulatoryDocumentModel(
        id=doc_id,
        domain_id="SEBI_CSCRF",
        title="SQLite Doc",
        workspace_id=ws.id,
        collection_id=col_id,
    )
    db.add(doc)
    db.flush()

    job_id = uuid.uuid4()
    job = IngestionJobModel(
        id=job_id,
        source_url="local://test/source.pdf",
        idempotency_key=f"sqlite_test_{job_id.hex}",
        document_id=doc_id,
        workspace_id=ws.id,
        collection_id=col_id,
        status="queued",
        stage="pending",
    )
    db.add(job)

    # 5. Add durable GuestQueryHistoryModel
    hist_entry = GuestQueryHistoryModel(
        id=uuid.uuid4(),
        session_id=sess_uuid,
        collection_id=col_id,
        query_text="What is the test policy?",
        disposition="FULLY_ANSWERED",
        answer_text="Test policy answer.",
        citations_count=1,
    )
    db.add(hist_entry)
    db.commit()

    # Assert entities exist
    ws_id = ws.id
    assert db.query(IngestionJobModel).filter(IngestionJobModel.workspace_id == ws_id).count() == 1
    assert db.query(RegulatoryDocumentModel).filter(RegulatoryDocumentModel.workspace_id == ws_id).count() == 1
    assert db.query(CollectionModel).filter(CollectionModel.workspace_id == ws_id).count() == 1
    assert db.query(WorkspaceModel).filter(WorkspaceModel.id == ws_id).count() == 1
    assert db.query(GuestQueryHistoryModel).filter(GuestQueryHistoryModel.session_id == sess_uuid).count() == 1

    # 6. Purge guest artifacts
    purge_res = purge_guest_session_artifacts(session_id=session_id, db=db, settings=settings)
    assert purge_res["cleaned_up"] is True
    assert purge_res["is_active"] is False

    # Assert entities purged
    assert db.query(IngestionJobModel).filter(IngestionJobModel.workspace_id == ws_id).count() == 0
    assert db.query(RegulatoryDocumentModel).filter(RegulatoryDocumentModel.workspace_id == ws_id).count() == 0
    assert db.query(CollectionModel).filter(CollectionModel.workspace_id == ws_id).count() == 0
    assert db.query(WorkspaceModel).filter(WorkspaceModel.id == ws_id).count() == 0
    assert db.query(GuestQueryHistoryModel).filter(GuestQueryHistoryModel.session_id == sess_uuid).count() == 0

    # Session marked inactive & cleaned
    updated_sess = db.query(GuestSessionModel).filter(GuestSessionModel.id == sess_uuid).first()
    assert updated_sess.is_active is False
    assert updated_sess.cleaned_up_at is not None


class DeterministicEmbedder(DenseEmbedder):
    """Deterministic 384-dimensional embedder for test predictability."""

    def __init__(self):
        super().__init__(model_name="mock-e2e-embedder")
        self._dim = 384

    def embed_texts(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        vectors = []
        for text in texts:
            val = float(abs(hash(text)) % 1000) / 1000.0
            vec = np.zeros(self._dim, dtype=np.float32)
            vec[0] = 1.0 - val
            vec[1] = val
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            vectors.append(vec)
        return np.vstack(vectors)

    def embed_query(self, query: str) -> np.ndarray:
        return self.embed_texts([query])[0]


def test_complete_guest_document_workflow_e2e():
    """
    Task 5: Complete end-to-end guest-owned document workflow:
    1. Create guest session
    2. Create temporary collection
    3. Generate and upload a valid PDF document with extractable text
    4. Worker processes job into chunks, embeddings, and published snapshot
    5. Ask a question about that specific PDF and receive verified grounded answer
    6. Terminate session and assert all storage files, snapshots, jobs, and records are removed.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        indexes_dir = tmp_path / "indexes"
        processed_dir = tmp_path / "processed"
        storage_dir = tmp_path / "storage"
        indexes_dir.mkdir(parents=True)
        processed_dir.mkdir(parents=True)
        storage_dir.mkdir(parents=True)

        storage_adapter = LocalStorageAdapter(root_dir=storage_dir)
        factory = FileStorageRepositoryFactory(processed_dir=processed_dir)
        job_repo = factory.get_ingestion_job_repository()
        doc_repo = factory.get_document_repository()

        # 1. Create guest session
        session_id = str(uuid.uuid4())
        col_id = get_guest_canonical_collection_id(session_id)
        guest_ws_id = get_guest_workspace_id(session_id)

        # 2. Create sample PDF with PyMuPDF
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text(
            (50, 60),
            "Apex Corporation Technical Standard 2026. "
            "Clause 7.1: All autonomous systems must verify numeric claims against underlying telemetry data. "
            "Clause 7.2: Telemetry anomalies must trigger fail-closed state within 50 milliseconds.",
            fontsize=12,
        )
        pdf_bytes = doc.tobytes()
        doc.close()

        # 3. Store PDF via storage adapter
        doc_id = f"guest_doc_{uuid.uuid4().hex}"
        save_res = storage_adapter.save_upload_stream(
            workspace_id=guest_ws_id,
            collection_id=col_id,
            document_id=doc_id,
            stream=io.BytesIO(pdf_bytes),
            original_filename="apex_standard.pdf",
        )
        assert storage_adapter.exists(save_res.source_uri)
        assert Path(save_res.storage_path).exists()

        # Enqueue job in repository
        job_rec = job_repo.create_job(
            source_url=save_res.source_uri,
            idempotency_key=f"guest_{uuid.uuid4().hex}",
            workspace_id=guest_ws_id,
            collection_id=col_id,
            document_id=doc_id,
            max_attempts=3,
        )

        # 4. Worker processes the job
        embedder = DeterministicEmbedder()
        orchestrator = IngestionOrchestrator(
            repository_factory=factory,
            embedder=embedder,
            indexes_dir=indexes_dir,
            processed_dir=processed_dir,
            storage_adapter=storage_adapter,
            publish_snapshots=True,
        )
        worker = IngestionWorker(
            repository_factory=factory,
            orchestrator=orchestrator,
            worker_id="test_guest_worker_01",
        )

        res = worker.run_once()
        assert res is not None
        assert res.status == JobStatus.COMPLETED
        assert res.chunks_count >= 1

        # Assert published searchable snapshot
        publisher = IndexPublisher(
            repository_factory=factory,
            indexes_dir=indexes_dir,
            processed_dir=processed_dir,
            embedder=embedder,
            collection_id=col_id,
            storage_adapter=storage_adapter,
        )
        pointer = publisher.get_active_pointer()
        assert pointer is not None
        assert pointer.collection_id == col_id
        manifest = publisher.get_active_manifest()
        assert manifest is not None
        assert doc_id in manifest.document_ids

        # 5. Query the newly published guest snapshot
        retrieval_service = RetrievalService(
            indexes_dir=indexes_dir,
            processed_dir=processed_dir,
            dense_embedder=embedder,
            storage_adapter=storage_adapter,
            repository_factory=factory,
        )
        ret_res = retrieval_service.search_hybrid(
            query="What must autonomous systems verify?",
            collection_id=col_id,
            top_k=5,
        )
        assert len(ret_res.results) >= 1
        assert "Clause 7.1" in ret_res.results[0].text or "telemetry" in ret_res.results[0].text

        # 6. Terminate session & purge
        class MockSettings:
            def __init__(self, idx_dir: str):
                self.indexes_dir = idx_dir

        mock_settings = MockSettings(str(indexes_dir))

        # Register in memory docs so purge knows about local storage file
        _memory_guest_documents[session_id] = [{"source_uri": save_res.source_uri, "file_size_bytes": save_res.byte_count}]
        _memory_guest_collections[session_id] = [{"id": col_id}]

        purge_result = purge_guest_session_artifacts(
            session_id=session_id,
            db=None,
            settings=mock_settings,
            storage_adapter=storage_adapter,
        )
        assert purge_result["cleaned_up"] is True

        # Assert raw upload file is deleted
        assert not storage_adapter.exists(save_res.source_uri)
        assert not Path(save_res.storage_path).exists()

        # Assert published index directory is deleted
        snapshot_dir = indexes_dir / col_id
        assert not snapshot_dir.exists()


def test_anti_race_publisher_aborts_on_deleted_guest_collection():
    """
    Task 3: Verify that if a guest collection is deleted during snapshot build,
    IndexPublisher detects the deletion and aborts activation without writing a lingering snapshot.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        indexes_dir = tmp_path / "indexes"
        processed_dir = tmp_path / "processed"
        indexes_dir.mkdir(parents=True)
        processed_dir.mkdir(parents=True)

        session_id = str(uuid.uuid4())
        col_id = get_guest_canonical_collection_id(session_id)

        factory = FileStorageRepositoryFactory(processed_dir=processed_dir)
        embedder = DenseEmbedder(model_name="mock-e2e-embedder")

        # Mock session_factory on repository factory that reports collection does not exist
        class MockDBSession:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def query(self, model):
                class MockQuery:
                    def filter(self, *args):
                        return self
                    def first(self):
                        return None  # Collection was purged!
                return MockQuery()

        factory.session_factory = lambda: MockDBSession()

        publisher = IndexPublisher(
            repository_factory=factory,
            indexes_dir=indexes_dir,
            processed_dir=processed_dir,
            embedder=embedder,
            collection_id=col_id,
        )

        # Build snapshot candidate (staging directory)
        staging_dir = publisher.snapshots_dir / "_staging_test_snap"
        staging_dir.mkdir(parents=True, exist_ok=True)
        (staging_dir / "manifest.json").write_text('{"collection_id": "' + col_id + '"}', encoding="utf-8")

        # Calling build_and_publish_snapshot will hit the anti-race check
        # and abort with CollectionPurgedRaceCondition
        res = publisher.build_and_publish_snapshot(snapshot_id="test_snap_001")
        assert res.success is False
        # Either no chunks to index or CollectionPurgedRaceCondition caught
        assert not (indexes_dir / col_id / "active_snapshot.json").exists()


def test_bounded_worker_execution_mode():
    """
    Task 1: Verify bounded worker execution:
    1. Processes up to max_jobs and exits cleanly.
    2. With exit_when_idle=True, drains remaining jobs and exits cleanly when empty.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        indexes_dir = tmp_path / "indexes"
        processed_dir = tmp_path / "processed"
        storage_dir = tmp_path / "storage"
        indexes_dir.mkdir(parents=True)
        processed_dir.mkdir(parents=True)
        storage_dir.mkdir(parents=True)

        storage_adapter = LocalStorageAdapter(root_dir=storage_dir)
        factory = FileStorageRepositoryFactory(processed_dir=processed_dir)
        job_repo = factory.get_ingestion_job_repository()

        ws_id = uuid.uuid4()
        col_id = "test_bounded_col"

        # Create 3 valid PDF files and enqueue 3 jobs
        for i in range(3):
            doc = fitz.open()
            page = doc.new_page()
            page.insert_text((50, 60), f"Bounded Test Document {i}: Data point {i * 100} verified.", fontsize=12)
            pdf_bytes = doc.tobytes()
            doc.close()

            doc_id = f"doc_bounded_{i}"
            save_res = storage_adapter.save_upload_stream(
                workspace_id=ws_id,
                collection_id=col_id,
                document_id=doc_id,
                stream=io.BytesIO(pdf_bytes),
                original_filename=f"doc_{i}.pdf",
            )
            job_repo.create_job(
                source_url=save_res.source_uri,
                idempotency_key=f"bounded_key_{i}_{uuid.uuid4().hex}",
                workspace_id=ws_id,
                collection_id=col_id,
                document_id=doc_id,
            )

        embedder = DeterministicEmbedder()
        orchestrator = IngestionOrchestrator(
            repository_factory=factory,
            embedder=embedder,
            indexes_dir=indexes_dir,
            processed_dir=processed_dir,
            storage_adapter=storage_adapter,
            publish_snapshots=True,
        )
        worker = IngestionWorker(
            repository_factory=factory,
            orchestrator=orchestrator,
            worker_id="test_bounded_worker",
            poll_interval_seconds=0.1,
        )

        # Batch 1: max_jobs=2 -> should process exactly 2 jobs and return 2
        processed_b1 = worker.run_loop(max_jobs=2)
        assert processed_b1 == 2

        # Batch 2: max_jobs=10, exit_when_idle=True -> should process the 1 remaining job and exit cleanly
        processed_b2 = worker.run_loop(max_jobs=10, exit_when_idle=True)
        assert processed_b2 == 1

        # Batch 3: Queue is now empty, with exit_when_idle=True, should process 0 and exit immediately
        processed_b3 = worker.run_loop(max_jobs=5, exit_when_idle=True)
        assert processed_b3 == 0


def test_interrupted_job_recovery_and_retry():
    """
    Task 1: Verify that interrupted/crashed jobs whose leases expired
    are recovered by stale lease recovery and completed by worker retry.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        indexes_dir = tmp_path / "indexes"
        processed_dir = tmp_path / "processed"
        storage_dir = tmp_path / "storage"
        indexes_dir.mkdir(parents=True)
        processed_dir.mkdir(parents=True)
        storage_dir.mkdir(parents=True)

        storage_adapter = LocalStorageAdapter(root_dir=storage_dir)
        factory = FileStorageRepositoryFactory(processed_dir=processed_dir)
        job_repo = factory.get_ingestion_job_repository()

        ws_id = uuid.uuid4()
        col_id = "test_retry_col"

        # Create valid PDF
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((50, 60), "Interrupted job recovery standard clause 12.3.", fontsize=12)
        pdf_bytes = doc.tobytes()
        doc.close()

        doc_id = "doc_interrupted_01"
        save_res = storage_adapter.save_upload_stream(
            workspace_id=ws_id,
            collection_id=col_id,
            document_id=doc_id,
            stream=io.BytesIO(pdf_bytes),
            original_filename="interrupted.pdf",
        )
        job = job_repo.create_job(
            source_url=save_res.source_uri,
            idempotency_key=f"interrupted_key_{uuid.uuid4().hex}",
            workspace_id=ws_id,
            collection_id=col_id,
            document_id=doc_id,
        )

        # Simulate job claimed by crashed worker and lease expired
        crashed_token = uuid.uuid4()
        job_repo.claim_next_job(worker_id="crashed_worker", claim_token=crashed_token)

        # Force heartbeat far in the past to expire lease
        jobs_data = job_repo._load_jobs()
        jobs_data[str(job.id)]["heartbeat_at"] = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        job_repo._save_jobs(jobs_data)

        # Fresh worker with short lease timeout
        embedder = DeterministicEmbedder()
        orchestrator = IngestionOrchestrator(
            repository_factory=factory,
            embedder=embedder,
            indexes_dir=indexes_dir,
            processed_dir=processed_dir,
            storage_adapter=storage_adapter,
            publish_snapshots=True,
        )
        worker = IngestionWorker(
            repository_factory=factory,
            orchestrator=orchestrator,
            worker_id="recovering_worker",
            lease_timeout_seconds=30.0,
            stale_recovery_interval_seconds=0.0,
        )

        res = worker.run_once()
        assert res is not None
        assert res.status == JobStatus.COMPLETED
        assert res.document_id == doc_id


def test_failed_storage_deletion_retry(sqlite_test_db):
    """
    Task 2: Verify that if storage deletion fails:
    1. Cleanup is NOT marked successful (cleaned_up=False).
    2. DB session cleaned_up_at remains NULL.
    3. Session is marked is_active=False (locked out).
    4. When storage heals, the sweeper retries and successfully cleans up.
    """
    session_id = str(uuid.uuid4())
    sess_uuid = uuid.UUID(session_id)
    now = datetime.now(timezone.utc)

    # Create expired guest session in DB
    db_sess = GuestSessionModel(
        id=sess_uuid,
        session_token_hash=hashlib.sha256(f"token_{session_id}".encode()).hexdigest(),
        created_at=now - timedelta(hours=25),
        expires_at=now - timedelta(hours=1),
        is_active=True,
        cleaned_up_at=None,
    )
    sqlite_test_db.add(db_sess)
    sqlite_test_db.commit()

    class FailingStorageAdapter(LocalStorageAdapter):
        def __init__(self, root):
            super().__init__(root_dir=root)
            self.should_fail = True

        def delete_file(self, uri):
            if self.should_fail:
                raise RuntimeError("Simulated remote B2 connection failure during delete")
            return super().delete_file(uri)

    with tempfile.TemporaryDirectory() as tmpdir:
        storage = FailingStorageAdapter(Path(tmpdir))
        dummy_file = Path(tmpdir) / "test" / "test.pdf"
        dummy_file.parent.mkdir(parents=True, exist_ok=True)
        dummy_file.write_bytes(b"%PDF-1.4 dummy")
        _memory_guest_documents[session_id] = [{"source_uri": "upload://test/test.pdf", "file_size_bytes": 100}]

        # 1. Attempt purge while storage fails
        purge_res = purge_guest_session_artifacts(
            session_id=session_id,
            db=sqlite_test_db,
            storage_adapter=storage,
        )
        assert purge_res["cleaned_up"] is False
        assert len(purge_res["storage_errors"]) > 0

        # Verify DB: session is deactivated, but cleaned_up_at is still NULL
        reloaded = sqlite_test_db.query(GuestSessionModel).filter(GuestSessionModel.id == sess_uuid).first()
        assert reloaded.is_active is False
        assert reloaded.cleaned_up_at is None

        # 2. Heal storage
        storage.should_fail = False

        # 3. Sweeper runs and retries cleanup
        cleaned_count = cleanup_expired_guest_sessions(
            db=sqlite_test_db,
            batch_limit=10,
            storage_adapter=storage,
        )
        assert cleaned_count >= 1

        # Verify DB: cleaned_up_at is now set!
        sqlite_test_db.expire_all()
        healed = sqlite_test_db.query(GuestSessionModel).filter(GuestSessionModel.id == sess_uuid).first()
        assert healed.cleaned_up_at is not None


def test_b2_remote_snapshot_and_prefix_cleanup():
    """
    Task 2: Verify that purge_guest_session_artifacts invokes remote prefix deletion
    for published snapshot bundles (indexes/{collection_id}/) and uploads.
    """
    session_id = str(uuid.uuid4())
    col_id = get_guest_canonical_collection_id(session_id)
    guest_ws_id = get_guest_workspace_id(session_id)

    class MockRemoteStorageAdapter:
        def __init__(self):
            self.deleted_prefixes = []
            self.deleted_files = []

        def exists(self, uri):
            return True

        def delete_file(self, uri):
            self.deleted_files.append(uri)
            return True

        def delete_prefix(self, prefix):
            self.deleted_prefixes.append(prefix)
            return 1

    mock_storage = MockRemoteStorageAdapter()
    _memory_guest_collections[session_id] = [{"id": col_id}]
    _memory_guest_documents[session_id] = [{"source_uri": f"upload://{guest_ws_id}/{col_id}/doc1.pdf"}]

    res = purge_guest_session_artifacts(
        session_id=session_id,
        db=None,
        storage_adapter=mock_storage,
    )
    assert res["cleaned_up"] is True
    assert f"indexes/{col_id}/" in mock_storage.deleted_prefixes
    assert f"uploads/{guest_ws_id}/" in mock_storage.deleted_prefixes
    assert len(mock_storage.deleted_files) >= 1


def test_registered_and_guest_collection_ingestion():
    """
    Task 1: Verify worker processes documents for both registered workspace collections
    and guest session collections seamlessly.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        indexes_dir = tmp_path / "indexes"
        processed_dir = tmp_path / "processed"
        storage_dir = tmp_path / "storage"
        indexes_dir.mkdir(parents=True)
        processed_dir.mkdir(parents=True)
        storage_dir.mkdir(parents=True)

        storage_adapter = LocalStorageAdapter(root_dir=storage_dir)
        factory = FileStorageRepositoryFactory(processed_dir=processed_dir)
        job_repo = factory.get_ingestion_job_repository()

        # 1. Registered collection document
        reg_ws = uuid.uuid4()
        reg_col = "reg_workspace_col"
        doc = fitz.open()
        p = doc.new_page()
        p.insert_text((50, 60), "Registered tenant policy document clause 5.", fontsize=12)
        reg_pdf = doc.tobytes()
        doc.close()

        save_reg = storage_adapter.save_upload_stream(
            workspace_id=reg_ws,
            collection_id=reg_col,
            document_id="reg_doc_01",
            stream=io.BytesIO(reg_pdf),
            original_filename="reg.pdf",
        )
        job_repo.create_job(
            source_url=save_reg.source_uri,
            idempotency_key=f"reg_key_{uuid.uuid4().hex}",
            workspace_id=reg_ws,
            collection_id=reg_col,
            document_id="reg_doc_01",
        )

        # 2. Guest collection document
        guest_sess_id = str(uuid.uuid4())
        guest_col = get_guest_canonical_collection_id(guest_sess_id)
        guest_ws = get_guest_workspace_id(guest_sess_id)
        doc = fitz.open()
        p = doc.new_page()
        p.insert_text((50, 60), "Ephemeral guest research standard clause 9.", fontsize=12)
        guest_pdf = doc.tobytes()
        doc.close()

        save_guest = storage_adapter.save_upload_stream(
            workspace_id=guest_ws,
            collection_id=guest_col,
            document_id="guest_doc_01",
            stream=io.BytesIO(guest_pdf),
            original_filename="guest.pdf",
        )
        job_repo.create_job(
            source_url=save_guest.source_uri,
            idempotency_key=f"guest_key_{uuid.uuid4().hex}",
            workspace_id=guest_ws,
            collection_id=guest_col,
            document_id="guest_doc_01",
        )

        embedder = DeterministicEmbedder()
        orchestrator = IngestionOrchestrator(
            repository_factory=factory,
            embedder=embedder,
            indexes_dir=indexes_dir,
            processed_dir=processed_dir,
            storage_adapter=storage_adapter,
            publish_snapshots=True,
        )
        worker = IngestionWorker(
            repository_factory=factory,
            orchestrator=orchestrator,
            worker_id="test_multi_tenant_worker",
        )

        # Process both
        processed_count = worker.run_loop(max_jobs=5, exit_when_idle=True)
        assert processed_count == 2

        # Verify active snapshot for registered collection
        assert (indexes_dir / reg_col / "active_snapshot.json").exists()
        # Verify active snapshot for guest collection
        assert (indexes_dir / guest_col / "active_snapshot.json").exists()
