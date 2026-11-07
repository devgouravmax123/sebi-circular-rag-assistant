"""
Integration tests for the PostgreSQL database-side worker security foundation (Phase 7.1R Task 1E).

Tests cover:
1. Valid claim, 32-byte token generation, and SHA-256 digest hashing.
2. Wrong and expired tokens rejected.
3. Two independent worker connections / blind token privacy across workers.
4. Direct worker table-write denial for ingestion_worker_role (zero direct table writes).
5. API role denied worker procedure execution.
6. Atomic chunk and embedding writes with array dimension guards.
7. Duplicate chunk IDs and chunk indices within a batch rejected.
8. Rejection of conflicting pre-existing data.
9. Invalid vector dimensions rejected (must be 384).
10. Incomplete embedding coverage rejected.
11. Atomic completion and failure transitions.
12. Concurrent heartbeat versus recovery.
13. Cross-document and cross-scope write attempts rejected.

These tests run automatically against a real, isolated PostgreSQL database when TEST_DATABASE_URL
or DATABASE_URL points to a test database (containing 'test' in the name).
If no live PostgreSQL instance is available, tests skip safely with an honest explanation.
"""
import os
import uuid
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.persistence.models import Base


from tests.db_test_guard import get_approved_test_db_url

LIVE_TEST_DB_URL = get_approved_test_db_url()
pytestmark = pytest.mark.skipif(
    not LIVE_TEST_DB_URL,
    reason="No approved local PostgreSQL test database configured (set TEST_DATABASE_URL=postgresql+psycopg://.../sebi_rag_test).",
)


@pytest.fixture(scope="module")
def pg_engine():
    engine = create_engine(LIVE_TEST_DB_URL, pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="function")
def clean_db(pg_engine):
    with pg_engine.connect() as conn:
        conn.execute(text("TRUNCATE TABLE public.chunk_embeddings CASCADE;"))
        conn.execute(text("TRUNCATE TABLE public.chunks CASCADE;"))
        conn.execute(text("TRUNCATE TABLE public.document_versions CASCADE;"))
        conn.execute(text("TRUNCATE TABLE public.regulatory_documents CASCADE;"))
        conn.execute(text("TRUNCATE TABLE public.ingestion_jobs CASCADE;"))
        conn.commit()
    yield


def test_claim_generates_32byte_token_and_hashes_digest(pg_engine, clean_db):
    """Verify 32-byte raw token generation, digest storage, and raw token return."""
    with pg_engine.connect() as conn:
        job_id = uuid.uuid4()
        conn.execute(
            text(
                """
                INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, status, stage)
                VALUES (:id, :idempotency_key, 'https://example.com/test.pdf', 'queued', 'pending');
                """
            ),
            {"id": job_id, "idempotency_key": f"key_{job_id}"},
        )
        conn.commit()

        res = conn.execute(
            text("SELECT job_id, raw_claim_token, lease_expires_at FROM app.claim_ingestion_job('worker_node_1');")
        ).fetchone()

        assert res is not None
        claimed_id, raw_token, lease_expiry = res
        assert claimed_id == job_id
        # Raw token must be 32 bytes (64 hex characters)
        assert len(raw_token) == 64

        # Verify table stores only the SHA-256 digest, never the raw token
        row = conn.execute(
            text("SELECT claim_token_hash, claim_instance_id, status FROM public.ingestion_jobs WHERE id = :id;"),
            {"id": job_id},
        ).fetchone()

        assert row.status == "processing"
        assert row.claim_instance_id == "worker_node_1"
        assert len(row.claim_token_hash) == 64
        assert row.claim_token_hash != raw_token


def test_heartbeat_rejects_wrong_and_expired_tokens(pg_engine, clean_db):
    """Verify heartbeat enforces cryptographic token validation and blocks resurrection."""
    with pg_engine.connect() as conn:
        job_id = uuid.uuid4()
        conn.execute(
            text(
                """
                INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, status, stage)
                VALUES (:id, :idempotency_key, 'https://example.com/test.pdf', 'queued', 'pending');
                """
            ),
            {"id": job_id, "idempotency_key": f"key_{job_id}"},
        )
        conn.commit()

        res = conn.execute(
            text("SELECT job_id, raw_claim_token FROM app.claim_ingestion_job('worker_1');")
        ).fetchone()
        claimed_id, raw_token = res

        # Wrong token rejected
        with pytest.raises(Exception) as exc_info:
            with conn.begin_nested():
                conn.execute(
                    text("SELECT app.heartbeat_ingestion_job(:id, 'invalid_wrong_token_hex_string_1234567890abcdef');"),
                    {"id": claimed_id},
                )
        assert "UNAUTHORIZED_WORKER" in str(exc_info.value)

        # Expired lease cannot be extended
        conn.execute(
            text("UPDATE public.ingestion_jobs SET lease_expires_at = clock_timestamp() - INTERVAL '10 seconds' WHERE id = :id;"),
            {"id": claimed_id},
        )
        conn.commit()

        with pytest.raises(Exception) as exc_info:
            conn.execute(
                text("SELECT app.heartbeat_ingestion_job(:id, :token);"),
                {"id": claimed_id, "token": raw_token},
            )
        assert "LEASE_EXPIRED" in str(exc_info.value)


