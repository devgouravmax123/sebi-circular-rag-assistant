"""
Tests for PostgresWorkerProcedureRepository (Phase 7.2 Task 2A).

Tests all seven worker stored procedures via the repository adapter against live PostgreSQL:
1. claim_job returns valid UUID, 64-char hex token, and lease expiry.
2. heartbeat accepts valid token and extends lease.
3. heartbeat rejects invalid token (UNAUTHORIZED_WORKER).
4. create_version creates version under valid claim.
5. write_chunks_batch persists chunks and 384-d embeddings.
6. write_chunks_batch rejects vector dimension mismatch.
7. complete_job succeeds when expected chunk count matches.
8. complete_job rejects mismatched chunk count.
9. fail_job handles retryable (queued) and terminal (failed) transitions.
10. recover_stale_jobs recovers expired processing jobs.
"""
import os
import uuid
from datetime import datetime, date, timezone
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.persistence.worker_procedure_repo import (
    PostgresWorkerProcedureRepository,
    ChunkBatchItem,
    ClaimedJobResult,
)
from app.persistence.interfaces import PersistenceError, ConnectionStorageError


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
                text(
                    """
                    INSERT INTO public.regulators (id, name, official_portal_url)
                    VALUES ('SEBI', 'SEBI', 'https://sebi.gov.in') ON CONFLICT DO NOTHING;
                    INSERT INTO public.regulatory_domains (id, regulator_id, code, title)
                    VALUES ('SEBI_CSCRF', 'SEBI', 'CSCRF', 'CSCRF') ON CONFLICT DO NOTHING;
                    """
                )
            )
    yield


@pytest.fixture
def repo(pg_session_factory):
    return PostgresWorkerProcedureRepository(session_factory=pg_session_factory)


def test_claim_job_success_and_empty(repo, clean_test_data, pg_session_factory):
    # Queue is initially empty
    empty_claim = repo.claim_job(instance_id="worker_test_1")
    assert empty_claim is None

    # Insert a queued job
    job_id = uuid.uuid4()
    with pg_session_factory() as session:
        with session.begin():
            session.execute(
                text(
                    """
                    INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, status, stage)
                    VALUES (:id, :key, 'https://example.com/circular.pdf', 'queued', 'pending');
                    """
                ),
                {"id": job_id, "key": f"key_{job_id}"},
            )

    claimed = repo.claim_job(instance_id="worker_node_alpha", lease_duration_seconds=300)
    assert claimed is not None
    assert claimed.job_id == job_id
    assert isinstance(claimed.raw_claim_token, str)
    assert len(claimed.raw_claim_token) == 64  # 32 bytes hex encoded
    assert claimed.lease_expires_at > datetime.now(timezone.utc)

    # Verify repr masks raw token
    assert "[REDACTED]" in repr(claimed)
    assert claimed.raw_claim_token not in repr(claimed)


def test_heartbeat_success_and_rejection(repo, clean_test_data, pg_session_factory):
    job_id = uuid.uuid4()
    with pg_session_factory() as session:
        with session.begin():
            session.execute(
                text(
                    """
                    INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, status, stage)
                    VALUES (:id, :key, 'https://example.com/circular.pdf', 'queued', 'pending');
                    """
                ),
                {"id": job_id, "key": f"key_{job_id}"},
            )

    claimed = repo.claim_job(instance_id="worker_hb_test")
    assert claimed is not None

    # Valid heartbeat
    new_expiry = repo.heartbeat(
        job_id=claimed.job_id,
        raw_claim_token=claimed.raw_claim_token,
        stage="parsing",
        lease_duration_seconds=400,
    )
    assert new_expiry > claimed.lease_expires_at

    # Invalid token rejected
    with pytest.raises(Exception) as exc_info:
        repo.heartbeat(
            job_id=claimed.job_id,
            raw_claim_token="invalid_token_1234567890abcdef1234567890abcdef1234567890abcdef12345678",
            stage="parsing",
        )
    assert "UNAUTHORIZED_WORKER" in str(exc_info.value)


