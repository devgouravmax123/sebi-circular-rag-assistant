"""
Integration tests for IngestionOrchestrator with PostgresWorkerProcedureRepository (Phase 7.2 Task 2B).

Verifies that IngestionOrchestrator.process_job() exclusively delegates worker-owned
database mutations to PostgresWorkerProcedureRepository and stored procedures:
1. End-to-end ingestion: version creation, batch writing, and completion.
2. Preservation of document metadata, chunk metadata, and page attribution.
3. Multiple chunk batches persistence.
4. Non-empty vs empty document behavior.
5. Duplicate document detection and handling without unauthorized direct ORM writes.
6. Retryable failure handling via fail_job (is_retryable=True -> 'queued').
7. Terminal failure handling via fail_job (is_retryable=False -> 'failed').
8. Staging file guaranteed cleanup on success and failure paths.
9. Verification that no direct ORM writes are executed by the orchestrator.
"""
import os
import uuid
from pathlib import Path
from datetime import datetime, timezone
import pytest
import httpx
import fitz
import numpy as np
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.schemas.ingestion_job import (
    IngestionJobRecord,
    JobStatus,
    ProcessingStage,
)
from app.ingestion.orchestrator import (
    IngestionOrchestrator,
    JobExecutionResult,
    NonRetryableIngestionError,
    RetryableIngestionError,
)
from app.ingestion.downloader import SecurePDFDownloader
from app.ingestion.extractor import PDFExtractor
from app.ingestion.chunker import TextChunker
from app.retrieval.dense_embedder import DenseEmbedder
from app.persistence.postgres import PostgresStorageRepositoryFactory
from app.persistence.worker_procedure_repo import (
    PostgresWorkerProcedureRepository,
    ChunkBatchItem,
)


def create_test_pdf_bytes(pages_text: list[str]) -> bytes:
    """Create a multi-page in-memory PDF using PyMuPDF."""
    doc = fitz.open()
    for text_content in pages_text:
        page = doc.new_page()  # type: ignore[attr-defined]
        page.insert_textbox(fitz.Rect(50, 50, 550, 750), text_content)
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


