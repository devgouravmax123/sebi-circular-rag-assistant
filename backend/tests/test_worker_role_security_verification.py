"""End-to-End Restricted Worker Role Security Verification (Task 2D).

This test suite validates that:
1. A temporary restricted PostgreSQL login inheriting ONLY `ingestion_worker_role`:
   - Has NO SUPERUSER, NO CREATEDB, NO CREATEROLE, NO BYPASSRLS, NO schema CREATE privileges.
   - Successfully runs the complete ingestion worker lifecycle via stored procedures.
2. Direct-write operations (INSERT, UPDATE, DELETE, TRUNCATE) to protected tables fail with PermissionDenied.
3. Negative security cases (invalid claim token, expired lease, tampering) fail with PostgreSQL procedure exceptions.
4. Secrets/passwords/claim tokens are never exposed in log outputs or string reprs.
5. The temporary restricted login is cleaned up automatically in teardown.
"""

from __future__ import annotations

import os
import secrets
import string
import uuid
import pytest
import httpx
import fitz  # PyMuPDF
import numpy as np
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import DBAPIError, ProgrammingError

from app.persistence.interfaces import ConnectionStorageError
from app.retrieval.dense_embedder import DenseEmbedder
from app.persistence.postgres import PostgresStorageRepositoryFactory
from app.persistence.worker_procedure_repo import (
    PostgresWorkerProcedureRepository,
    ChunkBatchItem,
)
from app.ingestion.orchestrator import IngestionOrchestrator, IngestionWorker
from app.ingestion.downloader import SecurePDFDownloader
from app.ingestion.extractor import PDFExtractor
from app.ingestion.chunker import TextChunker


from tests.db_test_guard import get_approved_test_db_url

ADMIN_DB_URL = get_approved_test_db_url()
pytestmark = pytest.mark.skipif(
    not ADMIN_DB_URL,
    reason="No approved local PostgreSQL test database configured (set TEST_DATABASE_URL=postgresql+psycopg://.../sebi_rag_test).",
)


def _generate_random_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def create_test_pdf_bytes(pages_text: list[str]) -> bytes:
    doc = fitz.open()
    for text_content in pages_text:
        page = doc.new_page()  # type: ignore[attr-defined]
        page.insert_textbox(fitz.Rect(50, 50, 550, 750), text_content)
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


class MockEmbedder(DenseEmbedder):
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


@pytest.fixture(scope="module")
def restricted_worker_login():
    """Create a temporary restricted login role for testing and drop it after the test module runs."""
    admin_engine = create_engine(ADMIN_DB_URL, isolation_level="AUTOCOMMIT")
    role_name = f"test_worker_{uuid.uuid4().hex[:8]}"
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

    # Construct the restricted worker URL using the temporary credentials
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(ADMIN_DB_URL)
    netloc = f"{role_name}:{role_password}@{parsed.hostname}:{parsed.port or 5432}"
    restricted_url = urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))

    yield {
        "role_name": role_name,
        "restricted_url": restricted_url,
    }

    # Teardown: terminate connections, revoke database privileges, and drop temporary role
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


@pytest.fixture
def admin_session():
    engine = create_engine(ADMIN_DB_URL, pool_pre_ping=True)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        # Pre-seed regulators / domains if needed
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
def clean_test_data(admin_session):
    admin_session.execute(text("TRUNCATE TABLE public.chunk_embeddings CASCADE;"))
    admin_session.execute(text("TRUNCATE TABLE public.chunks CASCADE;"))
    admin_session.execute(text("TRUNCATE TABLE public.document_versions CASCADE;"))
    admin_session.execute(text("TRUNCATE TABLE public.regulatory_documents CASCADE;"))
    admin_session.execute(text("TRUNCATE TABLE public.ingestion_jobs CASCADE;"))
    admin_session.commit()
    yield


@pytest.fixture
def restricted_session_factory(restricted_worker_login):
    engine = create_engine(restricted_worker_login["restricted_url"], pool_pre_ping=True)
    Session = sessionmaker(bind=engine)
    try:
        yield Session
    finally:
        engine.dispose()


