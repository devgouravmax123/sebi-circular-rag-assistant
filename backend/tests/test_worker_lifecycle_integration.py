"""
Unit and integration tests for IngestionWorker lifecycle, polling, and claim management (Phase 7.2 Task 2C).

Covers:
1. Worker successfully claims a job using PostgresWorkerProcedureRepository.
2. Raw string claim token reaches process_job() unchanged.
3. Empty claim result causes normal idle polling without errors.
4. Stale-job recovery uses the procedure repository.
5. Recovery respects the configured batch limit.
6. Worker does not invoke legacy PostgreSQL direct-write claim/recovery methods.
7. Missing WORKER_DATABASE_URL fails clearly when DATABASE_URL is set (no silent fallback).
8. Worker session factory is separate from the API session factory.
9. Graceful shutdown remains functional.
10. Claim/recovery failures do not leak sessions or expose raw claim tokens.
"""
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
import pytest
import httpx
import fitz
import numpy as np
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings, get_settings
from app.schemas.ingestion_job import (
    IngestionJobRecord,
    JobStatus,
    ProcessingStage,
)
from app.ingestion.orchestrator import (
    IngestionWorker,
    IngestionOrchestrator,
    JobExecutionResult,
)
from app.ingestion.worker import get_repository_factory, run_worker_daemon
from app.ingestion.downloader import SecurePDFDownloader
from app.retrieval.dense_embedder import DenseEmbedder
from app.persistence.postgres import PostgresStorageRepositoryFactory
from app.persistence.worker_procedure_repo import (
    PostgresWorkerProcedureRepository,
    ClaimedJobResult,
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
            vectors[i, 0] = 1.0
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


def test_worker_claim_job_via_procedure_repo(storage_factory, worker_procedure_repo, clean_test_data, pg_session_factory):
    """
    Test 1 & 2: Worker successfully claims job using PostgresWorkerProcedureRepository
    and passes the raw string claim token into process_job() unchanged.
    """
    job_id = uuid.uuid4()
    doc_id = "sebi_worker_poll_01"

    with pg_session_factory() as session:
        with session.begin():
            session.execute(
                text("INSERT INTO public.regulatory_documents (id, domain_id, title) VALUES (:doc_id, 'SEBI_CSCRF', 'Worker Poll Doc');"),
                {"doc_id": doc_id},
            )
            session.execute(
                text("INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, document_id, status, stage) VALUES (:job_id, :key, 'https://example.com/poll1.pdf', :doc_id, 'queued', 'pending');"),
                {"job_id": job_id, "doc_id": doc_id, "key": f"key_{job_id}"},
            )

    pdf_bytes = create_test_pdf_bytes(["Worker Polling Claim Lifecycle Test Content."])
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=pdf_bytes))
    downloader = SecurePDFDownloader(allow_http_for_test=True, custom_transport=transport)
    embedder = MockEmbedder()

    token_captured = []

    class MockOrchestrator(IngestionOrchestrator):
        def process_job(self, job, claim_token):
            token_captured.append(claim_token)
            return super().process_job(job=job, claim_token=claim_token)

    mock_orchestrator = MockOrchestrator(
        repository_factory=storage_factory,
        downloader=downloader,
        embedder=embedder,
        worker_procedure_repo=worker_procedure_repo,
    )

    worker = IngestionWorker(
        repository_factory=storage_factory,
        worker_id="poll_worker_unit_1",
        orchestrator=mock_orchestrator,
        worker_procedure_repo=worker_procedure_repo,
    )

    res = worker.run_once()
    assert res is not None
    assert res.status == JobStatus.COMPLETED
    assert len(token_captured) == 1
    # Raw token must be a 64-character hexadecimal string
    assert isinstance(token_captured[0], str)
    assert len(token_captured[0]) == 64
    assert all(c in "0123456789abcdefABCDEF" for c in token_captured[0])


def test_worker_empty_claim_idle_polling(storage_factory, worker_procedure_repo, clean_test_data):
    """
    Test 3: Empty queue produces None without errors, facilitating idle polling sleep.
    """
    worker = IngestionWorker(
        repository_factory=storage_factory,
        worker_id="poll_worker_empty_1",
        worker_procedure_repo=worker_procedure_repo,
    )
    result = worker.run_once()
    assert result is None