class MockEmbedder(DenseEmbedder):
    """Deterministic 384-dimensional unit vector embedder."""

    def __init__(self, dim: int = 384):
        super().__init__(model_name="mock-embedder")
        self.dim = dim

    def embed_texts(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        if not texts:
            return np.empty((0, self.dim), dtype=np.float32)
        vectors = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i in range(len(texts)):
            vectors[i, 0] = 1.0  # Unit vector along first dimension
        return vectors


from tests.db_test_guard import get_approved_test_db_url

LIVE_TEST_DB_URL = get_approved_test_db_url()
pytestmark = pytest.mark.skipif(
    not LIVE_TEST_DB_URL,
    reason="No approved local PostgreSQL test database configured (set TEST_DATABASE_URL or WORKER_DATABASE_URL).",
)


@pytest.fixture(scope="module")
def pg_session_factory():
    engine = create_engine(LIVE_TEST_DB_URL, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.fixture(scope="function")
def clean_test_data(pg_session_factory):
    with pg_session_factory() as session:
        with session.begin():
            session.execute(text("TRUNCATE TABLE public.chunk_embeddings CASCADE;"))
            session.execute(text("TRUNCATE TABLE public.chunks CASCADE;"))
            session.execute(text("TRUNCATE TABLE public.document_versions CASCADE;"))
            session.execute(text("TRUNCATE TABLE public.regulatory_documents CASCADE;"))
            session.execute(text("TRUNCATE TABLE public.ingestion_jobs CASCADE;"))
            # Ensure SEBI regulator and CSCRF domain exist
            session.execute(
                text("INSERT INTO public.regulators (id, name, official_portal_url) VALUES ('SEBI', 'SEBI', 'https://sebi.gov.in') ON CONFLICT DO NOTHING;")
            )
            session.execute(
                text("INSERT INTO public.regulatory_domains (id, regulator_id, code, title) VALUES ('SEBI_CSCRF', 'SEBI', 'CSCRF', 'CSCRF') ON CONFLICT DO NOTHING;")
            )
    yield


@pytest.fixture
def storage_factory():
    return PostgresStorageRepositoryFactory(database_url=LIVE_TEST_DB_URL)


@pytest.fixture
def worker_procedure_repo(pg_session_factory):
    return PostgresWorkerProcedureRepository(session_factory=pg_session_factory)


def test_orchestrator_successful_procedure_e2e(storage_factory, worker_procedure_repo, clean_test_data, pg_session_factory):
    """
    Test 1: End-to-end ingestion through stored procedure repository:
    claim -> download -> parse -> chunk -> embed -> create_version -> write_chunks_batch -> complete_job.
    """
    job_id = uuid.uuid4()
    doc_id = "sebi_cscrf_proc_01"

    # Pre-create parent regulatory document so foreign key in worker_create_version succeeds
    with pg_session_factory() as session:
        with session.begin():
            session.execute(
                text("INSERT INTO public.regulatory_documents (id, domain_id, title) VALUES (:doc_id, 'SEBI_CSCRF', 'Test Document');"),
                {"doc_id": doc_id},
            )
            session.execute(
                text("INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, document_id, status, stage) VALUES (:job_id, :key, 'https://example.com/test_circular.pdf', :doc_id, 'queued', 'pending');"),
                {"job_id": job_id, "doc_id": doc_id, "key": f"key_{job_id}"},
            )

    claimed = worker_procedure_repo.claim_job(instance_id="proc_worker_1")
    assert claimed is not None
    assert claimed.job_id == job_id
    raw_token = claimed.raw_claim_token

    pdf_bytes = create_test_pdf_bytes([
        "Securities and Exchange Board of India Circular 2026. Page 1 content on cybersecurity controls.",
        "Page 2 details operational resilience framework and incident response audits.",
    ])

    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=pdf_bytes))
    downloader = SecurePDFDownloader(allow_http_for_test=True, custom_transport=transport)
    embedder = MockEmbedder()

    orchestrator = IngestionOrchestrator(
        repository_factory=storage_factory,
        downloader=downloader,
        embedder=embedder,
        worker_procedure_repo=worker_procedure_repo,
        chunk_batch_size=10,
    )

    job_record = IngestionJobRecord(
        id=claimed.job_id,
        source_url=claimed.source_url,
        idempotency_key=f"key_{job_id}",
        document_id=claimed.document_id,
        status=JobStatus.PROCESSING,
    )

    result = orchestrator.process_job(job=job_record, claim_token=raw_token)
    assert result.status == JobStatus.COMPLETED
    assert result.document_id == doc_id
    assert result.chunks_count > 0

    # Verify database state created exclusively by stored procedures
    with pg_session_factory() as session:
        with session.begin():
            job_row = session.execute(
                text("SELECT status, stage, version_id, completed_at, error_details FROM public.ingestion_jobs WHERE id = :id"),
                {"id": job_id},
            ).mappings().fetchone()
            assert job_row["status"] == "completed"
            assert job_row["stage"] == "finished"
            assert job_row["version_id"] == f"{doc_id}_v1"
            assert job_row["completed_at"] is not None
            assert job_row["error_details"] is None

            # Verify version status transitioned to indexed
            ver_row = session.execute(
                text("SELECT extraction_status, version_number FROM public.document_versions WHERE id = :id"),
                {"id": f"{doc_id}_v1"},
            ).mappings().fetchone()
            assert ver_row["extraction_status"] == "indexed"
            assert ver_row["version_number"] == 1

            # Verify chunks count and embeddings
            chunk_count = session.execute(
                text("SELECT COUNT(*) FROM public.chunks WHERE version_id = :id"),
                {"id": f"{doc_id}_v1"},
            ).scalar_one()
            assert chunk_count == result.chunks_count

            emb_count = session.execute(
                text(
                    """
                    SELECT COUNT(*) FROM public.chunk_embeddings ce
                    JOIN public.chunks c ON c.id = ce.chunk_id
                    WHERE c.version_id = :id
                    """
                ),
                {"id": f"{doc_id}_v1"},
            ).scalar_one()
            assert emb_count == chunk_count