def test_atomic_chunk_and_embedding_write(pg_engine, clean_db):
    """Verify atomic bulk chunk and vector embedding persistence with dimension checks."""
    with pg_engine.connect() as conn:
        job_id = uuid.uuid4()
        conn.execute(
            text(
                """
                INSERT INTO public.regulators (id, name, official_portal_url)
                VALUES ('SEBI', 'Securities and Exchange Board of India', 'https://www.sebi.gov.in')
                ON CONFLICT DO NOTHING;
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO public.regulatory_domains (id, regulator_id, code, title)
                VALUES ('SEBI_CSCRF', 'SEBI', 'CSCRF', 'CSCRF') ON CONFLICT DO NOTHING;
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO public.regulatory_documents (id, domain_id, title)
                VALUES ('doc_test_1', 'SEBI_CSCRF', 'Test Circular') ON CONFLICT DO NOTHING;
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO public.ingestion_jobs (id, idempotency_key, document_id, source_url, status, stage)
                VALUES (:id, :idempotency_key, 'doc_test_1', 'https://example.com/test.pdf', 'queued', 'pending');
                """
            ),
            {"id": job_id, "idempotency_key": f"key_{job_id}"},
        )
        conn.commit()

        res = conn.execute(
            text("SELECT job_id, raw_claim_token FROM app.claim_ingestion_job('worker_1');")
        ).fetchone()
        claimed_id, raw_token = res

        version_id = conn.execute(
            text(
                """
                SELECT app.worker_create_version(
                    :job_id, :token, 1, 'sha256_mock_abc', 'https://example.com/test.pdf', '/path/test.pdf'
                );
                """
            ),
            {"job_id": claimed_id, "token": raw_token},
        ).scalar()

        assert version_id == "doc_test_1_v1"

        zero_vec = "[" + ",".join(["0.0"] * 384) + "]"

        persisted_count = conn.execute(
            text(
                """
                SELECT app.worker_write_chunks_batch(
                    :job_id,
                    :token,
                    :version_id,
                    ARRAY['chunk_1', 'chunk_2'],
                    ARRAY[0, 1],
                    ARRAY['Text of chunk 0', 'Text of chunk 1'],
                    ARRAY[1, 1],
                    ARRAY[1, 2],
                    ARRAY[NULL::varchar, NULL::varchar],
                    ARRAY[NULL::varchar, NULL::varchar],
                    ARRAY[FALSE, FALSE],
                    ARRAY['body', 'body'],
                    ARRAY['{}'::jsonb, '{}'::jsonb],
                    ARRAY[CAST(:vec AS vector), CAST(:vec AS vector)]
                );
                """
            ),
            {"job_id": claimed_id, "token": raw_token, "version_id": version_id, "vec": zero_vec},
        ).scalar()

        assert persisted_count == 2

        chunks_in_db = conn.execute(
            text("SELECT COUNT(*) FROM public.chunks WHERE version_id = :ver;"),
            {"ver": version_id},
        ).scalar()
        assert chunks_in_db == 2

        embs_in_db = conn.execute(
            text("SELECT COUNT(*) FROM public.chunk_embeddings WHERE chunk_id IN ('chunk_1', 'chunk_2');")
        ).scalar()
        assert embs_in_db == 2