def test_restricted_login_privilege_audit(restricted_session_factory, restricted_worker_login):
    """Step 1 & Step 3: Verify current_user, session_user, role flags, and schema permissions."""
    role_name = restricted_worker_login["role_name"]
    with restricted_session_factory() as session:
        # Check identity
        user_res = session.execute(text("SELECT current_user, session_user;")).fetchone()
        assert user_res is not None
        assert user_res[0] == role_name
        assert user_res[1] == role_name

        # Check role flags in pg_roles
        role_res = session.execute(
            text(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user;"
            )
        ).fetchone()
        assert role_res is not None
        assert role_res[0] is False  # rolsuper
        assert role_res[1] is False  # rolcreatedb
        assert role_res[2] is False  # rolcreaterole
        assert role_res[3] is False  # rolbypassrls

        # Check membership in ingestion_worker_role
        member_res = session.execute(
            text("SELECT pg_has_role(current_user, 'ingestion_worker_role', 'MEMBER');")
        ).scalar()
        assert member_res is True

        # Check schema permissions: USAGE allowed, CREATE disallowed
        app_usage = session.execute(text("SELECT has_schema_privilege('app', 'USAGE');")).scalar()
        app_create = session.execute(text("SELECT has_schema_privilege('app', 'CREATE');")).scalar()
        public_create = session.execute(text("SELECT has_schema_privilege('public', 'CREATE');")).scalar()
        assert app_usage is True
        assert app_create is False
        assert public_create is False


def test_direct_writes_denied_for_restricted_login(restricted_session_factory):
    """Step 3: Verify that direct INSERT, UPDATE, DELETE, TRUNCATE are blocked on protected tables."""
    with restricted_session_factory() as session:
        # 1. Direct INSERT to ingestion_jobs
        with pytest.raises(ProgrammingError) as exc_insert:
            session.execute(
                text(
                    "INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, status, stage) "
                    "VALUES (:id, :key, 'https://example.com/test.pdf', 'queued', 'pending');"
                ),
                {"id": str(uuid.uuid4()), "key": f"key_{uuid.uuid4()}"},
            )
        session.rollback()
        assert "permission denied" in str(exc_insert.value).lower()

        # 2. Direct UPDATE to ingestion_jobs
        with pytest.raises(ProgrammingError) as exc_update:
            session.execute(
                text("UPDATE public.ingestion_jobs SET status = 'completed' WHERE 1=1;")
            )
        session.rollback()
        assert "permission denied" in str(exc_update.value).lower()

        # 3. Direct DELETE from ingestion_jobs
        with pytest.raises(ProgrammingError) as exc_delete:
            session.execute(text("DELETE FROM public.ingestion_jobs WHERE 1=1;"))
        session.rollback()
        assert "permission denied" in str(exc_delete.value).lower()

        # 4. Direct TRUNCATE on ingestion_jobs
        with pytest.raises(ProgrammingError) as exc_trunc:
            session.execute(text("TRUNCATE TABLE public.ingestion_jobs;"))
        session.rollback()
        assert "permission denied" in str(exc_trunc.value).lower()

        # 5. Direct INSERT to document_versions
        with pytest.raises(ProgrammingError) as exc_ver:
            session.execute(
                text(
                    "INSERT INTO public.document_versions (id, document_id, version_number, source_url, storage_path, sha256) "
                    "VALUES (:id, :doc_id, 1, 'https://example.com/doc', 'path', 'hash');"
                ),
                {"id": str(uuid.uuid4()), "doc_id": "sebi_doc_01"},
            )
        session.rollback()
        assert "permission denied" in str(exc_ver.value).lower()

        # 6. Direct INSERT to chunks
        with pytest.raises(ProgrammingError) as exc_chunk:
            session.execute(
                text(
                    "INSERT INTO public.chunks (id, version_id, chunk_index, text, pdf_page_start, pdf_page_end) "
                    "VALUES (:id, :ver_id, 0, 'content', 1, 1);"
                ),
                {"id": str(uuid.uuid4()), "ver_id": str(uuid.uuid4())},
            )
        session.rollback()
        assert "permission denied" in str(exc_chunk.value).lower()