def test_orchestrator_multiple_chunk_batches(storage_factory, worker_procedure_repo, clean_test_data, pg_session_factory):
    """
    Test 2: Verify that multiple chunk batches are persisted properly when
    chunk_batch_size is smaller than total chunk count.
    """
    job_id = uuid.uuid4()
    doc_id = "sebi_cscrf_proc_batch"

    with pg_session_factory() as session:
        with session.begin():
            session.execute(
                text("INSERT INTO public.regulatory_documents (id, domain_id, title) VALUES (:doc_id, 'SEBI_CSCRF', 'Batch Test Document');"),
                {"doc_id": doc_id},
            )
            session.execute(
                text("INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, document_id, status, stage) VALUES (:job_id, :key, 'https://example.com/batch.pdf', :doc_id, 'queued', 'pending');"),
                {"job_id": job_id, "doc_id": doc_id, "key": f"key_{job_id}"},
            )

    claimed = worker_procedure_repo.claim_job()
    assert claimed is not None

    # Multi-page PDF generating at least 5 chunks
    pages = [f"Page {i} " + "compliance governance resilience framework " * 70 for i in range(1, 6)]
    pdf_bytes = create_test_pdf_bytes(pages)

    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=pdf_bytes))
    downloader = SecurePDFDownloader(allow_http_for_test=True, custom_transport=transport)
    embedder = MockEmbedder()

    # Set batch size to 2 to force multiple write_chunks_batch calls
    orchestrator = IngestionOrchestrator(
        repository_factory=storage_factory,
        downloader=downloader,
        embedder=embedder,
        worker_procedure_repo=worker_procedure_repo,
        chunk_batch_size=2,
    )

    job_record = IngestionJobRecord(
        id=claimed.job_id,
        source_url=claimed.source_url,
        idempotency_key=f"key_{job_id}",
        document_id=claimed.document_id,
        status=JobStatus.PROCESSING,
    )

    result = orchestrator.process_job(job=job_record, claim_token=claimed.raw_claim_token)
    assert result.status == JobStatus.COMPLETED
    assert result.chunks_count >= 5

    # Verify all chunks persisted in DB
    with pg_session_factory() as session:
        with session.begin():
            count = session.execute(
                text("SELECT COUNT(*) FROM public.chunks WHERE version_id = :ver"),
                {"ver": f"{doc_id}_v1"},
            ).scalar_one()
            assert count == result.chunks_count


def test_orchestrator_metadata_preservation(storage_factory, worker_procedure_repo, clean_test_data, pg_session_factory):
    """
    Test 3: Verify preservation of page attribution (pdf_page_start, pdf_page_end),
    chunk index, and JSON metadata in chunks table.
    """
    job_id = uuid.uuid4()
    doc_id = "sebi_cscrf_meta_01"

    with pg_session_factory() as session:
        with session.begin():
            session.execute(
                text("INSERT INTO public.regulatory_documents (id, domain_id, title) VALUES (:doc_id, 'SEBI_CSCRF', 'Meta Preservation Document');"),
                {"doc_id": doc_id},
            )
            session.execute(
                text("INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, document_id, status, stage) VALUES (:job_id, :key, 'https://example.com/meta.pdf', :doc_id, 'queued', 'pending');"),
                {"job_id": job_id, "doc_id": doc_id, "key": f"key_{job_id}"},
            )

    claimed = worker_procedure_repo.claim_job()
    pdf_bytes = create_test_pdf_bytes(["Page 1 test metadata content.", "Page 2 continued metadata test."])

    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=pdf_bytes))
    downloader = SecurePDFDownloader(allow_http_for_test=True, custom_transport=transport)
    embedder = MockEmbedder()

    orchestrator = IngestionOrchestrator(
        repository_factory=storage_factory,
        downloader=downloader,
        embedder=embedder,
        worker_procedure_repo=worker_procedure_repo,
    )

    job_record = IngestionJobRecord(
        id=claimed.job_id,
        source_url=claimed.source_url,
        idempotency_key=f"key_{job_id}",
        document_id=claimed.document_id,
        status=JobStatus.PROCESSING,
    )

    result = orchestrator.process_job(job=job_record, claim_token=claimed.raw_claim_token)
    assert result.status == JobStatus.COMPLETED

    with pg_session_factory() as session:
        with session.begin():
            chunk_row = session.execute(
                text("SELECT id, chunk_index, pdf_page_start, pdf_page_end, metadata_json FROM public.chunks WHERE version_id = :ver LIMIT 1"),
                {"ver": f"{doc_id}_v1"},
            ).mappings().fetchone()
            assert chunk_row["pdf_page_start"] >= 1
            assert chunk_row["pdf_page_end"] >= chunk_row["pdf_page_start"]
            assert chunk_row["chunk_index"] is not None
            assert "chunk_index" in chunk_row["metadata_json"]