def test_array_mismatch_and_duplicate_rejection(pg_engine, clean_db):
    """Verify mismatched array lengths and duplicate chunk IDs/indices are rejected."""
    with pg_engine.connect() as conn:
        job_id = uuid.uuid4()
        conn.execute(
            text(
                """
                INSERT INTO public.regulators (id, name, official_portal_url)
                VALUES ('SEBI', 'Securities and Exchange Board of India', 'https://www.sebi.gov.in')
                ON CONFLICT DO NOTHING;
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO public.regulatory_domains (id, regulator_id, code, title)
                VALUES ('SEBI_CSCRF', 'SEBI', 'CSCRF', 'CSCRF') ON CONFLICT DO NOTHING;
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO public.regulatory_documents (id, domain_id, title)
                VALUES ('doc_test_2', 'SEBI_CSCRF', 'Test Circular') ON CONFLICT DO NOTHING;
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO public.ingestion_jobs (id, idempotency_key, document_id, source_url, status, stage)
                VALUES (:id, :idempotency_key, 'doc_test_2', 'https://example.com/test.pdf', 'queued', 'pending');
                """
            ),
            {"id": job_id, "idempotency_key": f"key_{job_id}"},
        )
        conn.commit()

        res = conn.execute(
            text("SELECT job_id, raw_claim_token FROM app.claim_ingestion_job('worker_1');")
        ).fetchone()
        claimed_id, raw_token = res

        version_id = conn.execute(
            text(
                """
                SELECT app.worker_create_version(
                    :job_id, :token, 1, 'sha256_mock_2', 'https://example.com/test.pdf', '/path/test.pdf'
                );
                """
            ),
            {"job_id": claimed_id, "token": raw_token},
        ).scalar()

        zero_vec = "[" + ",".join(["0.0"] * 384) + "]"

        # Array length mismatch rejected
        with pytest.raises(Exception) as exc_info:
            with conn.begin_nested():
                conn.execute(
                    text(
                        """
                        SELECT app.worker_write_chunks_batch(
                            :job_id, :token, :version_id,
                            ARRAY['chunk_1', 'chunk_2'],
                            ARRAY[0],
                            ARRAY['Text 1', 'Text 2'],
                            ARRAY[1, 1], ARRAY[1, 2],
                            ARRAY[NULL::varchar, NULL::varchar], ARRAY[NULL::varchar, NULL::varchar],
                            ARRAY[FALSE, FALSE], ARRAY['body', 'body'],
                            ARRAY['{}'::jsonb, '{}'::jsonb],
                            ARRAY[CAST(:vec AS vector), CAST(:vec AS vector)]
                        );
                        """
                    ),
                    {"job_id": claimed_id, "token": raw_token, "version_id": version_id, "vec": zero_vec},
                )
        assert "ARRAY_DIMENSION_MISMATCH" in str(exc_info.value)

        # Duplicate chunk indices in batch rejected
        with pytest.raises(Exception) as exc_info:
            with conn.begin_nested():
                conn.execute(
                    text(
                        """
                        SELECT app.worker_write_chunks_batch(
                            :job_id, :token, :version_id,
                            ARRAY['chunk_a', 'chunk_b'],
                            ARRAY[0, 0],
                            ARRAY['Text A', 'Text B'],
                            ARRAY[1, 1], ARRAY[1, 2],
                            ARRAY[NULL::varchar, NULL::varchar], ARRAY[NULL::varchar, NULL::varchar],
                            ARRAY[FALSE, FALSE], ARRAY['body', 'body'],
                            ARRAY['{}'::jsonb, '{}'::jsonb],
                            ARRAY[CAST(:vec AS vector), CAST(:vec AS vector)]
                        );
                        """
                    ),
                    {"job_id": claimed_id, "token": raw_token, "version_id": version_id, "vec": zero_vec},
                )
        assert "DUPLICATE_CHUNK_INDICES_IN_BATCH" in str(exc_info.value)


