"""Integration and Security Tests for Dedicated Duplicate-Job Transition (Phase 7.2 Task 2E.2).

Validates stored procedure `app.mark_duplicate_job` and repository adapter:
- Test A — Successful duplicate transition: Preserves original indexed document and marks duplicate.
- Test B — Duplicate after partial processing: Safely deletes unindexed processing artifacts without touching original.
- Test C — No processing version: Valid claimed job without version transitions cleanly.
- Test D — Invalid token: Rejected with UNAUTHORIZED_WORKER; leaves state unchanged.
- Test E — Expired lease: Rejected with LEASE_EXPIRED; leaves state unchanged.
- Test F — Repeated transition: Attempting to mark an already duplicate job is rejected.
- Test G — Direct DML denial: Restricted worker login cannot directly INSERT/UPDATE/DELETE/TRUNCATE protected tables.
- Test H — API isolation: app_user cannot execute app.mark_duplicate_job.
"""

from __future__ import annotations

import os
import secrets
import string
import uuid
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import DBAPIError, ProgrammingError

from app.persistence.worker_procedure_repo import (
    PostgresWorkerProcedureRepository,
    ChunkBatchItem,
)
from app.persistence.interfaces import PersistenceError, ConnectionStorageError


from tests.db_test_guard import get_approved_test_db_url

ADMIN_DB_URL = get_approved_test_db_url()
pytestmark = pytest.mark.skipif(
    not ADMIN_DB_URL,
    reason="No approved local PostgreSQL test database configured (set TEST_DATABASE_URL=postgresql+psycopg://.../sebi_rag_test).",
)


def _generate_random_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


@pytest.fixture(scope="module")
def restricted_worker_login():
    """Create a temporary restricted login role inheriting ONLY ingestion_worker_role."""
    admin_engine = create_engine(ADMIN_DB_URL, isolation_level="AUTOCOMMIT")
    role_name = f"test_worker_dup_{uuid.uuid4().hex[:8]}"
    role_password = _generate_random_password()

    with admin_engine.connect() as conn:
        conn.execute(
            text(
                f"CREATE ROLE {role_name} WITH LOGIN PASSWORD '{role_password}' "
                f"NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS INHERIT;"
            )
        )
        conn.execute(text(f"GRANT ingestion_worker_role TO {role_name};"))
        conn.execute(text(f"GRANT CONNECT ON DATABASE sebi_rag_test TO {role_name};"))

    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(ADMIN_DB_URL)
    netloc = f"{role_name}:{role_password}@{parsed.hostname}:{parsed.port or 5432}"
    restricted_url = urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))

    yield {
        "role_name": role_name,
        "restricted_url": restricted_url,
    }

    # Teardown
    with admin_engine.connect() as conn:
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE usename = :r AND pid <> pg_backend_pid();"
            ),
            {"r": role_name},
        )
        conn.execute(text(f"REVOKE ALL ON DATABASE sebi_rag_test FROM {role_name};"))
        conn.execute(text(f"REVOKE ingestion_worker_role FROM {role_name};"))
        conn.execute(text(f"DROP ROLE IF EXISTS {role_name};"))
    admin_engine.dispose()


@pytest.fixture(scope="module")
def app_user_login():
    """Create a temporary login role inheriting ONLY app_user."""
    admin_engine = create_engine(ADMIN_DB_URL, isolation_level="AUTOCOMMIT")
    role_name = f"test_app_user_{uuid.uuid4().hex[:8]}"
    role_password = _generate_random_password()

    with admin_engine.connect() as conn:
        conn.execute(
            text(
                f"CREATE ROLE {role_name} WITH LOGIN PASSWORD '{role_password}' "
                f"NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS INHERIT;"
            )
        )
        conn.execute(text(f"GRANT app_user TO {role_name};"))
        conn.execute(text(f"GRANT CONNECT ON DATABASE sebi_rag_test TO {role_name};"))

    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(ADMIN_DB_URL)
    netloc = f"{role_name}:{role_password}@{parsed.hostname}:{parsed.port or 5432}"
    app_user_url = urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))

    yield {
        "role_name": role_name,
        "app_user_url": app_user_url,
    }

    # Teardown
    with admin_engine.connect() as conn:
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE usename = :r AND pid <> pg_backend_pid();"
            ),
            {"r": role_name},
        )
        conn.execute(text(f"REVOKE ALL ON DATABASE sebi_rag_test FROM {role_name};"))
        conn.execute(text(f"REVOKE app_user FROM {role_name};"))
        conn.execute(text(f"DROP ROLE IF EXISTS {role_name};"))
    admin_engine.dispose()