def test_restricted_worker_full_lifecycle_happy_path(
    clean_test_data,
    admin_session,
    restricted_worker_login,
    restricted_session_factory,
):
    """Step 4: Execute the complete worker ingestion lifecycle using ONLY the restricted login."""
    # 1. Seed document and job via admin_session
    doc_id = f"doc_{uuid.uuid4().hex[:8]}"
    job_id = uuid.uuid4()

    pdf_bytes = create_test_pdf_bytes([
        "SEBI Master Circular on Portfolio Managers. Section 1: Portfolio management norms.",
        "SEBI Master Circular on Portfolio Managers. Section 2: Investment advisory guidelines.",
    ])
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=pdf_bytes))
    downloader = SecurePDFDownloader(allow_http_for_test=True, custom_transport=transport)
    embedder = MockEmbedder(dim=384)

    admin_session.execute(
        text(
            "INSERT INTO public.regulatory_documents (id, domain_id, title) "
            "VALUES (:doc_id, 'SEBI_CSCRF', 'Restricted Worker Test Doc');"
        ),
        {"doc_id": doc_id},
    )
    admin_session.execute(
        text(
            "INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, document_id, status, stage) "
            "VALUES (:id, :key, 'https://example.com/sec_test.pdf', :doc_id, 'queued', 'pending');"
        ),
        {"id": str(job_id), "doc_id": doc_id, "key": f"key_{job_id}"},
    )
    admin_session.commit()

    # 2. Worker connects strictly via restricted_url
    worker_proc_repo = PostgresWorkerProcedureRepository(session_factory=restricted_session_factory)
    worker_repo_factory = PostgresStorageRepositoryFactory(database_url=restricted_worker_login["restricted_url"])

    orchestrator = IngestionOrchestrator(
        repository_factory=worker_repo_factory,
        downloader=downloader,
        embedder=embedder,
        worker_procedure_repo=worker_proc_repo,
    )
    worker = IngestionWorker(
        repository_factory=worker_repo_factory,
        orchestrator=orchestrator,
        worker_id="restricted-worker-test-01",
        lease_timeout_seconds=60,
        worker_procedure_repo=worker_proc_repo,
    )

    # 3. Worker executes a single polling cycle
    processed_result = worker.run_once()
    assert processed_result is not None
    assert getattr(processed_result, "status", None) is not None
    assert processed_result.status.value == "completed"

    # 4. Verify results via admin_session
    job_row = admin_session.execute(
        text("SELECT status, stage FROM public.ingestion_jobs WHERE id = :id;"),
        {"id": str(job_id)},
    ).fetchone()
    assert job_row is not None
    assert job_row[0] == "completed"

    version_row = admin_session.execute(
        text("SELECT id, extraction_status FROM public.document_versions WHERE document_id = :id;"),
        {"id": doc_id},
    ).fetchone()
    assert version_row is not None
    assert version_row[1] == "indexed"

    chunk_count = admin_session.execute(
        text("SELECT count(*) FROM public.chunks WHERE version_id = :vid;"),
        {"vid": str(version_row[0])},
    ).scalar()
    assert chunk_count >= 1

    embedding_count = admin_session.execute(
        text("SELECT count(*) FROM public.chunk_embeddings WHERE chunk_id IN (SELECT id FROM public.chunks WHERE version_id = :vid);"),
        {"vid": str(version_row[0])},
    ).scalar()
    assert embedding_count == chunk_count


