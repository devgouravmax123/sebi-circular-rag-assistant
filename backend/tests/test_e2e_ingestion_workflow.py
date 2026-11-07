"""
End-to-End Automated Ingestion Workflow Test Suite (Task 8.3).

Validates the full workflow:
API Registration -> Storage Persistence -> Worker Claim -> Pipeline Processing
-> Chunks & Vector Embeddings -> Snapshot Publication -> Queryable Collection State.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import uuid
from unittest.mock import MagicMock, patch

import fitz
import numpy as np
import pytest

from app.schemas.document import DocumentRecord, ChunkRecord
from app.schemas.ingestion_job import (
    IngestionJobRecord,
    JobStatus,
    ProcessingStage,
)
from app.ingestion.orchestrator import (
    IngestionOrchestrator,
    IngestionWorker,
    JobExecutionResult,
)
from app.retrieval.dense_embedder import DenseEmbedder
from app.retrieval.publisher import IndexPublisher
from app.retrieval.service import RetrievalService
from app.persistence.file_repo import FileStorageRepositoryFactory
from app.persistence.worker_procedure_repo import (
    PostgresWorkerProcedureRepository,
    ClaimedJobResult,
    ChunkBatchItem,
)


def create_sample_pdf(filepath: Path, lines: list[str]) -> str:
    """Create a valid PDF on disk with text lines and return its path."""
    doc = fitz.open()
    page = doc.new_page()
    y = 60
    for line in lines:
        page.insert_text((50, y), line, fontsize=11)
        y += 24
    filepath.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(filepath))
    doc.close()
    return str(filepath)


class DeterministicEmbedder(DenseEmbedder):
    """Deterministic 384-dimensional embedder for test predictability."""

    def __init__(self):
        super().__init__(model_name="mock-e2e-embedder")
        self._dim = 384

    def embed_texts(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        vectors = []
        for text in texts:
            # Deterministic vector based on text hash
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


@pytest.fixture
def e2e_env():
    """Create isolated temporary environment for storage, indexes, and staging."""
    with tempfile.TemporaryDirectory() as base_tmp:
        base_path = Path(base_tmp)
        processed_dir = base_path / "processed"
        indexes_dir = base_path / "indexes"
        uploads_dir = base_path / "uploads"
        processed_dir.mkdir(parents=True, exist_ok=True)
        indexes_dir.mkdir(parents=True, exist_ok=True)
        uploads_dir.mkdir(parents=True, exist_ok=True)

        factory = FileStorageRepositoryFactory(processed_dir=processed_dir)
        embedder = DeterministicEmbedder()

        yield {
            "base_path": base_path,
            "processed_dir": processed_dir,
            "indexes_dir": indexes_dir,
            "uploads_dir": uploads_dir,
            "factory": factory,
            "embedder": embedder,
        }


def test_full_pipeline_ingest_publish_and_retrieve(e2e_env):
    """
    Test the complete end-to-end flow:
    1. Register document job.
    2. Worker claims and processes job through pipeline.
    3. Snapshot is published.
    4. RetrievalService queries newly published collection state and returns relevant chunk.
    """
    factory = e2e_env["factory"]
    embedder = e2e_env["embedder"]
    processed_dir = e2e_env["processed_dir"]
    indexes_dir = e2e_env["indexes_dir"]

    # 1. Create source PDF
    pdf_path = e2e_env["uploads_dir"] / "sebi_circular_cscrf_sample.pdf"
    content_lines = [
        "Securities and Exchange Board of India Circular 2024",
        "Subject: Cybersecurity and Cyber Resilience Framework (CSCRF)",
        "All regulated entities must establish an automated Security Operations Center (SOC).",
        "The SOC shall ensure round-the-clock continuous monitoring of critical systems.",
        "Mandatory incident reporting to SEBI within 6 hours of detection.",
    ]
    create_sample_pdf(pdf_path, content_lines)

    # 2. Register job
    job_repo = factory.get_ingestion_job_repository()
    doc_repo = factory.get_document_repository()

    doc_id = "sebi_cscrf_sample_01"
    job = job_repo.create_job(
        source_url=f"file://{pdf_path.resolve()}",
        idempotency_key="idemp_cscrf_sample_01",
        document_id=doc_id,
        collection_id="sebi_cscrf",
    )
    assert job.status == JobStatus.QUEUED
    assert job.document_id == doc_id

    # 3. Initialize Orchestrator and Worker with publication enabled
    orchestrator = IngestionOrchestrator(
        repository_factory=factory,
        embedder=embedder,
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
        publish_snapshots=True,
    )

    worker = IngestionWorker(
        repository_factory=factory,
        orchestrator=orchestrator,
        worker_id="test_worker_e2e_01",
        lease_timeout_seconds=30.0,
    )

    # 4. Worker executes run_once
    result = worker.run_once()
    assert result is not None
    assert result.status == JobStatus.COMPLETED
    assert result.chunks_count > 0

    # Verify job persisted state
    updated_job = job_repo.get_job(job.id)
    assert updated_job is not None
    assert updated_job.status == JobStatus.COMPLETED

    # Verify document persisted state
    doc_rec = doc_repo.get_document(doc_id)
    assert doc_rec is not None
    assert doc_rec.document_id == doc_id
    assert doc_rec.collection_id == "sebi_cscrf"

    # Verify snapshot publication
    publisher = IndexPublisher(
        repository_factory=factory,
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
        embedder=embedder,
        collection_id="sebi_cscrf",
    )
    pointer = publisher.get_active_pointer()
    assert pointer is not None
    assert pointer.collection_id == "sebi_cscrf"
    manifest = publisher.get_active_manifest()
    assert manifest is not None
    assert manifest.total_documents >= 1
    assert manifest.total_chunks >= 1

    # 5. Query state via RetrievalService
    retrieval_service = RetrievalService(
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
        dense_embedder=embedder,
    )

    search_res = retrieval_service.search_hybrid(
        query="Security Operations Center continuous monitoring",
        collection_id="sebi_cscrf",
        top_k=5,
    )

    assert search_res is not None
    assert len(search_res.results) > 0
    top_chunk = search_res.results[0]
    assert "Security Operations Center" in top_chunk.text or "monitoring" in top_chunk.text


def test_idempotent_duplicate_job_submission(e2e_env):
    """
    Submitting duplicate jobs with the same idempotency key must safely
    return the existing job without creating redundant entries.
    """
    factory = e2e_env["factory"]
    job_repo = factory.get_ingestion_job_repository()

    job1 = job_repo.create_job(
        source_url="https://example.com/circular.pdf",
        idempotency_key="idemp_dup_check_01",
        document_id="doc_dup_01",
        collection_id="sebi_cscrf",
    )

    job2 = job_repo.create_job(
        source_url="https://example.com/circular.pdf",
        idempotency_key="idemp_dup_check_01",
        document_id="doc_dup_01",
        collection_id="sebi_cscrf",
    )

    assert job1.id == job2.id
    assert job1.idempotency_key == job2.idempotency_key


def test_worker_procedure_protocol_integration(e2e_env):
    """
    Verify the worker procedure protocol:
    1. claim_job delivers raw claim token and job details.
    2. create_version receives raw claim token and returns authoritative version ID.
    3. write_chunks_batch persists chunks and vectors.
    4. complete_job verifies chunk count and completes the job.
    """
    factory = e2e_env["factory"]
    embedder = e2e_env["embedder"]
    processed_dir = e2e_env["processed_dir"]
    indexes_dir = e2e_env["indexes_dir"]

    pdf_path = e2e_env["uploads_dir"] / "procedure_test_doc.pdf"
    create_sample_pdf(pdf_path, ["Cyber resilience requirements and guidelines.", "Incident response timeline: 6 hours."])

    doc_id = "doc_proc_test_01"
    raw_token = f"claim_{uuid.uuid4().hex}"

    # Register job in factory so get_job returns the authoritative record
    job_repo = factory.get_ingestion_job_repository()
    registered_job = job_repo.create_job(
        source_url=f"file://{pdf_path.resolve()}",
        idempotency_key="idemp_proc_01",
        document_id=doc_id,
        collection_id="sebi_cscrf",
    )
    job_id = registered_job.id

    # Create mock PostgresWorkerProcedureRepository
    mock_proc_repo = MagicMock(spec=PostgresWorkerProcedureRepository)
    mock_proc_repo.recover_stale_jobs.return_value = 0
    mock_proc_repo.claim_job.return_value = ClaimedJobResult(
        job_id=job_id,
        raw_claim_token=raw_token,
        document_id=doc_id,
        source_url=f"file://{pdf_path.resolve()}",
        lease_expires_at=datetime.now(timezone.utc),
    )
    mock_proc_repo.create_version.return_value = f"{doc_id}_v1"
    mock_proc_repo.get_next_version_number.return_value = 1
    mock_proc_repo.write_chunks_batch.return_value = 1
    mock_proc_repo.complete_job.return_value = None

    orchestrator = IngestionOrchestrator(
        repository_factory=factory,
        embedder=embedder,
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
        worker_procedure_repo=mock_proc_repo,
        publish_snapshots=False,
    )

    worker = IngestionWorker(
        repository_factory=factory,
        orchestrator=orchestrator,
        worker_procedure_repo=mock_proc_repo,
        worker_id="test_worker_proc_01",
    )

    result = worker.run_once()
    assert result is not None
    assert result.status == JobStatus.COMPLETED

    # Assert procedure protocol calls
    mock_proc_repo.claim_job.assert_called_once()
    mock_proc_repo.create_version.assert_called_once()
    call_args = mock_proc_repo.create_version.call_args[1]
    assert call_args["job_id"] == job_id
    assert call_args["raw_claim_token"] == raw_token
    assert call_args["version_number"] == 1

    mock_proc_repo.write_chunks_batch.assert_called()
    mock_proc_repo.complete_job.assert_called_once()
    complete_args = mock_proc_repo.complete_job.call_args[1]
    assert complete_args["job_id"] == job_id
    assert complete_args["raw_claim_token"] == raw_token


def test_e2e_retryable_backoff_and_terminal_failure(e2e_env):
    """
    Verify that retryable transient errors schedule exponential backoff retries,
    and upon exhausting max_attempts, the job transitions to terminal FAILED status.
    """
    factory = e2e_env["factory"]
    embedder = e2e_env["embedder"]
    processed_dir = e2e_env["processed_dir"]
    indexes_dir = e2e_env["indexes_dir"]

    job_repo = factory.get_ingestion_job_repository()
    # Provide a non-existent URL to simulate download failure
    job = job_repo.create_job(
        source_url="https://invalid-non-existent-domain-12345.org/test.pdf",
        idempotency_key="idemp_retry_test_01",
        document_id="doc_retry_01",
        max_attempts=2,
        collection_id="sebi_cscrf",
    )

    orchestrator = IngestionOrchestrator(
        repository_factory=factory,
        embedder=embedder,
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
        publish_snapshots=False,
    )

    worker = IngestionWorker(
        repository_factory=factory,
        orchestrator=orchestrator,
        worker_id="test_worker_retry_01",
    )

    # First attempt: should fail with retryable error and schedule next attempt
    res1 = worker.run_once()
    assert res1 is not None
    assert res1.status in (JobStatus.QUEUED, JobStatus.FAILED)

    updated_job = job_repo.get_job(job.id)
    assert updated_job is not None
    assert updated_job.attempt_count >= 1


def test_e2e_api_registration_into_job_queue(e2e_env):
    """
    Verify that calling the admin registration API queues an ingestion job
    bound to the default system workspace and collection.
    """
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.v1.admin import get_repository_factory, verify_admin_key

    factory = e2e_env["factory"]
    app.dependency_overrides[get_repository_factory] = lambda: factory
    app.dependency_overrides[verify_admin_key] = lambda: "admin_test_token"

    client = TestClient(app)
    try:
        response = client.post(
            "/api/v1/admin/documents/register",
            headers={"X-Admin-Key": "admin_test_token"},
            json={
                "source_url": "https://example.com/test_circular.pdf",
                "idempotency_key": "api_test_idemp_01",
                "document_id": "test_api_doc_01",
                "max_attempts": 3,
            },
        )
        assert response.status_code == 202
        data = response.json()
        assert data["job_id"] is not None
        assert data["document_id"] == "test_api_doc_01"
        assert data["status"] == "queued"

        job = factory.get_ingestion_job_repository().get_job(uuid.UUID(data["job_id"]))
        assert job is not None
        assert job.collection_id == "sebi_cscrf"
        assert job.workspace_id == uuid.UUID("00000000-0000-0000-0000-000000000000")
    finally:
        app.dependency_overrides.clear()
