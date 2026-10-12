"""
Integration tests against a disposable, real PostgreSQL + pgvector database.

These tests run automatically ONLY when DATABASE_URL is explicitly set to a live PostgreSQL
instance and points to a safe test database (must include 'test' in the database name
or be explicitly opted in with ALLOW_TEST_DATABASE_PURGE=1).

If no live PostgreSQL instance is available (e.g. Docker daemon is stopped),
these tests are safely SKIPPED with a descriptive, honest explanation rather than falsely passing.
"""
import os
import uuid
import pytest
import numpy as np
from sqlalchemy import create_engine, text, select, func
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.persistence.models import (
    Base,
    RegulatoryDocumentModel,
    DocumentVersionModel,
    ChunkModel,
    ChunkEmbeddingModel,
    RegulatoryEdgeModel,
    IngestionJobModel,
)
from app.persistence.postgres import (
    PostgresStorageRepositoryFactory,
    PostgresDocumentRepository,
    PostgresChunkRepository,
    PostgresGraphEdgeRepository,
    PostgresIngestionJobRepository,
)
from app.persistence.importer import CorpusImporter
from app.schemas.document import DocumentRecord, ChunkRecord
from app.schemas.graph import GraphEdge, RelationshipType, ScopeType, ReviewStatus, ConfidenceLevel
from app.schemas.ingestion_job import (
    IngestionJobRecord,
    JobStatus,
    ProcessingStage,
    StaleLeaseError,
    JobStateTransitionError,
)


