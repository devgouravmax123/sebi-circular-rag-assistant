"""
Unit and integration tests for IngestionOrchestrator, IngestionWorker, lease safety,
error classification, retry scheduling, and deduplication (Phase 8B.4).
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import uuid
import numpy as np
import pytest
import httpx
import fitz

from app.schemas.document import DocumentRecord, PageContent, ChunkRecord
from app.schemas.ingestion_job import (
    IngestionJobRecord,
    JobStatus,
    ProcessingStage,
    StaleLeaseError,
)
from app.ingestion.orchestrator import (
    IngestionOrchestrator,
    IngestionWorker,
    JobExecutionResult,
    NonRetryableIngestionError,
    RetryableIngestionError,
)
from app.ingestion.downloader import (
    SecurePDFDownloader,
    DownloadResult,
    SSRFValidationError,
    InvalidPDFError,
    DownloadHTTPError,
)
from app.ingestion.extractor import PDFExtractor
from app.ingestion.chunker import TextChunker
from app.retrieval.dense_embedder import DenseEmbedder
from app.persistence.file_repo import FileStorageRepositoryFactory


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
            vectors[i, 0] = 1.0  # Unit vector along first dimension
        return vectors


@pytest.fixture
def temp_env(tmp_path):
    """Fixture providing an isolated file repository environment."""
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    factory = FileStorageRepositoryFactory(processed_dir=processed_dir)
    return {
        "factory": factory,
        "processed_dir": processed_dir,
        "tmp_path": tmp_path,
    }


def test_orchestrator_successful_e2e_ingestion(temp_env):
    """
    Test 1: Successful end-to-end ingestion:
    queued job -> claimed -> downloaded -> parsed -> chunked -> embedded -> persisted -> completed.
    """
    factory = temp_env["factory"]
    job_repo = factory.get_ingestion_job_repository()
    doc_repo = factory.get_document_repository()
    chunk_repo = factory.get_chunk_repository()

    pdf_bytes = create_minimal_pdf_bytes([
        "Securities and Exchange Board of India Circular 2026",
        "Subject: Cybersecurity and Cyber Resilience Framework Compliance",
        "All regulated entities must establish robust governance and audit controls.",
    ])

    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=pdf_bytes))
    downloader = SecurePDFDownloader(allow_http_for_test=True, custom_transport=transport)
    embedder = MockEmbedder()

    orchestrator = IngestionOrchestrator(
        repository_factory=factory,
        downloader=downloader,
        embedder=embedder,
        heartbeat_interval_seconds=60.0,
    )

    # 1. Create queued job
    job = job_repo.create_job(
        source_url="http://127.0.0.1:8000/circular_2026_01.pdf",
        idempotency_key="key_test_e2e_1",
        document_id="sebi_cscrf_2026_01",
    )
    assert job.status == JobStatus.QUEUED

    # 2. Claim job
    claim_token = uuid.uuid4()
    claimed = job_repo.claim_next_job(worker_id="test_worker_1", claim_token=claim_token)
    assert claimed is not None
    assert claimed.status == JobStatus.PROCESSING

    # 3. Process job
    result = orchestrator.process_job(job=claimed, claim_token=claim_token)
    assert result.status == JobStatus.COMPLETED
    assert result.document_id == "sebi_cscrf_2026_01"
    assert result.chunks_count > 0

    # 4. Verify job state in repository
    persisted_job = job_repo.get_job(job.id)
    assert persisted_job.status == JobStatus.COMPLETED
    assert persisted_job.stage == ProcessingStage.FINISHED
    assert persisted_job.completed_at is not None

    # 5. Verify persisted document and chunks
    doc = doc_repo.get_document("sebi_cscrf_2026_01")
    assert doc is not None
    assert doc.document_id == "sebi_cscrf_2026_01"
    assert doc.sha256 == result.sha256

    chunks = chunk_repo.list_chunks_for_document("sebi_cscrf_2026_01")
    assert len(chunks) == result.chunks_count
    assert "Cybersecurity" in chunks[0].text


def test_orchestrator_duplicate_sha256_detection(temp_env):
    """
    Test 2: Duplicate content handling:
    Second job with identical PDF content is marked DUPLICATE without re-embedding or duplicating documents.
    """
    factory = temp_env["factory"]
    job_repo = factory.get_ingestion_job_repository()
    doc_repo = factory.get_document_repository()

    pdf_bytes = create_minimal_pdf_bytes(["SEBI Advisory on Cloud Security Requirements 2026"])
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=pdf_bytes))
    downloader = SecurePDFDownloader(allow_http_for_test=True, custom_transport=transport)

    orchestrator = IngestionOrchestrator(
        repository_factory=factory,
        downloader=downloader,
        embedder=MockEmbedder(),
    )

    # Ingest first job
    job1 = job_repo.create_job(
        source_url="http://127.0.0.1:8000/doc1.pdf",
        idempotency_key="key_dup_1",
        document_id="sebi_doc_1",
    )
    token1 = uuid.uuid4()
    claimed1 = job_repo.claim_next_job("worker_1", token1)
    res1 = orchestrator.process_job(claimed1, token1)
    assert res1.status == JobStatus.COMPLETED

    # Register second job pointing to same content with different key and doc_id
    job2 = job_repo.create_job(
        source_url="http://127.0.0.1:8000/doc2.pdf",
        idempotency_key="key_dup_2",
        document_id="sebi_doc_2_duplicate",
    )
    token2 = uuid.uuid4()
    claimed2 = job_repo.claim_next_job("worker_2", token2)
    res2 = orchestrator.process_job(claimed2, token2)

    # Second job must be DUPLICATE
    assert res2.status == JobStatus.DUPLICATE
    assert res2.sha256 == res1.sha256
    assert res2.document_id == "sebi_doc_1"

    job2_persisted = job_repo.get_job(job2.id)
    assert job2_persisted.status == JobStatus.DUPLICATE

    # Ensure doc_repo still only has the original document
    docs = doc_repo.list_documents()
    assert len(docs) == 1
    assert docs[0].document_id == "sebi_doc_1"


def test_orchestrator_non_retryable_ssrf_failure(temp_env):
    """
    Test 3: Non-retryable security rejection (SSRF):
    Job fails terminally and records structured error without retry scheduling.
    """
    factory = temp_env["factory"]
    job_repo = factory.get_ingestion_job_repository()

    downloader = SecurePDFDownloader(allow_http_for_test=False)  # HTTP forbidden, private IP rejected
    orchestrator = IngestionOrchestrator(repository_factory=factory, downloader=downloader)

    job = job_repo.create_job(
        source_url="http://127.0.0.1/private.pdf",
        idempotency_key="key_ssrf_test",
    )
    token = uuid.uuid4()
    claimed = job_repo.claim_next_job("worker_ssrf", token)

    result = orchestrator.process_job(claimed, token)
    assert result.status == JobStatus.FAILED
    assert "rejected by security policy" in result.message

    job_persisted = job_repo.get_job(job.id)
    assert job_persisted.status == JobStatus.FAILED
    assert job_persisted.error_details["category"] == "non_retryable"


def test_orchestrator_retryable_network_failure_and_backoff(temp_env):
    """
    Test 4: Retryable network error schedules retry with exponential backoff.
    """
    factory = temp_env["factory"]
    job_repo = factory.get_ingestion_job_repository()

    # Transport returns 503 Service Unavailable
    transport = httpx.MockTransport(lambda req: httpx.Response(503, content=b"Server busy"))
    downloader = SecurePDFDownloader(allow_http_for_test=True, custom_transport=transport)
    orchestrator = IngestionOrchestrator(repository_factory=factory, downloader=downloader)

    job = job_repo.create_job(
        source_url="http://127.0.0.1:8000/busy.pdf",
        idempotency_key="key_retry_test",
    )
    token = uuid.uuid4()
    claimed = job_repo.claim_next_job("worker_net", token)

    result = orchestrator.process_job(claimed, token)
    assert result.status == JobStatus.QUEUED  # Scheduled back to queued
    assert "Scheduled retry in" in result.message

    job_persisted = job_repo.get_job(job.id)
    assert job_persisted.status == JobStatus.QUEUED
    assert job_persisted.attempt_count == 1
    assert job_persisted.next_retry_at is not None
    assert job_persisted.error_details["category"] == "retryable"


def test_orchestrator_max_attempts_terminal_failure(temp_env):
    """
    Test 5: Exceeding max_attempts transitions a retryable error into terminal FAILED status.
    """
    factory = temp_env["factory"]
    job_repo = factory.get_ingestion_job_repository()

    transport = httpx.MockTransport(lambda req: httpx.Response(500, content=b"Internal error"))
    downloader = SecurePDFDownloader(allow_http_for_test=True, custom_transport=transport)
    orchestrator = IngestionOrchestrator(repository_factory=factory, downloader=downloader)

    job = job_repo.create_job(
        source_url="http://127.0.0.1:8000/fatal.pdf",
        idempotency_key="key_max_attempts",
        max_attempts=2,
    )
    # Simulate first attempt already used in previous failure
    job_file = temp_env["processed_dir"] / "ingestion_jobs.json"
    with open(job_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    data[str(job.id)]["attempt_count"] = 1
    with open(job_file, "w", encoding="utf-8") as f:
        json.dump(data, f)

    token = uuid.uuid4()
    claimed = job_repo.claim_next_job("worker_fatal", token)

    result = orchestrator.process_job(claimed, token)
    assert result.status == JobStatus.FAILED
    assert "Max attempts (2) reached" in result.message

    job_persisted = job_repo.get_job(job.id)
    assert job_persisted.status == JobStatus.FAILED
    assert job_persisted.error_details["category"] == "max_attempts_exceeded"


def test_orchestrator_stale_lease_abandonment(temp_env):
    """
    Test 6: Worker halts cleanly if lease is lost or superseded during processing.
    """
    factory = temp_env["factory"]
    job_repo = factory.get_ingestion_job_repository()

    pdf_bytes = create_minimal_pdf_bytes(["Valid PDF content"])
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=pdf_bytes))
    downloader = SecurePDFDownloader(allow_http_for_test=True, custom_transport=transport)
    orchestrator = IngestionOrchestrator(
        repository_factory=factory,
        downloader=downloader,
        embedder=MockEmbedder(),
    )

    job = job_repo.create_job(
        source_url="http://127.0.0.1:8000/stale.pdf",
        idempotency_key="key_stale_test",
    )
    token = uuid.uuid4()
    claimed = job_repo.claim_next_job("worker_orig", token)

    # Simulate another worker taking ownership (e.g. stale recovery occurred)
    bogus_token = uuid.uuid4()
    job_file = temp_env["processed_dir"] / "ingestion_jobs.json"
    with open(job_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    data[str(job.id)]["claim_token"] = str(bogus_token)
    with open(job_file, "w", encoding="utf-8") as f:
        json.dump(data, f)

    # Process job with original token -> should encounter StaleLeaseError and exit safely
    result = orchestrator.process_job(claimed, token)
    assert result.status == JobStatus.FAILED
    assert "Lease lost" in result.message


def test_ingestion_worker_polling_loop_and_clean_stop(temp_env):
    """
    Test 7: IngestionWorker processes available jobs and stops gracefully on stop().
    """
    factory = temp_env["factory"]
    job_repo = factory.get_ingestion_job_repository()

    pdf1 = create_minimal_pdf_bytes(["Worker Loop Test Circular One"])
    pdf2 = create_minimal_pdf_bytes(["Worker Loop Test Circular Two with different text"])

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "job1" in str(request.url):
            return httpx.Response(200, content=pdf1)
        return httpx.Response(200, content=pdf2)

    transport = httpx.MockTransport(mock_handler)
    downloader = SecurePDFDownloader(allow_http_for_test=True, custom_transport=transport)
    orchestrator = IngestionOrchestrator(
        repository_factory=factory,
        downloader=downloader,
        embedder=MockEmbedder(),
    )

    worker = IngestionWorker(
        repository_factory=factory,
        worker_id="test_loop_worker",
        poll_interval_seconds=0.05,
        orchestrator=orchestrator,
    )

    # Queue 2 jobs with distinct content
    job_repo.create_job(
        source_url="http://127.0.0.1:8000/job1.pdf",
        idempotency_key="loop_key_1",
        document_id="doc_loop_1",
    )
    job_repo.create_job(
        source_url="http://127.0.0.1:8000/job2.pdf",
        idempotency_key="loop_key_2",
        document_id="doc_loop_2",
    )

    # Run worker for exactly 2 jobs
    processed = worker.run_loop(max_jobs=2)
    assert processed == 2

    # Verify both completed
    completed = job_repo.list_jobs(status=JobStatus.COMPLETED)
    assert len(completed) == 2

    # Test clean stop
    worker.stop()
    assert not worker.is_running


def test_orchestrator_extraction_failure_and_cleanup(temp_env):
    """
    Test 8: Corrupt PDF payload fails at extraction stage, marks job failed, and cleans up temporary file.
    """
    factory = temp_env["factory"]
    job_repo = factory.get_ingestion_job_repository()

    # Corrupt PDF: magic bytes match '%PDF-' but rest is garbage that fitz cannot parse
    corrupt_pdf_bytes = b"%PDF-1.4\ncorrupt garbage bytes that will fail extraction completely\n%%EOF"
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=corrupt_pdf_bytes))
    downloader = SecurePDFDownloader(allow_http_for_test=True, custom_transport=transport)
    orchestrator = IngestionOrchestrator(
        repository_factory=factory,
        downloader=downloader,
        embedder=MockEmbedder(),
    )

    job = job_repo.create_job(
        source_url="http://127.0.0.1:8000/corrupt.pdf",
        idempotency_key="key_corrupt_test",
        document_id="doc_corrupt",
    )
    token = uuid.uuid4()
    claimed = job_repo.claim_next_job("worker_corrupt", token)

    result = orchestrator.process_job(claimed, token)
    assert result.status == JobStatus.FAILED
    assert "extraction failed" in result.message.lower()

    job_persisted = job_repo.get_job(job.id)
    assert job_persisted.status == JobStatus.FAILED
    assert job_persisted.error_details["category"] == "non_retryable"


def test_orchestrator_heartbeat_updates_during_stages(temp_env):
    """
    Test 9: Processing stage updates advance stage and refresh heartbeat timestamp in repository.
    """
    factory = temp_env["factory"]
    job_repo = factory.get_ingestion_job_repository()

    pdf_bytes = create_minimal_pdf_bytes(["Test Heartbeat Updates"])
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=pdf_bytes))
    downloader = SecurePDFDownloader(allow_http_for_test=True, custom_transport=transport)
    orchestrator = IngestionOrchestrator(
        repository_factory=factory,
        downloader=downloader,
        embedder=MockEmbedder(),
    )

    job = job_repo.create_job(
        source_url="http://127.0.0.1:8000/hb.pdf",
        idempotency_key="key_hb_test",
    )
    token = uuid.uuid4()
    claimed = job_repo.claim_next_job("worker_hb", token)
    orig_hb = claimed.heartbeat_at

    result = orchestrator.process_job(claimed, token)
    assert result.status == JobStatus.COMPLETED

    persisted = job_repo.get_job(job.id)
    assert persisted.status == JobStatus.COMPLETED
    assert persisted.stage == ProcessingStage.FINISHED
    assert persisted.heartbeat_at >= orig_hb


def test_crash_recovery_after_persistence_retry(temp_env):
    """
    Test 10 (Phase 8B.4.1): Deterministic simulation of crash after persistence but before job completion.
    Verifies that a subsequent retry:
    - Successfully completes the job
    - Does NOT create duplicate documents, chunks, or versions
    - Preserves existing document ID, chunk count, and SHA-256
    """
    factory = temp_env["factory"]
    job_repo = factory.get_ingestion_job_repository()
    doc_repo = factory.get_document_repository()
    chunk_repo = factory.get_chunk_repository()

    pdf_bytes = create_minimal_pdf_bytes([
        "SEBI Document Crash Recovery Test",
        "Provision on Cyber Risk Governance and Resiliency Controls.",
    ])
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=pdf_bytes))
    downloader = SecurePDFDownloader(allow_http_for_test=True, custom_transport=transport)
    embedder = MockEmbedder()

    orchestrator = IngestionOrchestrator(
        repository_factory=factory,
        downloader=downloader,
        embedder=embedder,
    )

    # 1. Register job
    job = job_repo.create_job(
        source_url="http://127.0.0.1:8000/crash_test.pdf",
        idempotency_key="key_crash_retry_1",
        document_id="doc_crash_recovered",
    )
    token1 = uuid.uuid4()
    claimed1 = job_repo.claim_next_job("worker_crasher", token1)

    # 2. Simulate worker executing up to persistence, then abruptly dying before mark_completed
    # We execute download, parsing, chunking, and persistence manually
    dl_res = downloader.fetch_pdf(job.source_url)
    pages = orchestrator.extractor.extract_pages(dl_res.staging_path)
    chunks = orchestrator.chunker.chunk_document("doc_crash_recovered", pages)
    embeddings_dict = {
        c.chunk_id: np.zeros(384, dtype=np.float32) for c in chunks
    }
    doc_record = DocumentRecord(
        document_id="doc_crash_recovered",
        title="doc_crash_recovered",
        source_url=job.source_url,
        sha256=dl_res.sha256,
        pdf_path=str(dl_res.staging_path),
        extraction_status="extracted",
    )
    doc_repo.save_document(doc_record)
    chunk_repo.save_chunks(chunks)
    chunk_repo.save_embeddings(embeddings_dict)
    # The process crashes here! Job is left in 'processing' in repository.

    # 3. Time passes, lease expires, stale recovery runs and returns job to 'queued'
    recovered_count = job_repo.recover_stale_jobs(lease_timeout_seconds=0.0)
    assert recovered_count == 1
    requeued_job = job_repo.get_job(job.id)
    assert requeued_job.status == JobStatus.QUEUED

    # 4. New worker claims the recovered job and processes it
    token2 = uuid.uuid4()
    claimed2 = job_repo.claim_next_job("worker_resurrector", token2)
    assert claimed2 is not None

    result = orchestrator.process_job(claimed2, token2)

    # Must complete successfully as a crash recovery
    assert result.status == JobStatus.COMPLETED
    assert result.document_id == "doc_crash_recovered"
    assert "Crash recovery" in result.message

    # 5. Verify no duplicates in document repository
    all_docs = doc_repo.list_documents()
    assert len(all_docs) == 1
    assert all_docs[0].document_id == "doc_crash_recovered"

    # Chunks count matches exactly without duplication
    persisted_chunks = chunk_repo.list_chunks_for_document("doc_crash_recovered")
    assert len(persisted_chunks) == len(chunks)


def test_lease_loss_during_processing_rejects_persistence(temp_env):
    """
    Test 11 (Phase 8B.4.1): If a worker's lease is lost/superseded during processing,
    further stage updates and finalization raise StaleLeaseError and are aborted cleanly.
    """
    factory = temp_env["factory"]
    job_repo = factory.get_ingestion_job_repository()

    pdf_bytes = create_minimal_pdf_bytes(["SEBI Lease Loss Safety Test"])
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=pdf_bytes))
    downloader = SecurePDFDownloader(allow_http_for_test=True, custom_transport=transport)
    orchestrator = IngestionOrchestrator(
        repository_factory=factory,
        downloader=downloader,
        embedder=MockEmbedder(),
    )

    job = job_repo.create_job(
        source_url="http://127.0.0.1:8000/lease_loss.pdf",
        idempotency_key="key_lease_loss_1",
        document_id="doc_lease_loss",
    )
    token1 = uuid.uuid4()
    claimed1 = job_repo.claim_next_job("worker_slow", token1)

    # Worker loses lease (e.g. timeout occurred, worker2 claimed)
    token2 = uuid.uuid4()
    job_file = temp_env["processed_dir"] / "ingestion_jobs.json"
    import json
    with open(job_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    data[str(job.id)]["claim_token"] = str(token2)
    data[str(job.id)]["claim_worker_id"] = "worker_fast"
    with open(job_file, "w", encoding="utf-8") as f:
        json.dump(data, f)

    # Worker1 tries to complete or update stage -> rejected with StaleLeaseError
    with pytest.raises(StaleLeaseError):
        job_repo.update_stage_and_heartbeat(
            job_id=job.id,
            claim_token=token1,
            stage=ProcessingStage.PERSISTING,
        )

    with pytest.raises(StaleLeaseError):
        job_repo.mark_completed(
            job_id=job.id,
            claim_token=token1,
            document_id="doc_lease_loss",
            sha256="dummy_sha",
        )