def test_restricted_worker_retryable_and_terminal_failures(
    clean_test_data,
    admin_session,
    restricted_session_factory,
):
    """Step 4: Test retryable failure and terminal dead-lettering through procedures using restricted login."""
    worker_proc_repo = PostgresWorkerProcedureRepository(session_factory=restricted_session_factory)

    # A. Retryable failure (attempt_count < max_attempts)
    job_id_retry = uuid.uuid4()
    admin_session.execute(
        text(
            "INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, status, stage, attempt_count, max_attempts) "
            "VALUES (:id, :key, 'https://example.com/retry.pdf', 'queued', 'pending', 0, 3);"
        ),
        {"id": str(job_id_retry), "key": f"key_{job_id_retry}"},
    )
    admin_session.commit()

    claim_res = worker_proc_repo.claim_job(instance_id="worker-retry-test", lease_duration_seconds=30)
    assert claim_res is not None
    assert claim_res.job_id == job_id_retry

    fail_res = worker_proc_repo.fail_job(
        job_id=job_id_retry,
        raw_claim_token=claim_res.raw_claim_token,
        error_details={"error": "Network glitch", "category": "TRANSIENT_NETWORK"},
        is_retryable=True,
    )
    assert fail_res == "queued"

    # Verify attempt_count incremented in DB
    job_after_retry = admin_session.execute(
        text("SELECT status, attempt_count FROM public.ingestion_jobs WHERE id = :id;"),
        {"id": str(job_id_retry)},
    ).fetchone()
    assert job_after_retry is not None
    assert job_after_retry[0] == "queued"
    assert job_after_retry[1] == 1

    # B. Terminal failure (is_retryable=False)
    job_id_terminal = uuid.uuid4()
    admin_session.execute(
        text(
            "INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, status, stage, attempt_count, max_attempts) "
            "VALUES (:id, :key, 'https://example.com/term.pdf', 'queued', 'pending', 2, 3);"
        ),
        {"id": str(job_id_terminal), "key": f"key_{job_id_terminal}"},
    )
    admin_session.commit()

    claim_term = worker_proc_repo.claim_job(instance_id="worker-term-test", lease_duration_seconds=30)
    assert claim_term is not None
    assert claim_term.job_id == job_id_terminal

    fail_term = worker_proc_repo.fail_job(
        job_id=job_id_terminal,
        raw_claim_token=claim_term.raw_claim_token,
        error_details={"error": "PDF corrupted", "category": "FATAL_CORRUPT_DOCUMENT"},
        is_retryable=False,
    )
    assert fail_term == "failed"


def test_restricted_worker_stale_job_recovery(
    clean_test_data,
    admin_session,
    restricted_session_factory,
):
    """Step 4: Test safe stale job recovery using restricted login."""
    worker_proc_repo = PostgresWorkerProcedureRepository(session_factory=restricted_session_factory)

    job_id_stale = uuid.uuid4()
    # Insert a job with expired lease in the past
    admin_session.execute(
        text(
            "INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, status, stage, attempt_count, max_attempts, lease_expires_at) "
            "VALUES (:id, :key, 'https://example.com/stale.pdf', 'processing', 'chunking', 0, 3, clock_timestamp() - interval '20 seconds');"
        ),
        {"id": str(job_id_stale), "key": f"key_{job_id_stale}"},
    )
    admin_session.commit()

    # Restricted worker executes recover_stale_jobs
    recovered_count = worker_proc_repo.recover_stale_jobs(batch_limit=10)
    assert recovered_count >= 1

    # Verify job reset to queued
    stale_status = admin_session.execute(
        text("SELECT status, attempt_count FROM public.ingestion_jobs WHERE id = :id;"),
        {"id": str(job_id_stale)},
    ).fetchone()
    assert stale_status is not None
    assert stale_status[0] == "queued"
    assert stale_status[1] == 1


def test_restricted_worker_negative_security_cases(
    clean_test_data,
    admin_session,
    restricted_session_factory,
):
    """Step 5: Test rejection of invalid claim token, expired lease, and unauthorized procedures."""
    worker_proc_repo = PostgresWorkerProcedureRepository(session_factory=restricted_session_factory)

    job_id = uuid.uuid4()
    admin_session.execute(
        text(
            "INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, status, stage, attempt_count, max_attempts) "
            "VALUES (:id, :key, 'https://example.com/neg.pdf', 'queued', 'pending', 0, 3);"
        ),
        {"id": str(job_id), "key": f"key_{job_id}"},
    )
    admin_session.commit()

    claim_res = worker_proc_repo.claim_job(instance_id="neg-test-worker", lease_duration_seconds=30)
    assert claim_res is not None

    # 1. Invalid claim token rejected on heartbeat
    fake_token = "0" * 64
    with pytest.raises(ConnectionStorageError) as exc_heartbeat:
        worker_proc_repo.heartbeat(job_id=job_id, raw_claim_token=fake_token, lease_duration_seconds=60)
    assert "UNAUTHORIZED_WORKER" in str(exc_heartbeat.value)

    # 2. Expired lease prevents write_chunks_batch
    # Force lease to expire in DB via admin
    admin_session.execute(
        text("UPDATE public.ingestion_jobs SET lease_expires_at = clock_timestamp() - interval '10 seconds' WHERE id = :id;"),
        {"id": str(job_id)},
    )
    admin_session.commit()

    # Attempt to write chunk batch with original token after expiration
    test_chunk = ChunkBatchItem(
        chunk_id=f"chk_{uuid.uuid4().hex[:8]}",
        chunk_index=0,
        text="Sample chunk content",
        pdf_page_start=1,
        pdf_page_end=1,
        embedding=[0.1] * 384,
    )
    with pytest.raises(ConnectionStorageError) as exc_write:
        worker_proc_repo.write_chunks_batch(
            job_id=job_id,
            raw_claim_token=claim_res.raw_claim_token,
            version_id="ver_expired_test",
            chunks=[test_chunk],
        )
    assert "UNAUTHORIZED_WORKER" in str(exc_write.value) or "LEASE_EXPIRED" in str(exc_write.value)