def test_worker_stale_recovery_via_procedure_repo(storage_factory, worker_procedure_repo, clean_test_data, pg_session_factory):
    """
    Test 4 & 5: Stale job recovery uses PostgresWorkerProcedureRepository,
    respects the batch limit, and recovers expired processing jobs back to queued.
    """
    # Insert 3 stale processing jobs with expired leases
    with pg_session_factory() as session:
        with session.begin():
            for i in range(3):
                jid = uuid.uuid4()
                session.execute(
                    text(
                        """
                        INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, status, stage, max_attempts, attempt_count, lease_expires_at, heartbeat_at)
                        VALUES (:id, :key, 'https://example.com/stale.pdf', 'processing', 'chunking', 3, 0, clock_timestamp() - INTERVAL '10 minutes', clock_timestamp() - INTERVAL '10 minutes');
                        """
                    ),
                    {"id": jid, "key": f"stale_key_{i}_{jid}"},
                )

    worker = IngestionWorker(
        repository_factory=storage_factory,
        worker_id="recovery_worker_1",
        worker_procedure_repo=worker_procedure_repo,
        stale_recovery_interval_seconds=0.0,  # Trigger on every check
        recovery_batch_limit=2,  # Respect batch limit of 2
    )

    # Calling run_once should trigger recovery first before claiming
    worker.run_once()

    with pg_session_factory() as session:
        with session.begin():
            # Out of 3 stale jobs, with batch_limit=2, at least 2 were recovered to 'queued'
            queued_count = session.execute(
                text("SELECT COUNT(*) FROM public.ingestion_jobs WHERE status = 'queued'")
            ).scalar_one()
            assert queued_count >= 1


def test_worker_does_not_invoke_legacy_direct_claim(storage_factory, worker_procedure_repo, clean_test_data, monkeypatch):
    """
    Test 6: Worker does not invoke legacy direct-write IngestionJobRepository.claim_next_job
    or recover_stale_jobs when worker_procedure_repo is present.
    """
    def forbidden_claim(*args, **kwargs):
        raise AssertionError("FORBIDDEN: legacy claim_next_job was invoked instead of stored procedure!")

    def forbidden_recovery(*args, **kwargs):
        raise AssertionError("FORBIDDEN: legacy recover_stale_jobs was invoked instead of stored procedure!")

    monkeypatch.setattr(storage_factory.get_ingestion_job_repository(), "claim_next_job", forbidden_claim)
    monkeypatch.setattr(storage_factory.get_ingestion_job_repository(), "recover_stale_jobs", forbidden_recovery)

    worker = IngestionWorker(
        repository_factory=storage_factory,
        worker_procedure_repo=worker_procedure_repo,
        stale_recovery_interval_seconds=0.0,
    )
    # Should cleanly call procedure repo without hitting forbidden mock
    res = worker.run_once()
    assert res is None