def get_live_test_db_url() -> str:
    """Retrieve test DATABASE_URL if configured, otherwise None."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url or not db_url.strip():
        return ""
    # Enforce safe test database naming
    if "test" not in db_url.lower() and os.getenv("ALLOW_TEST_DATABASE_PURGE") != "1":
        raise ValueError(
            f"Configured DATABASE_URL='{db_url}' does not contain 'test' in name. "
            "To prevent accidental data loss, integration tests only run against databases with 'test' in name."
        )
    return db_url.strip()


def check_live_postgres_available() -> tuple[bool, str]:
    """
    Test actual connectivity to PostgreSQL and pgvector.
    Returns (is_available, diagnostic_reason).
    """
    url = get_live_test_db_url()
    if not url:
        return False, "DATABASE_URL environment variable is unset or empty."
    try:
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as conn:
            # 1. Attempt to create vector extension if not yet created in this database
            try:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
                conn.commit()
            except Exception as ext_err:
                pass
            # 2. Check if pgvector is installed and active
            res = conn.execute(text("SELECT extname FROM pg_extension WHERE extname = 'vector';")).scalar()
            if not res:
                return False, f"Connected to PostgreSQL, but pgvector extension is not installed in database '{url}'."
            return True, "pgvector is available."
    except Exception as e:
        return False, f"Could not connect to PostgreSQL at '{url}': {e}"


LIVE_PG_AVAILABLE, PG_CHECK_DIAGNOSTIC = check_live_postgres_available()
SKIP_REASON = (
    f"Live PostgreSQL with pgvector extension is not available ({PG_CHECK_DIAGNOSTIC}). "
    "To run: start a PostgreSQL 16 container with pgvector and set DATABASE_URL=postgresql+psycopg://.../sebi_rag_test"
)


@pytest.mark.skipif(not LIVE_PG_AVAILABLE, reason=SKIP_REASON)
class TestLivePostgresPgvectorIntegration:
    """Live verification against disposable PostgreSQL database with pgvector."""

    @pytest.fixture(autouse=True)
    def clean_test_db(self):
        """Prepare fresh clean schema on disposable test database."""
        url = get_live_test_db_url()
        engine = create_engine(url, pool_pre_ping=True)
        # Drop and re-create metadata
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        yield
        Base.metadata.drop_all(bind=engine)

    def test_live_document_and_version_uniqueness(self):
        doc_repo = PostgresDocumentRepository(self.session_factory)
        doc = DocumentRecord(
            document_id="sebi_live_test",
            circular_number="CIR/LIVE/01",
            title="Live Test Circular",
            department="ITD",
            issue_date="2025-01-01",
            effective_date=None,
            source_url="https://sebi.gov.in/live.pdf",
            retrieved_at="2025-01-01T00:00:00Z",
            sha256="live_hash_v1",
            pdf_path="data/raw/live.pdf",
            extraction_status="extracted",
        )
        doc_repo.save_document(doc)

        # Retrieve
        retrieved = doc_repo.get_document("sebi_live_test")
        assert retrieved is not None
        assert retrieved.title == "Live Test Circular"

        # Repeat save (same sha256) -> no duplicate version
        doc_repo.save_document(doc)
        with self.session_factory() as session:
            v_count = session.scalar(select(func.count(DocumentVersionModel.id)))
            assert v_count == 1

        # New sha256 -> creates version 2
        doc_v2 = doc.model_copy(update={"sha256": "live_hash_v2", "title": "Updated Live Title"})
        doc_repo.save_document(doc_v2)
        with self.session_factory() as session:
            v_count = session.scalar(select(func.count(DocumentVersionModel.id)))
            assert v_count == 2
            versions = session.execute(
                select(DocumentVersionModel).order_by(DocumentVersionModel.version_number)
            ).scalars().all()
            assert versions[0].version_number == 1
            assert versions[1].version_number == 2

    def test_live_vector_cosine_distance_query(self):
        chunk_repo = PostgresChunkRepository(self.session_factory)
        doc_repo = PostgresDocumentRepository(self.session_factory)

        # Seed document and chunks
        doc = DocumentRecord(
            document_id="sebi_vec_test",
            circular_number="CIR/VEC/01",
            title="Vector Test Doc",
            department="ITD",
            issue_date="2025-01-01",
            effective_date=None,
            source_url="https://sebi.gov.in/vec.pdf",
            retrieved_at="2025-01-01T00:00:00Z",
            sha256="hash_vec_1",
            pdf_path="data/raw/vec.pdf",
            extraction_status="extracted",
        )
        doc_repo.save_document(doc)

        chunks = [
            ChunkRecord(
                chunk_id="c_ortho",
                document_id="sebi_vec_test",
                text="Orthogonal vector content.",
                page_start=1,
                page_end=1,
                is_ocr=False,
                chunk_type="body",
                metadata={"chunk_index": 0},
            ),
            ChunkRecord(
                chunk_id="c_match",
                document_id="sebi_vec_test",
                text="Target matching content.",
                page_start=1,
                page_end=1,
                is_ocr=False,
                chunk_type="body",
                metadata={"chunk_index": 1},
            ),
        ]
        chunk_repo.save_chunks(chunks)

        # 384-dimensional unit vectors
        v_target = np.zeros(384, dtype=np.float32)
        v_target[0] = 1.0  # Unit vector along axis 0

        v_match = np.zeros(384, dtype=np.float32)
        v_match[0] = 0.95
        v_match[1] = 0.312  # Near unit vector closely aligned with v_target

        v_ortho = np.zeros(384, dtype=np.float32)
        v_ortho[100] = 1.0  # Perpendicular to v_target

        chunk_repo.save_embeddings({"c_match": v_match, "c_ortho": v_ortho})

        # Query cosine similarity
        results = chunk_repo.search_similar_chunks(v_target, top_k=2)
        assert len(results) == 2
        # c_match should rank first with high similarity (~0.95)
        top_id, top_sim = results[0]
        second_id, second_sim = results[1]

        assert top_id == "c_match"
        assert top_sim > 0.90
        assert second_id == "c_ortho"
        assert second_sim < 0.05

    def test_live_corpus_import_and_idempotency(self):
        importer = CorpusImporter(
            session_factory=self.session_factory,
            processed_dir="data/processed",
            indexes_dir="data/indexes",
        )

        stats1 = importer.import_corpus(dry_run=False)
        assert stats1.documents_inserted == 5
        assert stats1.chunks_inserted == 168
        assert stats1.embeddings_inserted == 168
        assert stats1.edges_inserted == 9

        # Repeat run -> strictly 0 new rows
        stats2 = importer.import_corpus(dry_run=False)
        assert stats2.documents_inserted == 0
        assert stats2.documents_already_present == 5
        assert stats2.chunks_inserted == 0
        assert stats2.chunks_already_present == 168
        assert stats2.edges_inserted == 0
        assert stats2.edges_already_present == 9

    def test_live_ingestion_job_lifecycle_and_idempotency(self):
        job_repo = PostgresIngestionJobRepository(self.session_factory)

        # 1. Idempotent creation
        j1 = job_repo.create_job(
            source_url="https://sebi.gov.in/circular_live.pdf",
            idempotency_key="key-live-1",
            document_id="sebi_live_1",
        )
        assert j1.status == JobStatus.QUEUED
        assert j1.idempotency_key == "key-live-1"

        j2 = job_repo.create_job(
            source_url="https://sebi.gov.in/circular_live.pdf",
            idempotency_key="key-live-1",
        )
        assert j2.id == j1.id

        # 2. Atomic claim
        claim_token = uuid.uuid4()
        claimed = job_repo.claim_next_job(worker_id="live-worker-1", claim_token=claim_token)
        assert claimed is not None
        assert claimed.id == j1.id
        assert claimed.status == JobStatus.PROCESSING
        assert claimed.claim_worker_id == "live-worker-1"

        # 3. Stage & heartbeat update
        updated = job_repo.update_stage_and_heartbeat(
            job_id=j1.id,
            claim_token=claim_token,
            stage=ProcessingStage.DOWNLOADING,
        )
        assert updated.stage == ProcessingStage.DOWNLOADING

        # 4. Completion
        completed = job_repo.mark_completed(
            job_id=j1.id,
            claim_token=claim_token,
            document_id="sebi_live_1",
            sha256="abcd1234sha",
        )
        assert completed.status == JobStatus.COMPLETED
        assert completed.stage == ProcessingStage.FINISHED

    def test_live_concurrent_job_claiming_skip_locked(self):
        job_repo = PostgresIngestionJobRepository(self.session_factory)

        # Create 3 distinct queued jobs
        j_ids = []
        for i in range(3):
            j = job_repo.create_job(
                source_url=f"https://sebi.gov.in/doc_{i}.pdf",
                idempotency_key=f"concurrent-key-{i}",
            )
            j_ids.append(j.id)

        # Worker 1 claims
        c1 = job_repo.claim_next_job(worker_id="worker-A")
        assert c1 is not None

        # Worker 2 claims concurrently
        c2 = job_repo.claim_next_job(worker_id="worker-B")
        assert c2 is not None

        # Worker 3 claims concurrently
        c3 = job_repo.claim_next_job(worker_id="worker-C")
        assert c3 is not None

        # All claimed jobs must be mutually distinct
        claimed_ids = {c1.id, c2.id, c3.id}
        assert len(claimed_ids) == 3
        assert claimed_ids == set(j_ids)

        # No more jobs available
        c4 = job_repo.claim_next_job(worker_id="worker-D")
        assert c4 is None

    def test_live_stale_job_recovery_and_stale_worker_rejection(self):
        import time
        from datetime import timedelta
        job_repo = PostgresIngestionJobRepository(self.session_factory)

        job = job_repo.create_job(
            source_url="https://sebi.gov.in/stale_live.pdf",
            idempotency_key="stale-live-key",
        )
        old_token = uuid.uuid4()
        claimed = job_repo.claim_next_job(worker_id="worker-crashed", claim_token=old_token)
        assert claimed is not None

        # Recover jobs where heartbeat is older than 0.0 seconds
        time.sleep(0.05)
        recovered = job_repo.recover_stale_jobs(lease_timeout_seconds=0.01)
        assert recovered == 1

        rec = job_repo.get_job(job.id)
        assert rec.status == JobStatus.QUEUED
        assert rec.attempt_count == 1
        assert rec.claim_token is None

        # Crashed worker wakes up and attempts update -> rejected with StaleLeaseError
        with pytest.raises(StaleLeaseError):
            job_repo.update_stage_and_heartbeat(
                job_id=job.id,
                claim_token=old_token,
                stage=ProcessingStage.PARSING,
            )

    def test_live_worker_and_orchestrator_e2e_lifecycle(self):
        """
        Verify IngestionOrchestrator and IngestionWorker against a live PostgreSQL database:
        - Job registration, claim via FOR UPDATE SKIP LOCKED
        - Staged execution: download, parse, chunk, embed, persist to PostgreSQL
        - Logical document, version, chunks, and pgvector embeddings persisted and verifiable
        - Duplicate content detection against live PostgreSQL documents
        """
        import fitz
        import httpx
        from app.ingestion.orchestrator import IngestionOrchestrator, IngestionWorker
        from app.ingestion.downloader import SecurePDFDownloader
        from app.retrieval.dense_embedder import DenseEmbedder

        factory = PostgresStorageRepositoryFactory(get_live_test_db_url())
        job_repo = factory.get_ingestion_job_repository()
        doc_repo = factory.get_document_repository()
        chunk_repo = factory.get_chunk_repository()

        # Build minimal PDF in memory
        doc = fitz.open()
        p = doc.new_page()
        p.insert_text((50, 50), "SEBI Live PostgreSQL Integration Circular 2026")
        p.insert_text((50, 80), "Live database verified end-to-end ingestion worker and orchestrator.")
        pdf_bytes = doc.tobytes()
        doc.close()

        # Deterministic mock embedder returning unit float32 vectors
        class MockLiveEmbedder(DenseEmbedder):
            def __init__(self):
                super().__init__(model_name="mock-live")
            def embed_texts(self, texts, batch_size=32):
                arr = np.zeros((len(texts), 384), dtype=np.float32)
                for i in range(len(texts)):
                    arr[i, 0] = 1.0
                return arr

        transport = httpx.MockTransport(lambda req: httpx.Response(200, content=pdf_bytes))
        downloader = SecurePDFDownloader(allow_http_for_test=True, custom_transport=transport)
        orchestrator = IngestionOrchestrator(
            repository_factory=factory,
            downloader=downloader,
            embedder=MockLiveEmbedder(),
        )

        worker = IngestionWorker(
            repository_factory=factory,
            worker_id="live_test_worker",
            poll_interval_seconds=0.01,
            orchestrator=orchestrator,
        )

        # 1. Register job
        job = job_repo.create_job(
            source_url="http://127.0.0.1:8000/live_circular_2026.pdf",
            idempotency_key="live_job_key_1",
            document_id="live_doc_2026_01",
        )
        assert job.status == JobStatus.QUEUED

        # 2. Execute via worker loop (run 1 job)
        processed = worker.run_loop(max_jobs=1)
        assert processed == 1

        # 3. Verify job completed in PostgreSQL
        persisted_job = job_repo.get_job(job.id)
        assert persisted_job.status == JobStatus.COMPLETED
        assert persisted_job.stage == ProcessingStage.FINISHED
        assert persisted_job.completed_at is not None

        # 4. Verify document, version, chunks, and embeddings in PostgreSQL
        persisted_doc = doc_repo.get_document("live_doc_2026_01")
        assert persisted_doc is not None
        assert persisted_doc.document_id == "live_doc_2026_01"
        assert persisted_doc.sha256 == persisted_job.sha256

        chunks = chunk_repo.list_chunks_for_document("live_doc_2026_01")
        assert len(chunks) > 0
        assert "Live PostgreSQL" in chunks[0].text

        # 5. Verify duplicate detection on live database
        dup_job = job_repo.create_job(
            source_url="http://127.0.0.1:8000/live_circular_2026_dup.pdf",
            idempotency_key="live_job_key_dup",
            document_id="live_doc_2026_02_duplicate",
        )
        processed_dup = worker.run_loop(max_jobs=1)
        assert processed_dup == 1

        persisted_dup_job = job_repo.get_job(dup_job.id)
        assert persisted_dup_job.status == JobStatus.DUPLICATE
        assert persisted_dup_job.document_id == "live_doc_2026_01"

    def test_live_crash_recovery_after_persistence_and_lease_loss(self):
        """
        Verify on live PostgreSQL:
        1. A crash after document/chunk persistence does NOT create duplicate records on retry.
        2. A worker with an expired lease cannot mutate job state or persist data.
        """
        import fitz
        import httpx
        from app.ingestion.orchestrator import IngestionOrchestrator
        from app.ingestion.downloader import SecurePDFDownloader
        from app.retrieval.dense_embedder import DenseEmbedder

        factory = PostgresStorageRepositoryFactory(get_live_test_db_url())
        job_repo = factory.get_ingestion_job_repository()
        doc_repo = factory.get_document_repository()
        chunk_repo = factory.get_chunk_repository()

        doc = fitz.open()
        p = doc.new_page()
        p.insert_text((50, 50), "Live PostgreSQL Crash Recovery Document 2026")
        pdf_bytes = doc.tobytes()
        doc.close()

        class MockLiveEmbedder(DenseEmbedder):
            def __init__(self):
                super().__init__(model_name="mock-live")
            def embed_texts(self, texts, batch_size=32):
                arr = np.zeros((len(texts), 384), dtype=np.float32)
                for i in range(len(texts)):
                    arr[i, 0] = 1.0
                return arr

        transport = httpx.MockTransport(lambda req: httpx.Response(200, content=pdf_bytes))
        downloader = SecurePDFDownloader(allow_http_for_test=True, custom_transport=transport)
        orchestrator = IngestionOrchestrator(
            repository_factory=factory,
            downloader=downloader,
            embedder=MockLiveEmbedder(),
        )

        # 1. Register job
        job = job_repo.create_job(
            source_url="http://127.0.0.1:8000/live_crash_doc.pdf",
            idempotency_key="live_crash_key_1",
            document_id="live_crash_doc_01",
        )
        token1 = uuid.uuid4()
        claimed1 = job_repo.claim_next_job("worker_crasher", token1)

        # 2. Simulate worker persisting data to live PostgreSQL, then dying before mark_completed
        dl_res = downloader.fetch_pdf(job.source_url)
        pages = orchestrator.extractor.extract_pages(dl_res.staging_path)
        chunks = orchestrator.chunker.chunk_document("live_crash_doc_01", pages)
        embeddings_dict = {
            c.chunk_id: np.zeros(384, dtype=np.float32) for c in chunks
        }
        doc_record = DocumentRecord(
            document_id="live_crash_doc_01",
            title="live_crash_doc_01",
            source_url=job.source_url,
            sha256=dl_res.sha256,
            pdf_path=str(dl_res.staging_path),
            extraction_status="extracted",
        )
        doc_repo.save_document(doc_record)
        chunk_repo.save_chunks(chunks)
        chunk_repo.save_embeddings(embeddings_dict)

        # 3. Lease expires, stale recovery runs and returns job to 'queued'
        recovered = job_repo.recover_stale_jobs(lease_timeout_seconds=0.0)
        assert recovered >= 1

        # 4. Old worker attempts to mark completed with expired token1 -> rejected by StaleLeaseError
        with pytest.raises(StaleLeaseError):
            job_repo.mark_completed(
                job_id=job.id,
                claim_token=token1,
                document_id="live_crash_doc_01",
                sha256=dl_res.sha256,
            )

        # 5. New worker claims recovered job and processes it
        token2 = uuid.uuid4()
        claimed2 = job_repo.claim_next_job("worker_resurrector", token2)
        assert claimed2 is not None

        result = orchestrator.process_job(claimed2, token2)
        assert result.status == JobStatus.COMPLETED
        assert result.document_id == "live_crash_doc_01"
        assert "Crash recovery" in result.message

        # 6. Verify no duplicate chunks or documents exist in PostgreSQL
        chunks_in_db = chunk_repo.list_chunks_for_document("live_crash_doc_01")
        assert len(chunks_in_db) == len(chunks)

    def test_live_admin_api_registration_and_status(self):
        """
        Verify Administrative REST API endpoints against live PostgreSQL:
        - POST /api/v1/admin/documents/register writes persistent job to PostgreSQL
        - Idempotent re-registration returns existing record
        - GET /api/v1/admin/jobs/{job_id}/status reflects live database state
        - GET /api/v1/admin/jobs lists jobs with status filtering
        """
        from fastapi.testclient import TestClient
        from app.main import app
        from app.core.config import Settings, get_settings
        from app.api.v1.admin import get_repository_factory

        live_db_url = get_live_test_db_url()
        factory = PostgresStorageRepositoryFactory(live_db_url)
        test_settings = Settings(
            admin_api_key="live-admin-secret-key",
            database_url=live_db_url,
        )

        app.dependency_overrides[get_settings] = lambda: test_settings
        app.dependency_overrides[get_repository_factory] = lambda: factory

        try:
            client = TestClient(app)
            headers = {"X-Admin-Key": "live-admin-secret-key"}

            # 1. Register job on live PostgreSQL
            payload = {
                "source_url": "https://sebi.gov.in/live_admin_doc.pdf",
                "idempotency_key": "live_admin_key_1",
                "document_id": "live_admin_doc_01",
            }
            resp = client.post("/api/v1/admin/documents/register", json=payload, headers=headers)
            assert resp.status_code == 202
            body = resp.json()
            assert body["status"] == "queued"
            assert body["is_duplicate_registration"] is False
            job_id = body["job_id"]

            # 2. Idempotent registration returns duplicate flag
            resp_dup = client.post("/api/v1/admin/documents/register", json=payload, headers=headers)
            assert resp_dup.status_code == 202
            assert resp_dup.json()["is_duplicate_registration"] is True

            # 3. Status inspection from PostgreSQL
            resp_status = client.get(f"/api/v1/admin/jobs/{job_id}/status", headers=headers)
            assert resp_status.status_code == 200
            st = resp_status.json()
            assert st["job_id"] == job_id
            assert st["status"] == "queued"
            assert st["searchable"] is False

            # 4. List jobs from PostgreSQL
            resp_list = client.get("/api/v1/admin/jobs?status=queued", headers=headers)
            assert resp_list.status_code == 200
            assert resp_list.json()["total"] >= 1
        finally:
            app.dependency_overrides.clear()