@pytest.fixture
def admin_session():
    engine = create_engine(ADMIN_DB_URL, pool_pre_ping=True)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        session.execute(
            text("INSERT INTO public.regulators (id, name, official_portal_url) VALUES ('SEBI', 'SEBI', 'https://sebi.gov.in') ON CONFLICT DO NOTHING;")
        )
        session.execute(
            text("INSERT INTO public.regulatory_domains (id, regulator_id, code, title) VALUES ('SEBI_CSCRF', 'SEBI', 'CSCRF', 'CSCRF') ON CONFLICT DO NOTHING;")
        )
        session.commit()
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


@pytest.fixture
def restricted_repo(restricted_worker_login):
    engine = create_engine(restricted_worker_login["restricted_url"], pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield PostgresWorkerProcedureRepository(session_factory=factory)
    engine.dispose()


def test_a_successful_duplicate_transition(admin_session, restricted_repo):
    """
    Test A — Successful duplicate transition:
    - Create an indexed original document and version.
    - Create a second ingestion job with the same SHA-256 but a different document ID.
    - Claim the second job.
    - Invoke mark_duplicate_job.
    - Verify its status is duplicate.
    - Verify the original document remains indexed and unchanged.
    """
    orig_doc_id = f"doc_orig_{uuid.uuid4().hex[:8]}"
    dup_doc_id = f"doc_dup_{uuid.uuid4().hex[:8]}"
    sha256 = (uuid.uuid4().hex + uuid.uuid4().hex)[:64]
    orig_ver_id = f"{orig_doc_id}_v1"

    # 1. Admin creates indexed original document and version
    admin_session.execute(
        text("INSERT INTO public.regulatory_documents (id, domain_id, title) VALUES (:id, 'SEBI_CSCRF', 'Original');"),
        {"id": orig_doc_id},
    )
    admin_session.execute(
        text(
            """
            INSERT INTO public.document_versions (id, document_id, version_number, source_url, storage_path, sha256, extraction_status)
            VALUES (:ver_id, :doc_id, 1, 'https://example.com/orig.pdf', '/tmp/orig.pdf', :sha, 'indexed');
            """
        ),
        {"ver_id": orig_ver_id, "doc_id": orig_doc_id, "sha": sha256},
    )
    admin_session.execute(
        text("INSERT INTO public.chunks (id, version_id, chunk_index, text, pdf_page_start, pdf_page_end) VALUES (:cid, :vid, 0, 'original text', 1, 1);"),
        {"cid": f"{orig_ver_id}_c0", "vid": orig_ver_id},
    )
    admin_session.execute(
        text("INSERT INTO public.chunk_embeddings (chunk_id, model_name, embedding_dim, embedding) VALUES (:cid, 'BAAI/bge-small-en-v1.5', 384, :emb);"),
        {"cid": f"{orig_ver_id}_c0", "emb": [0.1] * 384},
    )

    # 2. Admin creates second ingestion job with same SHA-256 and different doc_id
    dup_job_id = uuid.uuid4()
    admin_session.execute(
        text("INSERT INTO public.regulatory_documents (id, domain_id, title) VALUES (:id, 'SEBI_CSCRF', 'Duplicate');"),
        {"id": dup_doc_id},
    )
    admin_session.execute(
        text(
            """
            INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, document_id, status, stage, attempt_count, max_attempts)
            VALUES (:id, :key, 'https://example.com/dup.pdf', :doc_id, 'queued', 'pending', 0, 3);
            """
        ),
        {"id": dup_job_id, "key": f"key_{dup_job_id}", "doc_id": dup_doc_id},
    )
    admin_session.commit()

    # 3. Restricted worker claims second job
    claimed = restricted_repo.claim_job(instance_id="worker_dup_a")
    assert claimed is not None
    assert claimed.job_id == dup_job_id

    # 4. Invoke mark_duplicate_job through restricted worker
    status = restricted_repo.mark_duplicate_job(
        job_id=claimed.job_id,
        raw_claim_token=claimed.raw_claim_token,
        error_details={
            "code": "DUPLICATE_DOCUMENT",
            "reason": f"Identical SHA-256 already ingested as '{orig_doc_id}'",
            "original_document_id": orig_doc_id,
        },
    )
    assert status == "duplicate"

    # 5. Verify duplicate job record in DB
    job_row = admin_session.execute(
        text("SELECT status, stage, claim_token_hash, lease_expires_at, completed_at, error_details FROM public.ingestion_jobs WHERE id = :id;"),
        {"id": dup_job_id},
    ).mappings().one()
    assert job_row["status"] == "duplicate"
    assert job_row["stage"] == "finished"
    assert job_row["claim_token_hash"] is None
    assert job_row["lease_expires_at"] is None
    assert job_row["completed_at"] is not None
    assert job_row["error_details"]["code"] == "DUPLICATE_DOCUMENT"

    # 6. Verify original document, version, chunks, and embeddings remain indexed and untouched
    orig_ver_row = admin_session.execute(
        text("SELECT extraction_status, sha256 FROM public.document_versions WHERE id = :id;"),
        {"id": orig_ver_id},
    ).mappings().one()
    assert orig_ver_row["extraction_status"] == "indexed"
    assert orig_ver_row["sha256"] == sha256

    chunk_count = admin_session.execute(
        text("SELECT COUNT(*) FROM public.chunks WHERE version_id = :id;"),
        {"id": orig_ver_id},
    ).scalar_one()
    assert chunk_count == 1


def test_b_duplicate_after_partial_processing(admin_session, restricted_repo):
    """
    Test B — Duplicate after partial processing:
    - Create a second job.
    - Create its processing version.
    - Persist some chunks and embeddings.
    - Mark the job duplicate.
    - Verify only the second job's unindexed processing artifacts are deleted.
    - Verify the original indexed document and its chunks/embeddings remain intact.
    """
    orig_doc_id = f"doc_orig_b_{uuid.uuid4().hex[:8]}"
    dup_doc_id = f"doc_dup_b_{uuid.uuid4().hex[:8]}"
    sha256 = (uuid.uuid4().hex + uuid.uuid4().hex)[:64]
    orig_ver_id = f"{orig_doc_id}_v1"

    # 1. Admin seeds original indexed document
    admin_session.execute(
        text("INSERT INTO public.regulatory_documents (id, domain_id, title) VALUES (:id, 'SEBI_CSCRF', 'Original B');"),
        {"id": orig_doc_id},
    )
    admin_session.execute(
        text(
            """
            INSERT INTO public.document_versions (id, document_id, version_number, source_url, storage_path, sha256, extraction_status)
            VALUES (:ver_id, :doc_id, 1, 'https://example.com/orig.pdf', '/tmp/orig.pdf', :sha, 'indexed');
            """
        ),
        {"ver_id": orig_ver_id, "doc_id": orig_doc_id, "sha": sha256},
    )
    admin_session.execute(
        text("INSERT INTO public.chunks (id, version_id, chunk_index, text, pdf_page_start, pdf_page_end) VALUES (:cid, :vid, 0, 'orig text', 1, 1);"),
        {"cid": f"{orig_ver_id}_c0", "vid": orig_ver_id},
    )
    admin_session.execute(
        text("INSERT INTO public.chunk_embeddings (chunk_id, model_name, embedding_dim, embedding) VALUES (:cid, 'BAAI/bge-small-en-v1.5', 384, :emb);"),
        {"cid": f"{orig_ver_id}_c0", "emb": [0.2] * 384},
    )

    # 2. Create second job
    dup_job_id = uuid.uuid4()
    admin_session.execute(
        text("INSERT INTO public.regulatory_documents (id, domain_id, title) VALUES (:id, 'SEBI_CSCRF', 'Dup B');"),
        {"id": dup_doc_id},
    )
    admin_session.execute(
        text(
            """
            INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, document_id, status, stage, attempt_count, max_attempts)
            VALUES (:id, :key, 'https://example.com/dupb.pdf', :doc_id, 'queued', 'pending', 0, 3);
            """
        ),
        {"id": dup_job_id, "key": f"key_{dup_job_id}", "doc_id": dup_doc_id},
    )
    admin_session.commit()

    # 3. Restricted worker claims second job
    claimed = restricted_repo.claim_job(instance_id="worker_dup_b")
    assert claimed is not None
    assert claimed.job_id == dup_job_id

    # 4. Partial processing: worker creates processing version
    proc_ver_id = restricted_repo.create_version(
        job_id=claimed.job_id,
        raw_claim_token=claimed.raw_claim_token,
        version_number=1,
        sha256_hash=sha256,
        source_url="https://example.com/dupb.pdf",
        storage_path="/tmp/dupb.pdf",
    )
    assert proc_ver_id == f"{dup_doc_id}_v1"

    # 5. Persist chunks and embeddings for the second job
    chunk_item = ChunkBatchItem(
        chunk_id=f"{proc_ver_id}_c0",
        chunk_index=0,
        text="temporary chunk text",
        pdf_page_start=1,
        pdf_page_end=1,
        embedding=[0.3] * 384,
    )
    written = restricted_repo.write_chunks_batch(
        job_id=claimed.job_id,
        raw_claim_token=claimed.raw_claim_token,
        version_id=proc_ver_id,
        chunks=[chunk_item],
    )
    assert written == 1

    # Verify processing chunks exist before transition
    assert admin_session.execute(
        text("SELECT COUNT(*) FROM public.chunks WHERE version_id = :id;"),
        {"id": proc_ver_id},
    ).scalar_one() == 1

    # 6. Mark job duplicate
    status = restricted_repo.mark_duplicate_job(
        job_id=claimed.job_id,
        raw_claim_token=claimed.raw_claim_token,
    )
    assert status == "duplicate"

    # 7. Verify the duplicate job's processing version, chunks, and embeddings were completely removed
    proc_ver_count = admin_session.execute(
        text("SELECT COUNT(*) FROM public.document_versions WHERE id = :id;"),
        {"id": proc_ver_id},
    ).scalar_one()
    assert proc_ver_count == 0

    proc_chunk_count = admin_session.execute(
        text("SELECT COUNT(*) FROM public.chunks WHERE version_id = :id;"),
        {"id": proc_ver_id},
    ).scalar_one()
    assert proc_chunk_count == 0

    proc_emb_count = admin_session.execute(
        text("SELECT COUNT(*) FROM public.chunk_embeddings WHERE chunk_id = :cid;"),
        {"cid": f"{proc_ver_id}_c0"},
    ).scalar_one()
    assert proc_emb_count == 0

    # 8. Verify the original document, version, chunks, and embeddings remain completely intact
    orig_ver_count = admin_session.execute(
        text("SELECT COUNT(*) FROM public.document_versions WHERE id = :id AND extraction_status = 'indexed';"),
        {"id": orig_ver_id},
    ).scalar_one()
    assert orig_ver_count == 1

    orig_chunk_count = admin_session.execute(
        text("SELECT COUNT(*) FROM public.chunks WHERE version_id = :id;"),
        {"id": orig_ver_id},
    ).scalar_one()
    assert orig_chunk_count == 1


def test_c_no_processing_version(admin_session, restricted_repo):
    """
    Test C — No processing version:
    - Mark a valid claimed duplicate job that has no processing version.
    - Verify the transition succeeds safely.
    """
    job_id = uuid.uuid4()
    admin_session.execute(
        text(
            """
            INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, status, stage, attempt_count, max_attempts)
            VALUES (:id, :key, 'https://example.com/nover.pdf', 'queued', 'pending', 0, 3);
            """
        ),
        {"id": job_id, "key": f"key_{job_id}"},
    )
    admin_session.commit()

    claimed = restricted_repo.claim_job(instance_id="worker_c")
    assert claimed is not None
    assert claimed.job_id == job_id

    # Verify version_id is NULL
    job_row = admin_session.execute(
        text("SELECT version_id FROM public.ingestion_jobs WHERE id = :id;"),
        {"id": job_id},
    ).mappings().one()
    assert job_row["version_id"] is None

    # Transition to duplicate
    status = restricted_repo.mark_duplicate_job(
        job_id=claimed.job_id,
        raw_claim_token=claimed.raw_claim_token,
    )
    assert status == "duplicate"

    row = admin_session.execute(
        text("SELECT status, stage FROM public.ingestion_jobs WHERE id = :id;"),
        {"id": job_id},
    ).mappings().one()
    assert row["status"] == "duplicate"
    assert row["stage"] == "finished"


def test_d_invalid_token(admin_session, restricted_repo):
    """
    Test D — Invalid token:
    - Call the procedure with an incorrect token.
    - Verify authorization fails.
    - Verify the job and document data remain unchanged.
    """
    job_id = uuid.uuid4()
    admin_session.execute(
        text(
            """
            INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, status, stage, attempt_count, max_attempts)
            VALUES (:id, :key, 'https://example.com/invtok.pdf', 'queued', 'pending', 0, 3);
            """
        ),
        {"id": job_id, "key": f"key_{job_id}"},
    )
    admin_session.commit()

    claimed = restricted_repo.claim_job(instance_id="worker_d")
    assert claimed is not None

    invalid_token = "0123456789abcdef" * 4  # 64-char wrong token

    with pytest.raises((PersistenceError, ConnectionStorageError)) as exc_info:
        restricted_repo.mark_duplicate_job(
            job_id=claimed.job_id,
            raw_claim_token=invalid_token,
        )
    assert "UNAUTHORIZED_WORKER" in str(exc_info.value)

    # Verify job remains unchanged in processing status
    row = admin_session.execute(
        text("SELECT status, claim_token_hash FROM public.ingestion_jobs WHERE id = :id;"),
        {"id": job_id},
    ).mappings().one()
    assert row["status"] == "processing"
    assert row["claim_token_hash"] is not None


def test_e_expired_lease(admin_session, restricted_repo):
    """
    Test E — Expired lease:
    - Expire the lease.
    - Attempt the transition.
    - Verify it is rejected.
    """
    job_id = uuid.uuid4()
    admin_session.execute(
        text(
            """
            INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, status, stage, attempt_count, max_attempts)
            VALUES (:id, :key, 'https://example.com/explease.pdf', 'queued', 'pending', 0, 3);
            """
        ),
        {"id": job_id, "key": f"key_{job_id}"},
    )
    admin_session.commit()

    claimed = restricted_repo.claim_job(instance_id="worker_e")
    assert claimed is not None

    # Force lease expiration
    admin_session.execute(
        text("UPDATE public.ingestion_jobs SET lease_expires_at = clock_timestamp() - INTERVAL '30 seconds' WHERE id = :id;"),
        {"id": job_id},
    )
    admin_session.commit()

    with pytest.raises((PersistenceError, ConnectionStorageError)) as exc_info:
        restricted_repo.mark_duplicate_job(
            job_id=claimed.job_id,
            raw_claim_token=claimed.raw_claim_token,
        )
    assert "LEASE_EXPIRED" in str(exc_info.value)

    # Verify job remains processing
    row = admin_session.execute(
        text("SELECT status FROM public.ingestion_jobs WHERE id = :id;"),
        {"id": job_id},
    ).mappings().one()
    assert row["status"] == "processing"


def test_f_repeated_transition(admin_session, restricted_repo):
    """
    Test F — Repeated transition:
    - Attempt to mark an already duplicate job again.
    - Verify the procedure rejects the invalid transition without modifying data.
    """
    job_id = uuid.uuid4()
    admin_session.execute(
        text(
            """
            INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, status, stage, attempt_count, max_attempts)
            VALUES (:id, :key, 'https://example.com/repeat.pdf', 'queued', 'pending', 0, 3);
            """
        ),
        {"id": job_id, "key": f"key_{job_id}"},
    )
    admin_session.commit()

    claimed = restricted_repo.claim_job(instance_id="worker_f")
    assert claimed is not None

    # First duplicate transition succeeds
    status = restricted_repo.mark_duplicate_job(
        job_id=claimed.job_id,
        raw_claim_token=claimed.raw_claim_token,
    )
    assert status == "duplicate"

    # Second duplicate transition attempt with same token must be rejected
    with pytest.raises((PersistenceError, ConnectionStorageError)) as exc_info:
        restricted_repo.mark_duplicate_job(
            job_id=claimed.job_id,
            raw_claim_token=claimed.raw_claim_token,
        )
    assert "INVALID_JOB_STATUS" in str(exc_info.value) or "UNAUTHORIZED_WORKER" in str(exc_info.value)

    # Job status remains duplicate
    row = admin_session.execute(
        text("SELECT status FROM public.ingestion_jobs WHERE id = :id;"),
        {"id": job_id},
    ).mappings().one()
    assert row["status"] == "duplicate"


def test_g_direct_dml_denial(restricted_worker_login):
    """
    Test G — Direct DML denial:
    - Verify the worker login still cannot directly INSERT, UPDATE, DELETE, or TRUNCATE protected tables.
    """
    worker_engine = create_engine(restricted_worker_login["restricted_url"], pool_pre_ping=True)

    # 1. INSERT denied
    with worker_engine.connect() as conn:
        with pytest.raises(DBAPIError) as exc_info:
            conn.execute(
                text(
                    "INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url) "
                    "VALUES ('00000000-0000-0000-0000-000000000099', 'dml_test', 'https://example.com');"
                )
            )
        assert "permission denied" in str(exc_info.value).lower()

    # 2. UPDATE denied
    with worker_engine.connect() as conn:
        with pytest.raises(DBAPIError) as exc_info:
            conn.execute(
                text("UPDATE public.ingestion_jobs SET status = 'duplicate' WHERE id = '00000000-0000-0000-0000-000000000099';")
            )
        assert "permission denied" in str(exc_info.value).lower()

    # 3. DELETE denied
    with worker_engine.connect() as conn:
        with pytest.raises(DBAPIError) as exc_info:
            conn.execute(
                text("DELETE FROM public.document_versions WHERE id = 'fake_v1';")
            )
        assert "permission denied" in str(exc_info.value).lower()

    # 4. TRUNCATE denied
    with worker_engine.connect() as conn:
        with pytest.raises(DBAPIError) as exc_info:
            conn.execute(text("TRUNCATE TABLE public.chunks;"))
        assert "permission denied" in str(exc_info.value).lower()

    worker_engine.dispose()


def test_h_api_isolation(app_user_login):
    """
    Test H — API isolation:
    - Verify app_user cannot execute app.mark_duplicate_job.
    """
    app_engine = create_engine(app_user_login["app_user_url"], pool_pre_ping=True)

    with app_engine.connect() as conn:
        with pytest.raises(DBAPIError) as exc_info:
            conn.execute(
                text("SELECT app.mark_duplicate_job('00000000-0000-0000-0000-000000000001'::uuid, 'dummy_token');")
            )
        assert "permission denied" in str(exc_info.value).lower()

    app_engine.dispose()