def test_create_version_and_write_chunks_batch(repo, clean_test_data, pg_session_factory):
    job_id = uuid.uuid4()
    with pg_session_factory() as session:
        with session.begin():
            session.execute(
                text(
                    """
                    INSERT INTO public.regulatory_documents (id, domain_id, title)
                    VALUES ('doc_repo_test', 'SEBI_CSCRF', 'Repo Test Circular') ON CONFLICT DO NOTHING;
                    """
                )
            )
            session.execute(
                text(
                    """
                    INSERT INTO public.ingestion_jobs (id, idempotency_key, document_id, source_url, status, stage)
                    VALUES (:id, :key, 'doc_repo_test', 'https://example.com/test.pdf', 'queued', 'pending');
                    """
                ),
                {"id": job_id, "key": f"key_{job_id}"},
            )

    claimed = repo.claim_job(instance_id="worker_batch_test")
    assert claimed is not None

    # Create document version
    version_id = repo.create_version(
        job_id=claimed.job_id,
        raw_claim_token=claimed.raw_claim_token,
        version_number=1,
        sha256_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        source_url="https://example.com/test.pdf",
        storage_path="/data/storage/test.pdf",
        issue_date=date(2026, 1, 15),
    )
    assert version_id == "doc_repo_test_v1"

    # Write chunks batch
    emb_zero = [0.0] * 384
    batch = [
        ChunkBatchItem(
            chunk_id="chunk_repo_1",
            chunk_index=0,
            text="First paragraph of regulatory circular.",
            pdf_page_start=1,
            pdf_page_end=1,
            embedding=emb_zero,
            metadata_json={"section": "definitions"},
        ),
        ChunkBatchItem(
            chunk_id="chunk_repo_2",
            chunk_index=1,
            text="Second paragraph of regulatory circular.",
            pdf_page_start=1,
            pdf_page_end=2,
            embedding=emb_zero,
            metadata_json={"section": "compliance"},
        ),
    ]

    persisted = repo.write_chunks_batch(
        job_id=claimed.job_id,
        raw_claim_token=claimed.raw_claim_token,
        version_id=version_id,
        chunks=batch,
    )
    assert persisted == 2

    # Vector dimension mismatch rejected client-side before SQL execution
    bad_batch = [
        ChunkBatchItem(
            chunk_id="chunk_bad_dim",
            chunk_index=2,
            text="Invalid vector length",
            pdf_page_start=2,
            pdf_page_end=2,
            embedding=[0.1, 0.2],  # only 2 dimensions
        )
    ]
    with pytest.raises(ValueError) as dim_exc:
        repo.write_chunks_batch(
            job_id=claimed.job_id,
            raw_claim_token=claimed.raw_claim_token,
            version_id=version_id,
            chunks=bad_batch,
        )
    assert "Vector dimension mismatch" in str(dim_exc.value)


def test_complete_job_success_and_mismatch(repo, clean_test_data, pg_session_factory):
    job_id = uuid.uuid4()
    with pg_session_factory() as session:
        with session.begin():
            session.execute(
                text(
                    """
                    INSERT INTO public.regulatory_documents (id, domain_id, title)
                    VALUES ('doc_comp_repo', 'SEBI_CSCRF', 'Completion Repo Test') ON CONFLICT DO NOTHING;
                    """
                )
            )
            session.execute(
                text(
                    """
                    INSERT INTO public.ingestion_jobs (id, idempotency_key, document_id, source_url, status, stage)
                    VALUES (:id, :key, 'doc_comp_repo', 'https://example.com/test.pdf', 'queued', 'pending');
                    """
                ),
                {"id": job_id, "key": f"key_{job_id}"},
            )

    claimed = repo.claim_job(instance_id="worker_comp")
    assert claimed is not None

    version_id = repo.create_version(
        job_id=claimed.job_id,
        raw_claim_token=claimed.raw_claim_token,
        version_number=1,
        sha256_hash="a591a6d40bf420404a011733cfb7b190d62c65bf0bcda32b57b277d9ad9f146e",
        source_url="https://example.com/test.pdf",
        storage_path="/data/storage/test.pdf",
    )

    batch = [
        ChunkBatchItem(
            chunk_id="chunk_comp_1",
            chunk_index=0,
            text="Completion chunk 0",
            pdf_page_start=1,
            pdf_page_end=1,
            embedding=[0.0] * 384,
        )
    ]
    repo.write_chunks_batch(
        job_id=claimed.job_id,
        raw_claim_token=claimed.raw_claim_token,
        version_id=version_id,
        chunks=batch,
    )

    # Mismatched chunk count rejected
    with pytest.raises(Exception) as exc_info:
        repo.complete_job(
            job_id=claimed.job_id,
            raw_claim_token=claimed.raw_claim_token,
            expected_chunk_count=5,
        )
    assert "CHUNK_COUNT_MISMATCH" in str(exc_info.value)

    # Correct chunk count completes
    completed = repo.complete_job(
        job_id=claimed.job_id,
        raw_claim_token=claimed.raw_claim_token,
        expected_chunk_count=1,
    )
    assert completed is True


