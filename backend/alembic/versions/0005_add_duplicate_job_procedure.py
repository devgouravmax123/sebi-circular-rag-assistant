"""add_duplicate_job_procedure

Revision ID: 0005_add_duplicate_job_procedure
Revises: 0004_add_worker_security_foundation
Create Date: 2026-10-31 15:00:00.000000

Implements the dedicated secure duplicate-job transition stored procedure:
app.mark_duplicate_job

1. Transitions ingestion_jobs.status to terminal 'duplicate'.
2. Sets stage to 'finished', completed_at to current timestamp.
3. Clears claim credentials (claim_token, claim_token_hash, claim_worker_id, claim_instance_id, lease_expires_at).
4. Atomically cleans up any unindexed processing version (and cascades to chunks/embeddings)
   belonging to the duplicate job without touching indexed documents/versions.
5. Preserves restricted-worker security: SECURITY DEFINER owned by worker_proc_owner,
   search_path set to app, public, pg_temp, granted only to ingestion_worker_role,
   revoked from PUBLIC and app_user.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0005_add_duplicate_job_procedure"
down_revision: Union[str, None] = "0004_add_worker_security_foundation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -------------------------------------------------------------------------
    # 1. Stored Procedure: app.mark_duplicate_job
    # -------------------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.mark_duplicate_job(
            p_job_id UUID,
            p_raw_claim_token TEXT,
            p_error_details JSONB DEFAULT NULL
        )
        RETURNS VARCHAR(32)
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = app, public, pg_temp
        AS $$
        DECLARE
            v_job RECORD;
            v_token_hash VARCHAR(64);
            v_details JSONB;
        BEGIN
            IF p_job_id IS NULL OR p_raw_claim_token IS NULL THEN
                RAISE EXCEPTION 'INVALID_ARGUMENT: Job ID and raw claim token must be provided'
                    USING ERRCODE = '22004';
            END IF;

            -- 1. Lock authoritative job record
            SELECT id, document_id, version_id, status, claim_worker_id, claim_token_hash, lease_expires_at
            INTO v_job
            FROM public.ingestion_jobs
            WHERE id = p_job_id
            FOR UPDATE;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'JOB_NOT_FOUND: Ingestion job % does not exist', p_job_id
                    USING ERRCODE = 'P0002';
            END IF;

            -- 2. Validate claim token hash
            v_token_hash := encode(sha256(p_raw_claim_token::bytea), 'hex');
            IF v_job.claim_token_hash IS DISTINCT FROM v_token_hash THEN
                RAISE EXCEPTION 'UNAUTHORIZED_WORKER: Claim token mismatch for job %', p_job_id
                    USING ERRCODE = '42501';
            END IF;

            -- 3. Validate job is in processing status
            IF v_job.status <> 'processing' THEN
                RAISE EXCEPTION 'INVALID_JOB_STATUS: Job % is in status %, expected processing', p_job_id, v_job.status
                    USING ERRCODE = '22023';
            END IF;

            -- 4. Validate lease is still active
            IF v_job.lease_expires_at IS NULL OR v_job.lease_expires_at < clock_timestamp() THEN
                RAISE EXCEPTION 'LEASE_EXPIRED: Worker lease expired for job %', p_job_id
                    USING ERRCODE = '55000';
            END IF;

            -- 5. Safe partial processing version cleanup
            -- If this job created an unindexed processing version, delete it (cascades to chunks & embeddings).
            -- Never delete indexed versions or original document records.
            IF v_job.version_id IS NOT NULL THEN
                DELETE FROM public.document_versions
                WHERE id = v_job.version_id AND extraction_status = 'processing';
            END IF;

            -- 6. Prepare sanitized error details with DUPLICATE_DOCUMENT code
            IF p_error_details IS NULL THEN
                v_details := jsonb_build_object(
                    'code', 'DUPLICATE_DOCUMENT',
                    'reason', 'Duplicate document content detected; job marked terminal duplicate.',
                    'marked_at', clock_timestamp()
                );
            ELSE
                v_details := p_error_details;
                IF NOT (v_details ? 'code') THEN
                    v_details := v_details || jsonb_build_object('code', 'DUPLICATE_DOCUMENT');
                END IF;
            END IF;

            -- 7. Atomic update to terminal duplicate status
            UPDATE public.ingestion_jobs
            SET status = 'duplicate',
                stage = 'finished',
                completed_at = clock_timestamp(),
                claim_worker_id = NULL,
                claim_instance_id = NULL,
                claim_token = NULL,
                claim_token_hash = NULL,
                lease_expires_at = NULL,
                version_id = NULL,
                error_details = v_details
            WHERE id = p_job_id;

            RETURN 'duplicate';
        END;
        $$;
        """
    )

    # -------------------------------------------------------------------------
    # 2. Procedure Ownership and Execution Grants
    # -------------------------------------------------------------------------
    op.execute("ALTER FUNCTION app.mark_duplicate_job(UUID, TEXT, JSONB) OWNER TO worker_proc_owner;")
    op.execute("REVOKE ALL ON FUNCTION app.mark_duplicate_job(UUID, TEXT, JSONB) FROM PUBLIC;")
    op.execute("GRANT EXECUTE ON FUNCTION app.mark_duplicate_job(UUID, TEXT, JSONB) TO ingestion_worker_role;")
    op.execute("REVOKE EXECUTE ON FUNCTION app.mark_duplicate_job(UUID, TEXT, JSONB) FROM app_user;")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app.mark_duplicate_job(UUID, TEXT, JSONB);")