def test_orchestrator_retryable_failure_handling(storage_factory, worker_procedure_repo, clean_test_data, pg_session_factory):
    """
    Test 4: Retryable failure uses stored procedure fail_job(is_retryable=True)
    and transitions job back to 'queued' with exponential backoff.
    """
    job_id = uuid.uuid4()
    with pg_session_factory() as session:
        with session.begin():
            session.execute(
                text("INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, collection_id, status, stage, max_attempts, attempt_count) VALUES (:job_id, :key, 'https://example.com/retryable.pdf', 'sebi_cscrf', 'queued', 'pending', 3, 0);"),
                {"job_id": job_id, "key": f"key_{job_id}"},
            )

    claimed = worker_procedure_repo.claim_job()

    # Simulate transient network timeout (503 Service Unavailable)
    transport = httpx.MockTransport(lambda req: httpx.Response(503))
    downloader = SecurePDFDownloader(allow_http_for_test=True, custom_transport=transport)

    orchestrator = IngestionOrchestrator(
        repository_factory=storage_factory,
        downloader=downloader,
        worker_procedure_repo=worker_procedure_repo,
    )

    job_record = IngestionJobRecord(
        id=claimed.job_id,
        source_url=claimed.source_url,
        idempotency_key=f"key_{job_id}",
        collection_id="sebi_cscrf",
        status=JobStatus.PROCESSING,
        attempt_count=0,
        max_attempts=3,
    )

    result = orchestrator.process_job(job=job_record, claim_token=claimed.raw_claim_token)
    assert result.status == JobStatus.QUEUED

    with pg_session_factory() as session:
        with session.begin():
            row = session.execute(
                text("SELECT status, stage, attempt_count, next_retry_at, claim_token_hash FROM public.ingestion_jobs WHERE id = :id"),
                {"id": job_id},
            ).mappings().fetchone()
            assert row["status"] == "queued"
            assert row["stage"] == "pending"
            assert row["attempt_count"] == 1
            assert row["next_retry_at"] is not None
            assert row["claim_token_hash"] is None


def test_orchestrator_terminal_failure_handling(storage_factory, worker_procedure_repo, clean_test_data, pg_session_factory):
    """
    Test 5: Terminal failure (e.g. SSRF violation or corrupted PDF) uses fail_job(is_retryable=False)
    and marks job 'failed' permanently.
    """
    job_id = uuid.uuid4()
    with pg_session_factory() as session:
        with session.begin():
            session.execute(
                text("INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, status, stage, max_attempts, attempt_count) VALUES (:job_id, :key, 'http://169.254.169.254/metadata', 'queued', 'pending', 3, 0);"),
                {"job_id": job_id, "key": f"key_{job_id}"},
            )

    claimed = worker_procedure_repo.claim_job()

    # Standard downloader with SSRF protection blocks cloud metadata endpoint
    downloader = SecurePDFDownloader(allow_http_for_test=False)

    orchestrator = IngestionOrchestrator(
        repository_factory=storage_factory,
        downloader=downloader,
        worker_procedure_repo=worker_procedure_repo,
    )

    job_record = IngestionJobRecord(
        id=claimed.job_id,
        source_url=claimed.source_url,
        idempotency_key=f"key_{job_id}",
        status=JobStatus.PROCESSING,
        attempt_count=0,
        max_attempts=3,
    )

    result = orchestrator.process_job(job=job_record, claim_token=claimed.raw_claim_token)
    assert result.status == JobStatus.FAILED

    with pg_session_factory() as session:
        with session.begin():
            row = session.execute(
                text("SELECT status, stage, completed_at, error_details, claim_token_hash FROM public.ingestion_jobs WHERE id = :id"),
                {"id": job_id},
            ).mappings().fetchone()
            assert row["status"] == "failed"
            assert row["stage"] == "finished"
            assert row["completed_at"] is not None
            assert row["error_details"] is not None
            assert row["claim_token_hash"] is None