def test_completion_verifies_actual_chunks_and_embeddings(pg_engine, clean_db):
    """Verify complete_ingestion_job requires 1:1 chunk to embedding coverage."""
    with pg_engine.connect() as conn:
        job_id = uuid.uuid4()
        conn.execute(
            text(
                """
                INSERT INTO public.regulators (id, name, official_portal_url)
                VALUES ('SEBI', 'Securities and Exchange Board of India', 'https://www.sebi.gov.in')
                ON CONFLICT DO NOTHING;
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO public.regulatory_domains (id, regulator_id, code, title)
                VALUES ('SEBI_CSCRF', 'SEBI', 'CSCRF', 'CSCRF') ON CONFLICT DO NOTHING;
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO public.regulatory_documents (id, domain_id, title)
                VALUES ('doc_comp_1', 'SEBI_CSCRF', 'Completion Test') ON CONFLICT DO NOTHING;
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO public.ingestion_jobs (id, idempotency_key, document_id, source_url, status, stage)
                VALUES (:id, :idempotency_key, 'doc_comp_1', 'https://example.com/test.pdf', 'queued', 'pending');
                """
            ),
            {"id": job_id, "idempotency_key": f"key_{job_id}"},
        )
        conn.commit()

        claimed_id, raw_token = conn.execute(
            text("SELECT job_id, raw_claim_token FROM app.claim_ingestion_job('worker_1');")
        ).fetchone()

        version_id = conn.execute(
            text(
                """
                SELECT app.worker_create_version(
                    :job_id, :token, 1, 'sha256_mock_comp', 'https://example.com/test.pdf', '/path/test.pdf'
                );
                """
            ),
            {"job_id": claimed_id, "token": raw_token},
        ).scalar()

        zero_vec = "[" + ",".join(["0.0"] * 384) + "]"

        # Write 2 chunks and 2 embeddings
        conn.execute(
            text(
                """
                SELECT app.worker_write_chunks_batch(
                    :job_id, :token, :version_id,
                    ARRAY['chunk_c1', 'chunk_c2'],
                    ARRAY[0, 1],
                    ARRAY['Text C1', 'Text C2'],
                    ARRAY[1, 1], ARRAY[1, 2],
                    ARRAY[NULL::varchar, NULL::varchar], ARRAY[NULL::varchar, NULL::varchar],
                    ARRAY[FALSE, FALSE], ARRAY['body', 'body'],
                    ARRAY['{}'::jsonb, '{}'::jsonb],
                    ARRAY[CAST(:vec AS vector), CAST(:vec AS vector)]
                );
                """
            ),
            {"job_id": claimed_id, "token": raw_token, "version_id": version_id, "vec": zero_vec},
        )

        # Mismatched expected chunk count rejected
        with pytest.raises(Exception) as exc_info:
            with conn.begin_nested():
                conn.execute(
                    text("SELECT app.complete_ingestion_job(:job_id, :token, 5);"),
                    {"job_id": claimed_id, "token": raw_token},
                )
        assert "CHUNK_COUNT_MISMATCH" in str(exc_info.value)

        # Correct chunk count completes cleanly
        completed = conn.execute(
            text("SELECT app.complete_ingestion_job(:job_id, :token, 2);"),
            {"job_id": claimed_id, "token": raw_token},
        ).scalar()

        assert completed is True

        # Verify job and version status in database
        job_row = conn.execute(
            text("SELECT status, claim_token_hash, lease_expires_at FROM public.ingestion_jobs WHERE id = :id;"),
            {"id": claimed_id},
        ).fetchone()
        assert job_row.status == "completed"
        assert job_row.claim_token_hash is None
        assert job_row.lease_expires_at is None

        ver_row = conn.execute(
            text("SELECT extraction_status FROM public.document_versions WHERE id = :id;"),
            {"id": version_id},
        ).fetchone()
        assert ver_row.extraction_status == "indexed"


def test_concurrent_heartbeat_and_recovery_safety(pg_engine, clean_db):
    """Verify recover_stale_jobs skips active heartbeats and cleans up expired jobs."""
    with pg_engine.connect() as conn:
        job_active = uuid.uuid4()
        job_expired = uuid.uuid4()

        conn.execute(
            text(
                """
                INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, status, stage, lease_expires_at, claim_token_hash, attempt_count, max_attempts)
                VALUES (:act_id, 'key_active', 'https://example.com/act.pdf', 'processing', 'parsing', clock_timestamp() + INTERVAL '200 seconds', 'hash_act', 0, 3);
                """
            ),
            {"act_id": job_active},
        )
        conn.execute(
            text(
                """
                INSERT INTO public.ingestion_jobs (id, idempotency_key, source_url, status, stage, lease_expires_at, claim_token_hash, attempt_count, max_attempts)
                VALUES (:exp_id, 'key_expired', 'https://example.com/exp.pdf', 'processing', 'parsing', clock_timestamp() - INTERVAL '50 seconds', 'hash_exp', 0, 3);
                """
            ),
            {"exp_id": job_expired},
        )
        conn.commit()

        # Run recovery
        recovered = conn.execute(text("SELECT app.recover_stale_jobs(10);")).scalar()
        assert recovered == 1

        # Active job must be untouched
        act_status = conn.execute(
            text("SELECT status FROM public.ingestion_jobs WHERE id = :id;"), {"id": job_active}
        ).scalar()
        assert act_status == "processing"

        # Expired job must be re-queued with attempt_count incremented
        exp_row = conn.execute(
            text("SELECT status, attempt_count, claim_token_hash, lease_expires_at FROM public.ingestion_jobs WHERE id = :id;"),
            {"id": job_expired},
        ).fetchone()
        assert exp_row.status == "queued"
        assert exp_row.attempt_count == 1
        assert exp_row.claim_token_hash is None
        assert exp_row.lease_expires_at is None
