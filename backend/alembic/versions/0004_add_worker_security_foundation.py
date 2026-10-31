"""add_worker_security_foundation

Revision ID: 0004_add_worker_security_foundation
Revises: 0003_expand_regulatory_edge_management
Create Date: 2026-10-30 19:00:00.000000

Implements the database-side worker security foundation:
1. Adds lease_expires_at, claim_token_hash, version_id, and claim_instance_id to ingestion_jobs.
2. Creates the app schema.
3. Provisions role hierarchy: auth_proc_owner, worker_proc_owner, ingestion_worker_role, app_user.
4. Creates stored procedures:
   - app.claim_ingestion_job (generates 32-byte cryptographically secure random claim token, stores SHA-256 digest, returns raw token)
   - app.heartbeat_ingestion_job (verifies SHA-256 digest, validates lease, extends bounded lease)
   - app.worker_create_version (authoritative document version creation)
   - app.worker_write_chunks_batch (atomic set-based chunk and vector embedding persistence with strict dimension and array validation)
   - app.complete_ingestion_job (atomic completion verifying actual chunk count, 1:1 embedding coverage, and status transitions)
   - app.fail_ingestion_job (atomic failure/retry with lease validation and partial data cleanup)
   - app.recover_stale_jobs (safe concurrent recovery using FOR UPDATE SKIP LOCKED)
5. Enforces strict schema, function, and table privileges with zero direct table write grants for ingestion_worker_role.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0004_add_worker_security_foundation"
down_revision: Union[str, None] = "0003_expand_regulatory_edge_management"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Ensure pgcrypto is installed for gen_random_bytes
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;")

    # -------------------------------------------------------------------------
    # 1. Alter ingestion_jobs with authoritative lease and hashed claim fields
    # -------------------------------------------------------------------------
    op.add_column(
        "ingestion_jobs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column("claim_token_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column("claim_instance_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column("version_id", sa.String(length=96), nullable=True),
    )

    # Migrate any legacy claim_token UUID values to claim_token_hash if existing
    op.execute(
        """
        UPDATE public.ingestion_jobs
        SET claim_token_hash = encode(sha256(claim_token::text::bytea), 'hex')
        WHERE claim_token IS NOT NULL AND claim_token_hash IS NULL;
        """
    )

    op.create_index(
        "idx_ingestion_jobs_lease",
        "ingestion_jobs",
        ["status", "lease_expires_at"],
    )

    # -------------------------------------------------------------------------
    # 2. Schema and Role Provisioning
    # -------------------------------------------------------------------------
    op.execute("CREATE SCHEMA IF NOT EXISTS app;")

    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'worker_proc_owner') THEN
                CREATE ROLE worker_proc_owner WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'auth_proc_owner') THEN
                CREATE ROLE auth_proc_owner WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ingestion_worker_role') THEN
                CREATE ROLE ingestion_worker_role WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
                CREATE ROLE app_user WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
            END IF;
        END
        $$;
        """
    )

    # -------------------------------------------------------------------------
    # 3. Create Hardened Stored Procedures under schema app
    # -------------------------------------------------------------------------

    # 3.1 app.claim_ingestion_job
    # Generates a 32-byte cryptographically secure random claim token using gen_random_bytes(32)
    # Computes SHA-256 digest, stores digest in table, returns raw token only to caller
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.claim_ingestion_job(
            p_instance_id VARCHAR(64) DEFAULT NULL,
            p_lease_duration INTERVAL DEFAULT INTERVAL '300 seconds'
        )
        RETURNS TABLE (
            job_id UUID,
            raw_claim_token TEXT,
            document_id VARCHAR(64),
            source_url TEXT,
            lease_expires_at TIMESTAMPTZ
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = app, public, pg_temp
        AS $$
        DECLARE
            v_job RECORD;
            v_raw_token_bytes BYTEA;
            v_raw_token TEXT;
            v_token_hash VARCHAR(64);
            v_lease TIMESTAMPTZ;
        BEGIN
            -- Select next eligible queued job using FOR UPDATE SKIP LOCKED
            SELECT j.id, j.document_id, j.source_url
            INTO v_job
            FROM public.ingestion_jobs j
            WHERE j.status = 'queued'
              AND (j.next_retry_at IS NULL OR j.next_retry_at <= clock_timestamp())
            ORDER BY j.created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1;

            IF NOT FOUND THEN
                RETURN;
            END IF;

            -- 1. Cryptographically secure 32-byte (256-bit) raw claim token
            v_raw_token_bytes := gen_random_bytes(32);
            v_raw_token := encode(v_raw_token_bytes, 'hex');

            -- 2. Store only SHA-256 digest
            v_token_hash := encode(sha256(v_raw_token::bytea), 'hex');

            -- 3. Calculate bounded lease (capped at 600s)
            v_lease := clock_timestamp() + LEAST(p_lease_duration, INTERVAL '600 seconds');

            UPDATE public.ingestion_jobs
            SET status = 'processing',
                stage = 'pending',
                claim_worker_id = session_user::varchar(64),
                claim_instance_id = NULLIF(TRIM(p_instance_id), ''),
                claim_token_hash = v_token_hash,
                heartbeat_at = clock_timestamp(),
                lease_expires_at = v_lease,
                started_at = COALESCE(started_at, clock_timestamp())
            WHERE id = v_job.id;

            RETURN QUERY SELECT
                v_job.id,
                v_raw_token,
                v_job.document_id,
                v_job.source_url,
                v_lease;
        END;
        $$;
        """
    )

    # 3.2 app.heartbeat_ingestion_job
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.heartbeat_ingestion_job(
            p_job_id UUID,
            p_raw_claim_token TEXT,
            p_stage VARCHAR(32) DEFAULT NULL,
            p_lease_duration INTERVAL DEFAULT INTERVAL '300 seconds'
        )
        RETURNS TIMESTAMPTZ
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = app, public, pg_temp
        AS $$
        DECLARE
            v_job RECORD;
            v_token_hash VARCHAR(64);
            v_new_expiry TIMESTAMPTZ;
        BEGIN
            IF p_job_id IS NULL OR p_raw_claim_token IS NULL OR TRIM(p_raw_claim_token) = '' THEN
                RAISE EXCEPTION 'INVALID_ARGUMENT: Job ID and raw claim token must be provided'
                    USING ERRCODE = '22004';
            END IF;

            SELECT id, status, claim_token_hash, lease_expires_at, stage
            INTO v_job
            FROM public.ingestion_jobs
            WHERE id = p_job_id
            FOR UPDATE;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'JOB_NOT_FOUND: Ingestion job % does not exist', p_job_id
                    USING ERRCODE = 'P0002';
            END IF;

            v_token_hash := encode(sha256(p_raw_claim_token::bytea), 'hex');
            IF v_job.claim_token_hash IS DISTINCT FROM v_token_hash THEN
                RAISE EXCEPTION 'UNAUTHORIZED_WORKER: Claim token digest mismatch for job %', p_job_id
                    USING ERRCODE = '42501';
            END IF;

            IF v_job.status <> 'processing' THEN
                RAISE EXCEPTION 'INVALID_JOB_STATUS: Cannot heartbeat job in % status', v_job.status
                    USING ERRCODE = '22023';
            END IF;

            -- Prevent lease resurrection after physical expiration
            IF v_job.lease_expires_at IS NULL OR v_job.lease_expires_at <= clock_timestamp() THEN
                RAISE EXCEPTION 'LEASE_EXPIRED: Cannot extend expired lease for job %', p_job_id
                    USING ERRCODE = '55000';
            END IF;

            v_new_expiry := LEAST(clock_timestamp() + p_lease_duration, clock_timestamp() + INTERVAL '600 seconds');

            UPDATE public.ingestion_jobs
            SET heartbeat_at = clock_timestamp(),
                lease_expires_at = v_new_expiry,
                stage = COALESCE(p_stage, stage)
            WHERE id = p_job_id;

            RETURN v_new_expiry;
        END;
        $$;
        """
    )

    # 3.3 app.worker_create_version
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.worker_create_version(
            p_job_id UUID,
            p_raw_claim_token TEXT,
            p_version_number INT,
            p_sha256 VARCHAR(64),
            p_source_url TEXT,
            p_storage_path TEXT,
            p_issue_date DATE DEFAULT NULL,
            p_effective_date DATE DEFAULT NULL
        )
        RETURNS VARCHAR(96)
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = app, public, pg_temp
        AS $$
        DECLARE
            v_job RECORD;
            v_token_hash VARCHAR(64);
            v_expected_version_id VARCHAR(96);
            v_existing_version RECORD;
        BEGIN
            IF p_job_id IS NULL OR p_raw_claim_token IS NULL OR p_version_number IS NULL OR p_sha256 IS NULL THEN
                RAISE EXCEPTION 'INVALID_ARGUMENT: Required version parameters must not be NULL'
                    USING ERRCODE = '22004';
            END IF;

            SELECT id, document_id, status, claim_token_hash, lease_expires_at
            INTO v_job
            FROM public.ingestion_jobs
            WHERE id = p_job_id
            FOR UPDATE;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'JOB_NOT_FOUND: Ingestion job % does not exist', p_job_id
                    USING ERRCODE = 'P0002';
            END IF;

            v_token_hash := encode(sha256(p_raw_claim_token::bytea), 'hex');
            IF v_job.claim_token_hash IS DISTINCT FROM v_token_hash THEN
                RAISE EXCEPTION 'UNAUTHORIZED_WORKER: Claim token mismatch for job %', p_job_id
                    USING ERRCODE = '42501';
            END IF;

            IF v_job.status <> 'processing' THEN
                RAISE EXCEPTION 'INVALID_JOB_STATUS: Job % is in status %, expected processing', p_job_id, v_job.status
                    USING ERRCODE = '22023';
            END IF;

            IF v_job.lease_expires_at IS NULL OR v_job.lease_expires_at < clock_timestamp() + INTERVAL '30 seconds' THEN
                RAISE EXCEPTION 'LEASE_EXPIRED: Worker lease expired or within safety margin for job %', p_job_id
                    USING ERRCODE = '55000';
            END IF;

            IF v_job.document_id IS NULL THEN
                RAISE EXCEPTION 'JOB_DOCUMENT_MISSING: Ingestion job % has no assigned document_id', p_job_id
                    USING ERRCODE = '23502';
            END IF;

            v_expected_version_id := v_job.document_id || '_v' || p_version_number::text;

            -- Check for existing version record
            SELECT id, document_id, version_number, sha256, extraction_status
            INTO v_existing_version
            FROM public.document_versions
            WHERE id = v_expected_version_id
            FOR UPDATE;

            IF FOUND THEN
                IF v_existing_version.document_id <> v_job.document_id OR v_existing_version.sha256 <> p_sha256 THEN
                    RAISE EXCEPTION 'VERSION_COLLISION_OWNERSHIP_MISMATCH: Version % exists with conflicting metadata', v_expected_version_id
                        USING ERRCODE = '23505';
                END IF;

                IF v_existing_version.extraction_status = 'indexed' THEN
                    RAISE EXCEPTION 'VERSION_ALREADY_INDEXED: Version % is immutable and already indexed', v_expected_version_id
                        USING ERRCODE = '55000';
                END IF;

                UPDATE public.document_versions
                SET storage_path = p_storage_path,
                    source_url = p_source_url,
                    issue_date = COALESCE(p_issue_date, issue_date),
                    effective_date = COALESCE(p_effective_date, effective_date),
                    retrieved_at = clock_timestamp()
                WHERE id = v_expected_version_id;
            ELSE
                INSERT INTO public.document_versions (
                    id,
                    document_id,
                    version_number,
                    source_url,
                    storage_path,
                    sha256,
                    issue_date,
                    effective_date,
                    extraction_status,
                    retrieved_at,
                    created_at
                ) VALUES (
                    v_expected_version_id,
                    v_job.document_id,
                    p_version_number,
                    p_source_url,
                    p_storage_path,
                    p_sha256,
                    p_issue_date,
                    p_effective_date,
                    'processing',
                    clock_timestamp(),
                    clock_timestamp()
                );
            END IF;

            UPDATE public.ingestion_jobs
            SET version_id = v_expected_version_id,
                sha256 = p_sha256
            WHERE id = p_job_id;

            RETURN v_expected_version_id;
        END;
        $$;
        """
    )

    # 3.4 app.worker_write_chunks_batch
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.worker_write_chunks_batch(
            p_job_id UUID,
            p_raw_claim_token TEXT,
            p_version_id VARCHAR(96),
            p_chunk_ids VARCHAR(96)[],
            p_chunk_indices INT[],
            p_texts TEXT[],
            p_pdf_starts INT[],
            p_pdf_ends INT[],
            p_printed_starts VARCHAR(32)[],
            p_printed_ends VARCHAR(32)[],
            p_is_ocrs BOOLEAN[],
            p_chunk_types VARCHAR(32)[],
            p_metadatas JSONB[],
            p_embeddings vector(384)[]
        )
        RETURNS INT
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = app, public, pg_temp
        AS $$
        DECLARE
            v_job RECORD;
            v_version RECORD;
            v_token_hash VARCHAR(64);
            v_count INT;
            v_distinct_ids INT;
            v_distinct_indices INT;
            v_existing_conflict INT;
        BEGIN
            -- 1. Input parameter validation
            IF p_job_id IS NULL OR p_raw_claim_token IS NULL OR p_version_id IS NULL THEN
                RAISE EXCEPTION 'INVALID_ARGUMENT: Job ID, claim token, and version ID must not be NULL'
                    USING ERRCODE = '22004';
            END IF;

            v_count := array_length(p_chunk_ids, 1);
            IF v_count IS NULL OR v_count = 0 THEN
                RETURN 0;
            END IF;

            -- 2. Strict dimension and length validation
            IF array_length(p_chunk_indices, 1) <> v_count OR
               array_length(p_texts, 1) <> v_count OR
               array_length(p_pdf_starts, 1) <> v_count OR
               array_length(p_pdf_ends, 1) <> v_count OR
               array_length(p_embeddings, 1) <> v_count THEN
                RAISE EXCEPTION 'ARRAY_DIMENSION_MISMATCH: Input array lengths must match chunk IDs length (%)', v_count
                    USING ERRCODE = '22023';
            END IF;

            -- Check for null required elements in parallel arrays
            IF EXISTS (SELECT 1 FROM unnest(p_chunk_ids) id WHERE id IS NULL) OR
               EXISTS (SELECT 1 FROM unnest(p_chunk_indices) idx WHERE idx IS NULL) OR
               EXISTS (SELECT 1 FROM unnest(p_texts) t WHERE t IS NULL) OR
               EXISTS (SELECT 1 FROM unnest(p_pdf_starts) s WHERE s IS NULL) OR
               EXISTS (SELECT 1 FROM unnest(p_pdf_ends) e WHERE e IS NULL) OR
               EXISTS (SELECT 1 FROM unnest(p_embeddings) emb WHERE emb IS NULL) THEN
                RAISE EXCEPTION 'NULL_ARRAY_ELEMENT_DISALLOWED: Required array elements cannot be NULL'
                    USING ERRCODE = '22004';
            END IF;

            -- Reject duplicate chunk IDs in the batch
            SELECT COUNT(DISTINCT id) INTO v_distinct_ids FROM unnest(p_chunk_ids) AS id;
            IF v_distinct_ids <> v_count THEN
                RAISE EXCEPTION 'DUPLICATE_CHUNK_IDS_IN_BATCH: Input batch contains duplicate chunk IDs'
                    USING ERRCODE = '23505';
            END IF;

            -- Reject duplicate chunk indices in the batch
            SELECT COUNT(DISTINCT idx) INTO v_distinct_indices FROM unnest(p_chunk_indices) AS idx;
            IF v_distinct_indices <> v_count THEN
                RAISE EXCEPTION 'DUPLICATE_CHUNK_INDICES_IN_BATCH: Input batch contains duplicate chunk indices'
                    USING ERRCODE = '23505';
            END IF;

            -- Validate embedding dimensions
            IF EXISTS (
                SELECT 1 FROM unnest(p_embeddings) AS emb WHERE vector_dims(emb) <> 384
            ) THEN
                RAISE EXCEPTION 'INVALID_EMBEDDING_DIMENSION: Every vector must have dimension 384'
                    USING ERRCODE = '22023';
            END IF;

            -- 3. Lock authoritative job record
            SELECT id, document_id, version_id, status, claim_worker_id, claim_token_hash, lease_expires_at
            INTO v_job
            FROM public.ingestion_jobs
            WHERE id = p_job_id
            FOR UPDATE;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'JOB_NOT_FOUND: Ingestion job % does not exist', p_job_id
                    USING ERRCODE = 'P0002';
            END IF;

            v_token_hash := encode(sha256(p_raw_claim_token::bytea), 'hex');
            IF v_job.claim_token_hash IS DISTINCT FROM v_token_hash THEN
                RAISE EXCEPTION 'UNAUTHORIZED_WORKER: Claim token mismatch for job %', p_job_id
                    USING ERRCODE = '42501';
            END IF;

            IF v_job.status <> 'processing' THEN
                RAISE EXCEPTION 'INVALID_JOB_STATUS: Job % is in status %, expected processing', p_job_id, v_job.status
                    USING ERRCODE = '22023';
            END IF;

            IF v_job.lease_expires_at IS NULL OR v_job.lease_expires_at < clock_timestamp() + INTERVAL '30 seconds' THEN
                RAISE EXCEPTION 'LEASE_EXPIRED: Worker lease expired or within safety margin for job %', p_job_id
                    USING ERRCODE = '55000';
            END IF;

            IF v_job.version_id IS DISTINCT FROM p_version_id THEN
                RAISE EXCEPTION 'VERSION_MISMATCH: Version % does not match assigned job version %', p_version_id, v_job.version_id
                    USING ERRCODE = '23503';
            END IF;

            -- 4. Lock and validate version ownership
            SELECT id, document_id, extraction_status
            INTO v_version
            FROM public.document_versions
            WHERE id = v_job.version_id
            FOR UPDATE;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'VERSION_NOT_FOUND: Version % does not exist', v_job.version_id
                    USING ERRCODE = 'P0002';
            END IF;

            IF v_version.extraction_status <> 'processing' THEN
                RAISE EXCEPTION 'VERSION_NOT_PROCESSING: Version % has status %, cannot write chunks',
                    v_version.id, v_version.extraction_status
                    USING ERRCODE = '55000';
            END IF;

            -- 5. Detect conflicting pre-existing data
            SELECT COUNT(*) INTO v_existing_conflict
            FROM public.chunks c
            WHERE c.version_id = v_job.version_id
              AND c.chunk_index = ANY(p_chunk_indices)
              AND c.id <> ALL(p_chunk_ids);

            IF v_existing_conflict > 0 THEN
                RAISE EXCEPTION 'CHUNK_CONFLICT_DETECTED: % chunk indices already exist with different IDs for version %',
                    v_existing_conflict, v_job.version_id
                    USING ERRCODE = '23505';
            END IF;

            -- 6. Atomic set-based insert into chunks
            INSERT INTO public.chunks (
                id,
                version_id,
                chunk_index,
                text,
                pdf_page_start,
                pdf_page_end,
                printed_page_start,
                printed_page_end,
                is_ocr,
                chunk_type,
                metadata_json,
                created_at
            )
            SELECT
                p_chunk_ids[i],
                v_job.version_id,
                p_chunk_indices[i],
                p_texts[i],
                p_pdf_starts[i],
                p_pdf_ends[i],
                p_printed_starts[i],
                p_printed_ends[i],
                COALESCE(p_is_ocrs[i], FALSE),
                COALESCE(p_chunk_types[i], 'body'),
                COALESCE(p_metadatas[i], '{}'::jsonb),
                clock_timestamp()
            FROM generate_subscripts(p_chunk_ids, 1) AS i
            ON CONFLICT (id) DO NOTHING;

            -- 7. Atomic set-based insert into chunk_embeddings
            INSERT INTO public.chunk_embeddings (
                chunk_id,
                model_name,
                embedding_dim,
                embedding,
                created_at
            )
            SELECT
                p_chunk_ids[i],
                'BAAI/bge-small-en-v1.5',
                384,
                p_embeddings[i],
                clock_timestamp()
            FROM generate_subscripts(p_chunk_ids, 1) AS i
            ON CONFLICT (chunk_id) DO UPDATE
            SET embedding = EXCLUDED.embedding,
                model_name = EXCLUDED.model_name;

            RETURN v_count;
        END;
        $$;
        """
    )

    # 3.5 app.complete_ingestion_job
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.complete_ingestion_job(
            p_job_id UUID,
            p_raw_claim_token TEXT,
            p_expected_chunk_count INT,
            p_allow_empty BOOLEAN DEFAULT FALSE
        )
        RETURNS BOOLEAN
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = app, public, pg_temp
        AS $$
        DECLARE
            v_job RECORD;
            v_token_hash VARCHAR(64);
            v_actual_chunks INT;
            v_actual_embeddings INT;
        BEGIN
            IF p_job_id IS NULL OR p_raw_claim_token IS NULL THEN
                RAISE EXCEPTION 'INVALID_ARGUMENT: Job ID and raw claim token must be provided'
                    USING ERRCODE = '22004';
            END IF;

            SELECT id, document_id, version_id, status, claim_token_hash, lease_expires_at
            INTO v_job
            FROM public.ingestion_jobs
            WHERE id = p_job_id
            FOR UPDATE;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'JOB_NOT_FOUND: Job % does not exist', p_job_id USING ERRCODE = 'P0002';
            END IF;

            v_token_hash := encode(sha256(p_raw_claim_token::bytea), 'hex');
            IF v_job.claim_token_hash IS DISTINCT FROM v_token_hash THEN
                RAISE EXCEPTION 'UNAUTHORIZED_WORKER: Claim token mismatch' USING ERRCODE = '42501';
            END IF;

            IF v_job.status <> 'processing' THEN
                RAISE EXCEPTION 'INVALID_JOB_STATUS: Job in status %', v_job.status USING ERRCODE = '22023';
            END IF;

            IF v_job.lease_expires_at IS NULL OR v_job.lease_expires_at < clock_timestamp() + INTERVAL '30 seconds' THEN
                RAISE EXCEPTION 'LEASE_EXPIRED: Cannot complete on expired lease' USING ERRCODE = '55000';
            END IF;

            -- Verify actual chunk count in database
            SELECT COUNT(*) INTO v_actual_chunks FROM public.chunks WHERE version_id = v_job.version_id;

            IF p_expected_chunk_count > 0 AND v_actual_chunks <> p_expected_chunk_count THEN
                RAISE EXCEPTION 'CHUNK_COUNT_MISMATCH: Expected %, found % in database', p_expected_chunk_count, v_actual_chunks
                    USING ERRCODE = '23514';
            END IF;

            IF v_actual_chunks = 0 AND NOT p_allow_empty THEN
                RAISE EXCEPTION 'EMPTY_DOCUMENT_DISALLOWED: Version has 0 chunks' USING ERRCODE = '23514';
            END IF;

            -- Verify 1:1 embedding coverage
            SELECT COUNT(ce.chunk_id) INTO v_actual_embeddings
            FROM public.chunk_embeddings ce
            JOIN public.chunks c ON c.id = ce.chunk_id
            WHERE c.version_id = v_job.version_id;

            IF v_actual_chunks <> v_actual_embeddings THEN
                RAISE EXCEPTION 'EMBEDDING_COVERAGE_INCOMPLETE: % chunks but % embeddings', v_actual_chunks, v_actual_embeddings
                    USING ERRCODE = '23514';
            END IF;

            -- Atomic status transitions
            UPDATE public.document_versions
            SET extraction_status = 'indexed'
            WHERE id = v_job.version_id;

            UPDATE public.ingestion_jobs
            SET status = 'completed',
                stage = 'finished',
                completed_at = clock_timestamp(),
                claim_worker_id = NULL,
                claim_instance_id = NULL,
                claim_token_hash = NULL,
                lease_expires_at = NULL,
                error_details = NULL
            WHERE id = p_job_id;

            RETURN TRUE;
        END;
        $$;
        """
    )

    # 3.6 app.fail_ingestion_job
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.fail_ingestion_job(
            p_job_id UUID,
            p_raw_claim_token TEXT,
            p_error_details JSONB,
            p_is_retryable BOOLEAN DEFAULT TRUE
        )
        RETURNS VARCHAR(32)
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = app, public, pg_temp
        AS $$
        DECLARE
            v_job RECORD;
            v_token_hash VARCHAR(64);
            v_new_status VARCHAR(32);
            v_new_stage VARCHAR(32);
            v_new_attempts INT;
        BEGIN
            IF p_job_id IS NULL OR p_raw_claim_token IS NULL THEN
                RAISE EXCEPTION 'INVALID_ARGUMENT: Job ID and raw claim token must be provided'
                    USING ERRCODE = '22004';
            END IF;

            SELECT id, version_id, attempt_count, max_attempts, status, claim_token_hash
            INTO v_job
            FROM public.ingestion_jobs
            WHERE id = p_job_id
            FOR UPDATE;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'JOB_NOT_FOUND: Job % does not exist', p_job_id USING ERRCODE = 'P0002';
            END IF;

            v_token_hash := encode(sha256(p_raw_claim_token::bytea), 'hex');
            IF v_job.claim_token_hash IS DISTINCT FROM v_token_hash THEN
                RAISE EXCEPTION 'UNAUTHORIZED_WORKER: Claim token mismatch' USING ERRCODE = '42501';
            END IF;

            IF v_job.status <> 'processing' THEN
                RAISE EXCEPTION 'INVALID_JOB_STATUS: Job in status %', v_job.status USING ERRCODE = '22023';
            END IF;

            v_new_attempts := v_job.attempt_count + 1;

            IF p_is_retryable AND v_new_attempts < v_job.max_attempts THEN
                v_new_status := 'queued';
                v_new_stage := 'pending';
            ELSE
                v_new_status := 'failed';
                v_new_stage := 'finished';

                -- Purge partial version chunks on permanent failure
                IF v_job.version_id IS NOT NULL THEN
                    DELETE FROM public.document_versions
                    WHERE id = v_job.version_id AND extraction_status = 'processing';
                END IF;
            END IF;

            UPDATE public.ingestion_jobs
            SET status = v_new_status,
                stage = v_new_stage,
                attempt_count = v_new_attempts,
                claim_worker_id = NULL,
                claim_instance_id = NULL,
                claim_token_hash = NULL,
                lease_expires_at = NULL,
                error_details = p_error_details,
                completed_at = CASE WHEN v_new_status = 'failed' THEN clock_timestamp() ELSE NULL END,
                next_retry_at = CASE WHEN v_new_status = 'queued' THEN clock_timestamp() + (INTERVAL '15 seconds' * POWER(2, v_new_attempts)) ELSE NULL END
            WHERE id = p_job_id;

            RETURN v_new_status;
        END;
        $$;
        """
    )

    # 3.7 app.recover_stale_jobs
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.recover_stale_jobs(
            p_batch_limit INT DEFAULT 50
        )
        RETURNS INT
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = app, public, pg_temp
        AS $$
        DECLARE
            v_rec RECORD;
            v_count INT := 0;
        BEGIN
            FOR v_rec IN
                SELECT id, version_id, attempt_count, max_attempts
                FROM public.ingestion_jobs
                WHERE status = 'processing'
                  AND lease_expires_at < clock_timestamp()
                ORDER BY heartbeat_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT LEAST(p_batch_limit, 100)
            LOOP
                IF v_rec.attempt_count + 1 >= v_rec.max_attempts THEN
                    UPDATE public.ingestion_jobs
                    SET status = 'failed',
                        stage = 'finished',
                        attempt_count = attempt_count + 1,
                        completed_at = clock_timestamp(),
                        claim_worker_id = NULL,
                        claim_instance_id = NULL,
                        claim_token_hash = NULL,
                        lease_expires_at = NULL,
                        error_details = jsonb_build_object(
                            'reason', 'Worker lease expired; max retry attempts reached',
                            'recovered_at', clock_timestamp()
                        )
                    WHERE id = v_rec.id;

                    IF v_rec.version_id IS NOT NULL THEN
                        DELETE FROM public.document_versions
                        WHERE id = v_rec.version_id AND extraction_status = 'processing';
                    END IF;
                ELSE
                    UPDATE public.ingestion_jobs
                    SET status = 'queued',
                        stage = 'pending',
                        attempt_count = attempt_count + 1,
                        claim_worker_id = NULL,
                        claim_instance_id = NULL,
                        claim_token_hash = NULL,
                        lease_expires_at = NULL,
                        next_retry_at = clock_timestamp() + (INTERVAL '15 seconds' * POWER(2, attempt_count + 1)),
                        error_details = jsonb_build_object(
                            'reason', 'Recovered from timed-out worker lease',
                            'recovered_at', clock_timestamp()
                        )
                    WHERE id = v_rec.id;
                END IF;

                v_count := v_count + 1;
            END LOOP;

            RETURN v_count;
        END;
        $$;
        """
    )

    # -------------------------------------------------------------------------
    # 4. Procedure Ownership and Execution Grants
    # -------------------------------------------------------------------------
    op.execute("ALTER FUNCTION app.claim_ingestion_job OWNER TO worker_proc_owner;")
    op.execute("ALTER FUNCTION app.heartbeat_ingestion_job OWNER TO worker_proc_owner;")
    op.execute("ALTER FUNCTION app.worker_create_version OWNER TO worker_proc_owner;")
    op.execute("ALTER FUNCTION app.worker_write_chunks_batch OWNER TO worker_proc_owner;")
    op.execute("ALTER FUNCTION app.complete_ingestion_job OWNER TO worker_proc_owner;")
    op.execute("ALTER FUNCTION app.fail_ingestion_job OWNER TO worker_proc_owner;")
    op.execute("ALTER FUNCTION app.recover_stale_jobs OWNER TO worker_proc_owner;")

    op.execute("REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app FROM PUBLIC;")
    op.execute("GRANT USAGE ON SCHEMA app, public TO ingestion_worker_role, app_user;")

    op.execute("GRANT EXECUTE ON FUNCTION app.claim_ingestion_job TO ingestion_worker_role;")
    op.execute("GRANT EXECUTE ON FUNCTION app.heartbeat_ingestion_job TO ingestion_worker_role;")
    op.execute("GRANT EXECUTE ON FUNCTION app.worker_create_version TO ingestion_worker_role;")
    op.execute("GRANT EXECUTE ON FUNCTION app.worker_write_chunks_batch TO ingestion_worker_role;")
    op.execute("GRANT EXECUTE ON FUNCTION app.complete_ingestion_job TO ingestion_worker_role;")
    op.execute("GRANT EXECUTE ON FUNCTION app.fail_ingestion_job TO ingestion_worker_role;")
    op.execute("GRANT EXECUTE ON FUNCTION app.recover_stale_jobs TO ingestion_worker_role;")

    # Explicitly deny app_user execution on all worker procedures
    op.execute(
        """
        REVOKE EXECUTE ON FUNCTION app.claim_ingestion_job FROM app_user;
        REVOKE EXECUTE ON FUNCTION app.heartbeat_ingestion_job FROM app_user;
        REVOKE EXECUTE ON FUNCTION app.worker_create_version FROM app_user;
        REVOKE EXECUTE ON FUNCTION app.worker_write_chunks_batch FROM app_user;
        REVOKE EXECUTE ON FUNCTION app.complete_ingestion_job FROM app_user;
        REVOKE EXECUTE ON FUNCTION app.fail_ingestion_job FROM app_user;
        REVOKE EXECUTE ON FUNCTION app.recover_stale_jobs FROM app_user;
        """
    )

    # -------------------------------------------------------------------------
    # 5. Table Privileges: Zero Direct Table Writes for Ingestion Worker Role
    # -------------------------------------------------------------------------
    op.execute(
        """
        GRANT SELECT ON TABLE public.regulators, public.regulatory_domains TO ingestion_worker_role, app_user;
        GRANT SELECT ON TABLE public.regulatory_documents, public.document_versions, public.chunks, public.chunk_embeddings, public.regulatory_edges TO ingestion_worker_role;
        GRANT SELECT ON TABLE public.ingestion_jobs TO ingestion_worker_role;

        REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE public.regulatory_documents, public.document_versions, public.chunks, public.chunk_embeddings, public.ingestion_jobs, public.regulatory_edges FROM ingestion_worker_role;

        -- worker_proc_owner internal table grants for stored procedures
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.regulatory_documents, public.document_versions, public.chunks, public.chunk_embeddings, public.ingestion_jobs TO worker_proc_owner;
        """
    )


def downgrade() -> None:
    # Drop procedures
    op.execute("DROP FUNCTION IF EXISTS app.recover_stale_jobs(INT);")
    op.execute("DROP FUNCTION IF EXISTS app.fail_ingestion_job(UUID, TEXT, JSONB, BOOLEAN);")
    op.execute("DROP FUNCTION IF EXISTS app.complete_ingestion_job(UUID, TEXT, INT, BOOLEAN);")
    op.execute("DROP FUNCTION IF EXISTS app.worker_write_chunks_batch(UUID, TEXT, VARCHAR, VARCHAR[], INT[], TEXT[], INT[], INT[], VARCHAR[], VARCHAR[], BOOLEAN[], VARCHAR[], JSONB[], vector[]);")
    op.execute("DROP FUNCTION IF EXISTS app.worker_create_version(UUID, TEXT, INT, VARCHAR, TEXT, TEXT, DATE, DATE);")
    op.execute("DROP FUNCTION IF EXISTS app.heartbeat_ingestion_job(UUID, TEXT, VARCHAR, INTERVAL);")
    op.execute("DROP FUNCTION IF EXISTS app.claim_ingestion_job(VARCHAR, INTERVAL);")

    # Drop index and columns
    op.drop_index("idx_ingestion_jobs_lease", table_name="ingestion_jobs")
    op.drop_column("ingestion_jobs", "version_id")
    op.drop_column("ingestion_jobs", "claim_instance_id")
    op.drop_column("ingestion_jobs", "claim_token_hash")
    op.drop_column("ingestion_jobs", "lease_expires_at")