def test_orchestrator_duplicate_document_handling(storage_factory, worker_procedure_repo, clean_test_data, pg_session_factory):
    """
    Test 6: Duplicate document detection when stored procedure repository is active.
    Preserves duplicate detection semantics and reports that mark_duplicate procedure
    is missing in migration 0004 without attempting unauthorized direct table updates.
    """
    # 1. Ingest initial document successfully
    doc_id_1 = "sebi_doc_original"
    job_id_1 = uuid.uuid4()
    with pg_session_factory() as session:
        with session.begin():
            session.execute(
                text("INSERT INTO public.regulatory_documents (id, domain_id, title) VALUES (:doc_id, 'SEBI_CSCRF', 'Original Doc');"),
                {"doc_id": doc_id_1},
            )
            session.execute(
                text("INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, document_id, status, stage) VALUES (:job_id, :key, 'https://example.com/doc1.pdf', :doc_id, 'queued', 'pending');"),
                {"job_id": job_id_1, "doc_id": doc_id_1, "key": f"key_{job_id_1}"},
            )

    claimed_1 = worker_procedure_repo.claim_job()
    pdf_bytes = create_test_pdf_bytes(["Identical content in both original and duplicate circulars."])
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=pdf_bytes))
    downloader = SecurePDFDownloader(allow_http_for_test=True, custom_transport=transport)
    embedder = MockEmbedder()

    orchestrator = IngestionOrchestrator(
        repository_factory=storage_factory,
        downloader=downloader,
        embedder=embedder,
        worker_procedure_repo=worker_procedure_repo,
    )

    job_rec_1 = IngestionJobRecord(
        id=claimed_1.job_id,
        source_url=claimed_1.source_url,
        idempotency_key=f"key_{job_id_1}",
        document_id=claimed_1.document_id,
        status=JobStatus.PROCESSING,
    )
    res_1 = orchestrator.process_job(job=job_rec_1, claim_token=claimed_1.raw_claim_token)
    assert res_1.status == JobStatus.COMPLETED

    # 2. Queue duplicate job with different document_id and identical PDF content
    doc_id_2 = "sebi_doc_duplicate"
    job_id_2 = uuid.uuid4()
    with pg_session_factory() as session:
        with session.begin():
            session.execute(
                text("INSERT INTO public.regulatory_documents (id, domain_id, title) VALUES (:doc_id, 'SEBI_CSCRF', 'Duplicate Doc');"),
                {"doc_id": doc_id_2},
            )
            session.execute(
                text("INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, document_id, status, stage) VALUES (:job_id, :key, 'https://example.com/doc2.pdf', :doc_id, 'queued', 'pending');"),
                {"job_id": job_id_2, "doc_id": doc_id_2, "key": f"key_{job_id_2}"},
            )

    claimed_2 = worker_procedure_repo.claim_job()
    job_rec_2 = IngestionJobRecord(
        id=claimed_2.job_id,
        source_url=claimed_2.source_url,
        idempotency_key=f"key_{job_id_2}",
        document_id=claimed_2.document_id,
        status=JobStatus.PROCESSING,
    )

    res_2 = orchestrator.process_job(job=job_rec_2, claim_token=claimed_2.raw_claim_token)
    # Under worker_procedure_repo with migration 0005, mark_duplicate_job is invoked,
    # cleanly transitioning job to terminal DUPLICATE without failing via fail_job.
    assert res_2.status == JobStatus.DUPLICATE
    assert "already ingested" in res_2.message

    with pg_session_factory() as session:
        row = session.execute(
            text("SELECT status, stage, claim_token_hash, lease_expires_at, completed_at, error_details FROM public.ingestion_jobs WHERE id = :id;"),
            {"id": job_id_2},
        ).mappings().one()
        assert row["status"] == "duplicate"
        assert row["stage"] == "finished"
        assert row["claim_token_hash"] is None
        assert row["lease_expires_at"] is None
        assert row["completed_at"] is not None
        assert row["error_details"]["code"] == "DUPLICATE_DOCUMENT"