def test_fail_job_retryable_and_terminal(repo, clean_test_data, pg_session_factory):
    # Test retryable failure
    job_id = uuid.uuid4()
    with pg_session_factory() as session:
        with session.begin():
            session.execute(
                text(
                    """
                    INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, status, stage, attempt_count, max_attempts)
                    VALUES (:id, :key, 'https://example.com/retry.pdf', 'queued', 'pending', 0, 3);
                    """
                ),
                {"id": job_id, "key": f"key_{job_id}"},
            )

    claimed = repo.claim_job(instance_id="worker_fail")
    assert claimed is not None

    status = repo.fail_job(
        job_id=claimed.job_id,
        raw_claim_token=claimed.raw_claim_token,
        error_details={"reason": "network timeout"},
        is_retryable=True,
    )
    assert status == "queued"

    # Test terminal failure (max_attempts reached)
    job_term = uuid.uuid4()
    with pg_session_factory() as session:
        with session.begin():
            session.execute(
                text(
                    """
                    INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, status, stage, attempt_count, max_attempts)
                    VALUES (:id, :key, 'https://example.com/term.pdf', 'queued', 'pending', 2, 3);
                    """
                ),
                {"id": job_term, "key": f"key_{job_term}"},
            )

    claimed_term = repo.claim_job(instance_id="worker_term")
    assert claimed_term is not None

    term_status = repo.fail_job(
        job_id=claimed_term.job_id,
        raw_claim_token=claimed_term.raw_claim_token,
        error_details={"reason": "permanent corrupt pdf"},
        is_retryable=False,
    )
    assert term_status == "failed"


def test_recover_stale_jobs(repo, clean_test_data, pg_session_factory):
    expired_id = uuid.uuid4()
    with pg_session_factory() as session:
        with session.begin():
            session.execute(
                text(
                    """
                    INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, status, stage, lease_expires_at, claim_token_hash, attempt_count, max_attempts)
                    VALUES (:id, 'key_stale', 'https://example.com/stale.pdf', 'processing', 'parsing', clock_timestamp() - INTERVAL '100 seconds', 'hash_stale', 0, 3);
                    """
                ),
                {"id": expired_id},
            )

    recovered = repo.recover_stale_jobs(batch_limit=10)
    assert recovered == 1


def test_mark_duplicate_job(repo, clean_test_data, pg_session_factory):
    job_id = uuid.uuid4()
    with pg_session_factory() as session:
        with session.begin():
            session.execute(
                text(
                    """
                    INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, status, stage, attempt_count, max_attempts)
                    VALUES (:id, :key, 'https://example.com/dup.pdf', 'queued', 'pending', 0, 3);
                    """
                ),
                {"id": job_id, "key": f"key_{job_id}"},
            )

    claimed = repo.claim_job(instance_id="worker_dup")
    assert claimed is not None
    assert claimed.job_id == job_id

    # Invalid token rejected
    with pytest.raises((PersistenceError, ConnectionStorageError)):
        repo.mark_duplicate_job(
            job_id=job_id,
            raw_claim_token="bad" * 21 + "a",
            error_details={"reason": "duplicate content"},
        )

    # Valid duplicate transition
    status = repo.mark_duplicate_job(
        job_id=job_id,
        raw_claim_token=claimed.raw_claim_token,
        error_details={"reason": "Identical SHA-256 content detected"},
    )
    assert status == "duplicate"

    with pg_session_factory() as session:
        row = session.execute(
            text(
                """
                SELECT status, stage, claim_token_hash, lease_expires_at, completed_at, error_details
                FROM public.ingestion_jobs
                WHERE id = :id;
                """
            ),
            {"id": job_id},
        ).mappings().one()

        assert row["status"] == "duplicate"
        assert row["stage"] == "finished"
        assert row["claim_token_hash"] is None
        assert row["lease_expires_at"] is None
        assert row["completed_at"] is not None
        assert row["error_details"]["code"] == "DUPLICATE_DOCUMENT"