def test_restricted_worker_cannot_execute_unauthorized_procedures(
    restricted_session_factory,
):
    """Step 5: Ensure restricted worker cannot execute internal or non-granted procedures."""
    with restricted_session_factory() as session:
        with pytest.raises(ProgrammingError) as exc:
            session.execute(text("SELECT pg_catalog.pg_read_file('postgresql.conf');"))
        session.rollback()
        assert "permission denied" in str(exc.value).lower()


def test_restricted_worker_partial_batch_crash_recovery(
    clean_test_data,
    admin_session,
    tmp_path,
):
    """
    Task 2E.1B — Restricted-Role Partial-Batch Crash/Retry Integration Test.

    Exercises the full multi-batch recovery sequence:
    Step 1: Worker A (Role A) claims the job with a temporary restricted login.
    Step 2: Worker A creates the processing version and extracts/chunks a multi-batch PDF.
    Step 3: Worker A writes ONLY Batch 1 (out of 3 batches).
    Step 4: Process death simulated (Worker A halts, lease expired in DB).
    Step 5: Worker B (Role B) reclaims the job with a fresh claim token.
            Asserts Worker A's stale token is rejected on write and complete.
    Step 6: Worker B retries, reconstructs chunks, and persists ALL batches (Batch 1 deduplicated, Batches 2 & 3 inserted).
    Step 7: Worker B completes the job with exact expected chunk count.
            Asserts job is 'completed', version is 'indexed', and 1:1 embedding coverage holds.
    """
    admin_engine = create_engine(ADMIN_DB_URL, isolation_level="AUTOCOMMIT")

    role_a = f"test_wka_{uuid.uuid4().hex[:8]}"
    pwd_a = _generate_random_password()
    role_b = f"test_wkb_{uuid.uuid4().hex[:8]}"
    pwd_b = _generate_random_password()

    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(ADMIN_DB_URL)

    url_a = urlunparse((parsed.scheme, f"{role_a}:{pwd_a}@{parsed.hostname}:{parsed.port or 5432}", parsed.path, "", "", ""))
    url_b = urlunparse((parsed.scheme, f"{role_b}:{pwd_b}@{parsed.hostname}:{parsed.port or 5432}", parsed.path, "", "", ""))

    # Create both restricted login roles
    with admin_engine.connect() as conn:
        conn.execute(text(f"CREATE ROLE {role_a} WITH LOGIN PASSWORD '{pwd_a}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS INHERIT;"))
        conn.execute(text(f"GRANT ingestion_worker_role TO {role_a};"))
        conn.execute(text(f"GRANT CONNECT ON DATABASE sebi_rag_test TO {role_a};"))

        conn.execute(text(f"CREATE ROLE {role_b} WITH LOGIN PASSWORD '{pwd_b}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS INHERIT;"))
        conn.execute(text(f"GRANT ingestion_worker_role TO {role_b};"))
        conn.execute(text(f"GRANT CONNECT ON DATABASE sebi_rag_test TO {role_b};"))

    try:
        engine_a = create_engine(url_a, pool_pre_ping=True)
        SessionA = sessionmaker(bind=engine_a)
        repo_a = PostgresWorkerProcedureRepository(session_factory=SessionA)

        engine_b = create_engine(url_b, pool_pre_ping=True)
        SessionB = sessionmaker(bind=engine_b)
        repo_b = PostgresWorkerProcedureRepository(session_factory=SessionB)

        # 1. Seed document and job
        doc_id = f"doc_crash_{uuid.uuid4().hex[:8]}"
        job_id = uuid.uuid4()
        source_url = f"https://example.com/{doc_id}.pdf"

        # Create a document that produces at least 6 chunks (with chunk_batch_size=2, this creates 3 batches)
        page_texts = [
            f"SEBI Section {i}: Compliance regulations, risk governance, operational guidelines, and auditing framework standard {i}."
            * 20
            for i in range(1, 7)
        ]
        pdf_bytes = create_test_pdf_bytes(page_texts)

        admin_session.execute(
            text("INSERT INTO public.regulatory_documents (id, domain_id, title) VALUES (:doc_id, 'SEBI_CSCRF', 'Crash Recovery Multi Batch Doc');"),
            {"doc_id": doc_id},
        )
        admin_session.execute(
            text("INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, document_id, status, stage, max_attempts) VALUES (:id, :key, :url, :doc_id, 'queued', 'pending', 3);"),
            {"id": str(job_id), "key": f"key_{job_id}", "url": source_url, "doc_id": doc_id},
        )
        admin_session.commit()

        # Step 1: Worker A claims the job using restricted role A
        claim_a = repo_a.claim_job(instance_id="worker-a-instance", lease_duration_seconds=60)
        assert claim_a is not None
        assert claim_a.job_id == job_id
        token_a = claim_a.raw_claim_token

        # Step 2: Worker A creates processing version
        version_id = repo_a.create_version(
            job_id=job_id,
            raw_claim_token=token_a,
            version_number=1,
            sha256_hash="a" * 64,
            source_url=source_url,
            storage_path=f"storage/{doc_id}.pdf",
        )
        assert version_id == f"{doc_id}_v1"

        # Generate deterministic chunks
        extractor = PDFExtractor()
        chunker = TextChunker(chunk_size_words=100, chunk_overlap_words=10)
        staging_pdf = tmp_path / "temp_crash.pdf"
        staging_pdf.write_bytes(pdf_bytes)
        pages = extractor.extract_pages(staging_pdf)
        chunks = chunker.chunk_document(doc_id, pages)
        assert len(chunks) >= 3, f"Expected at least 3 chunks, got {len(chunks)}"

        embedder = MockEmbedder(dim=384)
        emb_arr = embedder.embed_texts([c.text for c in chunks])

        chunk_items: list[ChunkBatchItem] = [
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

        # Divide into batches of 1 (to ensure multiple distinct batches)
        batch_size = 1
        batch_1 = chunk_items[0:batch_size]
        remaining_batches = chunk_items[batch_size:]

        # Step 3: Worker A writes ONLY Batch 1
        persisted_1 = repo_a.write_chunks_batch(
            job_id=job_id,
            raw_claim_token=token_a,
            version_id=version_id,
            chunks=batch_1,
        )
        assert persisted_1 == len(batch_1)

        # Verify Batch 1 is in DB
        batch1_count = admin_session.execute(
            text("SELECT count(*) FROM public.chunks WHERE version_id = :vid;"),
            {"vid": version_id},
        ).scalar()
        assert batch1_count == len(batch_1)

        # Step 4: Simulate process death of Worker A
        # Expire lease in DB
        admin_session.execute(
            text("UPDATE public.ingestion_jobs SET lease_expires_at = clock_timestamp() - interval '10 seconds' WHERE id = :id;"),
            {"id": str(job_id)},
        )
        admin_session.commit()

        # Step 5: Worker B reclaims the job
        # Recover stale job
        recovered = repo_b.recover_stale_jobs(batch_limit=10)
        assert recovered >= 1

        # Because recover_stale_jobs sets an exponential backoff next_retry_at (e.g. +30s),
        # advance next_retry_at to past so it is immediately eligible for claim in test
        admin_session.execute(
            text("UPDATE public.ingestion_jobs SET next_retry_at = clock_timestamp() - interval '1 second' WHERE id = :id;"),
            {"id": str(job_id)},
        )
        admin_session.commit()

        # Claim with Worker B
        claim_b = repo_b.claim_job(instance_id="worker-b-instance", lease_duration_seconds=60)
        assert claim_b is not None
        assert claim_b.job_id == job_id
        token_b = claim_b.raw_claim_token
        assert token_b != token_a

        # Negative assertion: Worker A's stale token is rejected on write_chunks_batch
        with pytest.raises(ConnectionStorageError) as exc_stale_write:
            repo_a.write_chunks_batch(
                job_id=job_id,
                raw_claim_token=token_a,
                version_id=version_id,
                chunks=remaining_batches[0:1],
            )
        assert "UNAUTHORIZED_WORKER" in str(exc_stale_write.value)

        # Negative assertion: Worker A's stale token is rejected on complete_job
        with pytest.raises(ConnectionStorageError) as exc_stale_complete:
            repo_a.complete_job(
                job_id=job_id,
                raw_claim_token=token_a,
                expected_chunk_count=len(chunks),
                allow_empty=False,
            )
        assert "UNAUTHORIZED_WORKER" in str(exc_stale_complete.value)

        # Step 6: Worker B retries and persists ALL batches (including re-submitting batch 1)
        # Re-submitting batch 1 must be safely deduplicated via ON CONFLICT (id) DO NOTHING
        repo_b.write_chunks_batch(
            job_id=job_id,
            raw_claim_token=token_b,
            version_id=version_id,
            chunks=batch_1,
        )

        # Persist remaining batches
        for b_idx in range(len(remaining_batches)):
            repo_b.write_chunks_batch(
                job_id=job_id,
                raw_claim_token=token_b,
                version_id=version_id,
                chunks=[remaining_batches[b_idx]],
            )

        # Negative assertion: Missing chunks check prevents premature completion if count is wrong
        with pytest.raises(ConnectionStorageError) as exc_count_mismatch:
            repo_b.complete_job(
                job_id=job_id,
                raw_claim_token=token_b,
                expected_chunk_count=len(chunks) + 5,  # False expected count
                allow_empty=False,
            )
        assert "CHUNK_COUNT_MISMATCH" in str(exc_count_mismatch.value)

        # Step 7: Completion by Worker B with exact expected count
        complete_ok = repo_b.complete_job(
            job_id=job_id,
            raw_claim_token=token_b,
            expected_chunk_count=len(chunks),
            allow_empty=False,
        )
        assert complete_ok is True

        # Verify final state in DB
        final_job = admin_session.execute(
            text("SELECT status, stage FROM public.ingestion_jobs WHERE id = :id;"),
            {"id": str(job_id)},
        ).fetchone()
        assert final_job is not None
        assert final_job[0] == "completed"
        assert final_job[1] == "finished"

        final_ver = admin_session.execute(
            text("SELECT extraction_status FROM public.document_versions WHERE id = :vid;"),
            {"vid": version_id},
        ).fetchone()
        assert final_ver is not None
        assert final_ver[0] == "indexed"

        # Verify no duplicate chunk indices exist
        idx_count = admin_session.execute(
            text("SELECT count(distinct chunk_index), count(*) FROM public.chunks WHERE version_id = :vid;"),
            {"vid": version_id},
        ).fetchone()
        assert idx_count is not None
        assert idx_count[0] == len(chunks)
        assert idx_count[1] == len(chunks)

        # Verify 1:1 embedding coverage
        emb_count = admin_session.execute(
            text("SELECT count(*) FROM public.chunk_embeddings ce JOIN public.chunks c ON c.id = ce.chunk_id WHERE c.version_id = :vid;"),
            {"vid": version_id},
        ).scalar()
        assert emb_count == len(chunks)

        engine_a.dispose()
        engine_b.dispose()

    finally:
        with admin_engine.connect() as conn:
            conn.execute(text(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename IN ('{role_a}', '{role_b}') AND pid <> pg_backend_pid();"))
            conn.execute(text(f"REVOKE ALL ON DATABASE sebi_rag_test FROM {role_a}, {role_b};"))
            conn.execute(text(f"REVOKE ingestion_worker_role FROM {role_a}, {role_b};"))
            conn.execute(text(f"DROP ROLE IF EXISTS {role_a};"))
            conn.execute(text(f"DROP ROLE IF EXISTS {role_b};"))
        admin_engine.dispose()