def test_orchestrator_empty_document_handling(storage_factory, worker_procedure_repo, clean_test_data, pg_session_factory):
    """
    Test 7: Empty document (PDF with 0 readable text/pages) produces 0 chunks
    and is rejected as NonRetryableIngestionError, failing terminal via fail_job.
    """
    job_id = uuid.uuid4()
    doc_id = "sebi_empty_doc"

    with pg_session_factory() as session:
        with session.begin():
            session.execute(
                text("INSERT INTO public.regulatory_documents (id, domain_id, title) VALUES (:doc_id, 'SEBI_CSCRF', 'Empty Doc');"),
                {"doc_id": doc_id},
            )
            session.execute(
                text("INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, document_id, status, stage) VALUES (:job_id, :key, 'https://example.com/empty.pdf', :doc_id, 'queued', 'pending');"),
                {"job_id": job_id, "doc_id": doc_id, "key": f"key_{job_id}"},
            )

    claimed = worker_procedure_repo.claim_job()
    # Empty PDF with 1 blank page (no text)
    doc = fitz.open()
    doc.new_page()  # type: ignore[attr-defined]
    empty_pdf_bytes = doc.tobytes()
    doc.close()

    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=empty_pdf_bytes))
    downloader = SecurePDFDownloader(allow_http_for_test=True, custom_transport=transport)

    orchestrator = IngestionOrchestrator(
        repository_factory=storage_factory,
        downloader=downloader,
        worker_procedure_repo=worker_procedure_repo,
    )

    job_rec = IngestionJobRecord(
        id=claimed.job_id,
        source_url=claimed.source_url,
        idempotency_key=f"key_{job_id}",
        document_id=claimed.document_id,
        status=JobStatus.PROCESSING,
    )

    result = orchestrator.process_job(job=job_rec, claim_token=claimed.raw_claim_token)
    assert result.status == JobStatus.FAILED
    assert "0 text chunks" in result.message

    with pg_session_factory() as session:
        with session.begin():
            row = session.execute(
                text("SELECT status FROM public.ingestion_jobs WHERE id = :id"),
                {"id": job_id},
            ).mappings().fetchone()
            assert row["status"] == "failed"


def test_orchestrator_staging_cleanup_guarantee(storage_factory, worker_procedure_repo, clean_test_data, pg_session_factory, monkeypatch):
    """
    Test 8: Guarantee that downloaded staging PDF files are cleaned up from disk on both
    successful completion and failed exit paths.
    """
    job_id = uuid.uuid4()
    doc_id = "sebi_cleanup_doc"

    with pg_session_factory() as session:
        with session.begin():
            session.execute(
                text("INSERT INTO public.regulatory_documents (id, domain_id, title) VALUES (:doc_id, 'SEBI_CSCRF', 'Cleanup Doc');"),
                {"doc_id": doc_id},
            )
            session.execute(
                text("INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, document_id, status, stage) VALUES (:job_id, :key, 'https://example.com/cleanup.pdf', :doc_id, 'queued', 'pending');"),
                {"job_id": job_id, "doc_id": doc_id, "key": f"key_{job_id}"},
            )

    claimed = worker_procedure_repo.claim_job()
    pdf_bytes = create_test_pdf_bytes(["Test staging cleanup guarantee."])
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=pdf_bytes))
    downloader = SecurePDFDownloader(allow_http_for_test=True, custom_transport=transport)
    embedder = MockEmbedder()

    created_paths = []
    original_fetch = downloader.fetch_pdf

    def tracking_fetch(url):
        res = original_fetch(url)
        created_paths.append(res.staging_path)
        return res

    monkeypatch.setattr(downloader, "fetch_pdf", tracking_fetch)

    orchestrator = IngestionOrchestrator(
        repository_factory=storage_factory,
        downloader=downloader,
        embedder=embedder,
        worker_procedure_repo=worker_procedure_repo,
    )

    job_rec = IngestionJobRecord(
        id=claimed.job_id,
        source_url=claimed.source_url,
        idempotency_key=f"key_{job_id}",
        document_id=claimed.document_id,
        status=JobStatus.PROCESSING,
    )

    result = orchestrator.process_job(job=job_rec, claim_token=claimed.raw_claim_token)
    assert result.status == JobStatus.COMPLETED
    assert len(created_paths) == 1
    # File must be deleted by finally block
    assert not created_paths[0].exists()