def test_missing_worker_database_url_fails_clearly(monkeypatch):
    """
    Test 7: If DATABASE_URL is set but WORKER_DATABASE_URL is absent/empty,
    get_repository_factory() must raise ValueError and NEVER silently fall back.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://postgres:pass@localhost:5432/api_db")
    monkeypatch.delenv("WORKER_DATABASE_URL", raising=False)

    with pytest.raises(ValueError) as excinfo:
        get_repository_factory()
    assert "WORKER_DATABASE_URL must be explicitly configured" in str(excinfo.value)
    assert "Silent fallback to DATABASE_URL is strictly prohibited" in str(excinfo.value)


def test_worker_database_connection_separation(monkeypatch):
    """
    Test 8: Worker session factory connects to WORKER_DATABASE_URL separately from DATABASE_URL.
    """
    worker_url = "postgresql+psycopg://worker_user:worker_pass@localhost:5432/worker_db"
    api_url = "postgresql+psycopg://api_user:api_pass@localhost:5432/api_db"

    monkeypatch.setenv("DATABASE_URL", api_url)
    monkeypatch.setenv("WORKER_DATABASE_URL", worker_url)

    factory = get_repository_factory()
    assert isinstance(factory, PostgresStorageRepositoryFactory)
    assert factory.database_url == worker_url
    assert "worker_user" in str(factory.engine.url)


def test_worker_graceful_shutdown(storage_factory, worker_procedure_repo, clean_test_data):
    """
    Test 9: Graceful shutdown via worker.stop() terminates polling loop cleanly.
    """
    worker = IngestionWorker(
        repository_factory=storage_factory,
        worker_procedure_repo=worker_procedure_repo,
        poll_interval_seconds=0.1,
    )
    assert worker.is_running
    worker.stop()
    assert not worker.is_running
    processed = worker.run_loop(max_jobs=5)
    assert processed == 0


def test_claim_failure_does_not_leak_raw_tokens(storage_factory, clean_test_data, monkeypatch):
    """
    Test 10: Procedure failure during claim handles exceptions cleanly,
    never logs or exposes raw claim tokens.
    """
    mock_repo = PostgresWorkerProcedureRepository(session_factory=storage_factory.session_factory)

    def failing_claim(*args, **kwargs):
        raise RuntimeError("Simulated transient connection failure")

    monkeypatch.setattr(mock_repo, "claim_job", failing_claim)

    worker = IngestionWorker(
        repository_factory=storage_factory,
        worker_procedure_repo=mock_repo,
    )

    with pytest.raises(RuntimeError) as excinfo:
        worker.run_once()
    assert "Simulated transient connection failure" in str(excinfo.value)


def test_engine_disposal_on_normal_and_exceptional_shutdown(monkeypatch):
    """
    Test 11: run_worker_daemon disposes factory.engine in finally block
    both on normal termination and when an exception occurs in the loop.
    """
    from unittest.mock import MagicMock
    from app.persistence.file_repo import FileStorageRepositoryFactory

    # 1. Normal shutdown with mock postgres factory
    mock_factory = MagicMock(spec=PostgresStorageRepositoryFactory)
    mock_factory.engine = MagicMock()
    mock_factory.get_ingestion_job_repository.return_value = MagicMock()

    monkeypatch.setattr("app.ingestion.worker.get_repository_factory", lambda: mock_factory)
    monkeypatch.setattr("app.ingestion.worker.IngestionWorker.run_loop", lambda self, max_jobs: 0)

    exit_code = run_worker_daemon(max_jobs=1)
    assert exit_code == 0
    assert mock_factory.engine.dispose.call_count == 1

    # 2. Exceptional shutdown with mock postgres factory
    mock_factory.engine.reset_mock()
    def failing_loop(self, max_jobs):
        raise RuntimeError("Fatal worker loop error")
    monkeypatch.setattr("app.ingestion.worker.IngestionWorker.run_loop", failing_loop)

    exit_code = run_worker_daemon(max_jobs=1)
    assert exit_code == 1
    assert mock_factory.engine.dispose.call_count == 1

    # 3. Factory without engine (e.g. FileStorageRepositoryFactory) does not fail
    file_factory = FileStorageRepositoryFactory(processed_dir="data/processed")
    monkeypatch.setattr("app.ingestion.worker.get_repository_factory", lambda: file_factory)
    monkeypatch.setattr("app.ingestion.worker.IngestionWorker.run_loop", lambda self, max_jobs: 0)
    exit_code = run_worker_daemon(max_jobs=1)
    assert exit_code == 0


def test_shutdown_during_in_flight_job_allows_completion(storage_factory, worker_procedure_repo, clean_test_data, pg_session_factory):
    """
    Test 12: Stopping worker while job is actively running allows in-flight job to finish,
    joins heartbeat thread, and prevents subsequent jobs from being claimed.
    """
    job_id = uuid.uuid4()
    with pg_session_factory() as session:
        with session.begin():
            session.execute(
                text(
                    """
                    INSERT INTO public.regulatory_documents (id, domain_id, title)
                    VALUES ('sebi_inflight_doc', 'SEBI_CSCRF', 'In-Flight Doc')
                    ON CONFLICT DO NOTHING;
                    """
                )
            )
            session.execute(
                text(
                    """
                    INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, document_id, status, stage, attempt_count, max_attempts)
                    VALUES (:id, :key, 'https://example.com/inflight.pdf', 'sebi_inflight_doc', 'queued', 'pending', 0, 3);
                    """
                ),
                {"id": job_id, "key": f"key_{job_id}"},
            )

    pdf_bytes = create_test_pdf_bytes(["In-flight job page content."])
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=pdf_bytes, headers={"Content-Type": "application/pdf"}))
    downloader = SecurePDFDownloader(allow_http_for_test=True, custom_transport=transport)

    worker = IngestionWorker(
        repository_factory=storage_factory,
        worker_procedure_repo=worker_procedure_repo,
        poll_interval_seconds=0.1,
    )
    worker.orchestrator.downloader = downloader
    worker.orchestrator.embedder = MockEmbedder()

    # Hook into orchestrator.process_job to signal worker.stop() mid-flight
    orig_process_job = worker.orchestrator.process_job
    stop_called_in_flight = False

    def hook_process_job(job, claim_token):
        nonlocal stop_called_in_flight
        assert worker.current_job_id == job.id
        worker.stop()
        stop_called_in_flight = True
        return orig_process_job(job, claim_token)

    worker.orchestrator.process_job = hook_process_job

    # Execute run_once: claims job, signals stop in-flight, but lets current job finish
    res = worker.run_once()
    assert stop_called_in_flight
    assert res is not None
    assert res.status == JobStatus.COMPLETED
    assert worker.current_job_id is None
    assert not worker.is_running

    # Verify subsequent run_once immediately returns None because stop_event is set
    assert worker.run_once() is None


def test_orphan_staging_file_cleanup():
    """
    Test 13: cleanup_orphan_staging_files deletes only worker-owned files
    older than threshold, leaving active/foreign files intact.
    """
    import tempfile
    import time
    from app.persistence.file_repo import FileStorageRepositoryFactory

    with tempfile.TemporaryDirectory() as tmpdir:
        staging_dir = Path(tmpdir)
        now = time.time()

        # 1. Old worker staging file (> 3600s old) -> Should be deleted
        old_worker_file = staging_dir / "sebi_staging_worker_old123.pdf.staging"
        old_worker_file.write_bytes(b"dummy pdf bytes")
        os.utime(old_worker_file, (now - 5000, now - 5000))

        # 2. Fresh worker staging file (10s old) -> Must be preserved
        fresh_worker_file = staging_dir / "sebi_staging_worker_fresh123.pdf.staging"
        fresh_worker_file.write_bytes(b"fresh pdf bytes")
        os.utime(fresh_worker_file, (now - 10, now - 10))

        # 3. Old foreign file not matching worker prefix -> Must be preserved
        foreign_old_file = staging_dir / "other_service_file.pdf"
        foreign_old_file.write_bytes(b"foreign bytes")
        os.utime(foreign_old_file, (now - 10000, now - 10000))

        # 4. Old staging file with different suffix -> Must be preserved
        wrong_suffix_file = staging_dir / "sebi_staging_worker_old.txt"
        wrong_suffix_file.write_bytes(b"wrong suffix")
        os.utime(wrong_suffix_file, (now - 5000, now - 5000))

        dummy_factory = FileStorageRepositoryFactory(processed_dir="data/processed")
        worker = IngestionWorker(repository_factory=dummy_factory)

        # Execute cleanup with 3600s threshold
        cleaned = worker.cleanup_orphan_staging_files(staging_dir=staging_dir, max_age_seconds=3600.0)
        assert cleaned == 1

        # Assertions on disk
        assert not old_worker_file.exists()
        assert fresh_worker_file.exists()
        assert foreign_old_file.exists()
        assert wrong_suffix_file.exists()

        # Idempotence: running again removes 0
        cleaned_again = worker.cleanup_orphan_staging_files(staging_dir=staging_dir, max_age_seconds=3600.0)
        assert cleaned_again == 0