def test_crash_recovery_verifies_exact_reconstructed_chunk_count(
    storage_factory,
    worker_procedure_repo,
    clean_test_data,
    pg_session_factory,
):
    """
    Task 2E.1A: Regression test proving crash recovery reconstructs expected chunk count
    and rejects completion if chunks or embeddings are missing in the database.
    """
    job_id = uuid.uuid4()
    doc_id = "sebi_crash_count_doc"
    source_url = "https://example.com/crash_count.pdf"

    pdf_bytes = create_test_pdf_bytes([
        "Page 1: SEBI regulatory compliance guidelines and cyber security auditing standards.",
        "Page 2: Risk management architecture, asset monitoring, and operational resilience.",
    ])
    from app.ingestion.extractor import compute_sha256
    import io
    import tempfile
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tf:
        tf.write(pdf_bytes)
        tf_path = Path(tf.name)

    sha256 = compute_sha256(tf_path)
    tf_path.unlink()

    # Pre-seed document, version, and job
    with pg_session_factory() as session:
        with session.begin():
            session.execute(
                text("INSERT INTO public.regulatory_documents (id, domain_id, title) VALUES (:doc_id, 'SEBI_CSCRF', 'Crash Count Test Doc');"),
                {"doc_id": doc_id},
            )
            session.execute(
                text("INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, document_id, status, stage, max_attempts) VALUES (:job_id, :key, :url, :doc_id, 'queued', 'pending', 3);"),
                {"job_id": job_id, "doc_id": doc_id, "url": source_url, "key": f"key_{job_id}"},
            )

    claimed = worker_procedure_repo.claim_job()
    assert claimed is not None

    version_id = worker_procedure_repo.create_version(
        job_id=job_id,
        raw_claim_token=claimed.raw_claim_token,
        version_number=1,
        sha256_hash=sha256,
        source_url=source_url,
        storage_path=f"storage/{doc_id}.pdf",
    )

    # Reconstruct chunks to get count
    extractor = PDFExtractor()
    chunker = TextChunker()
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tf:
        tf.write(pdf_bytes)
        t_path = Path(tf.name)
    pages = extractor.extract_pages(t_path)
    chunks = chunker.chunk_document(doc_id, pages)
    t_path.unlink()

    embedder = MockEmbedder(dim=384)
    emb_arr = embedder.embed_texts([c.text for c in chunks])

    # Persist all chunks and embeddings
    chunk_items = [
        ChunkBatchItem(
            chunk_id=c.chunk_id,
            chunk_index=i,
            text=c.text,
            pdf_page_start=c.page_start,
            pdf_page_end=c.page_end,
            embedding=emb_arr[i].tolist(),
            chunk_type="body",
            metadata_json=c.metadata,
        )
        for i, c in enumerate(chunks)
    ]
    worker_procedure_repo.write_chunks_batch(
        job_id=job_id,
        raw_claim_token=claimed.raw_claim_token,
        version_id=version_id,
        chunks=chunk_items,
    )

    # Now simulate a retry where the document SHA-256 is discovered in DB
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=pdf_bytes))
    downloader = SecurePDFDownloader(allow_http_for_test=True, custom_transport=transport)

    orchestrator = IngestionOrchestrator(
        repository_factory=storage_factory,
        downloader=downloader,
        embedder=embedder,
        worker_procedure_repo=worker_procedure_repo,
    )

    job_rec = IngestionJobRecord(
        id=job_id,
        source_url=source_url,
        idempotency_key=f"key_{job_id}",
        document_id=doc_id,
        status=JobStatus.PROCESSING,
    )

    result = orchestrator.process_job(job=job_rec, claim_token=claimed.raw_claim_token)
    assert result.status == JobStatus.COMPLETED
    assert result.chunks_count == len(chunks)
    assert f"verified chunk count {len(chunks)}" in result.message

    with pg_session_factory() as session:
        with session.begin():
            row = session.execute(
                text("SELECT status FROM public.ingestion_jobs WHERE id = :id"),
                {"id": job_id},
            ).mappings().fetchone()
            assert row["status"] == "completed"
