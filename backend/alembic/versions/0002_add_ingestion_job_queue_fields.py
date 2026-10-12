"""add_ingestion_job_queue_fields

Revision ID: 0002_add_ingestion_job_queue_fields
Revises: 0001_initial_persistence_foundation
Create Date: 2026-10-11 14:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002_add_ingestion_job_queue_fields"
down_revision: Union[str, None] = "0001_initial_persistence_foundation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add new queue, claim lease, and retry fields to ingestion_jobs
    op.add_column(
        "ingestion_jobs",
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column("stage", sa.String(length=32), nullable=False, server_default="pending"),
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column("claim_worker_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Populate any pre-existing rows with default idempotency_key if any exist
    op.execute("UPDATE ingestion_jobs SET idempotency_key = id::text WHERE idempotency_key IS NULL;")
    op.alter_column("ingestion_jobs", "idempotency_key", nullable=False)

    # Add constraints and indexes
    op.create_unique_constraint(
        "uq_ingestion_jobs_idempotency_key", "ingestion_jobs", ["idempotency_key"]
    )
    op.create_check_constraint(
        "chk_ingestion_job_status",
        "ingestion_jobs",
        "status IN ('queued', 'processing', 'completed', 'duplicate', 'failed')",
    )
    op.create_check_constraint(
        "chk_ingestion_job_stage",
        "ingestion_jobs",
        "stage IN ('pending', 'downloading', 'validating', 'parsing', 'chunking', 'embedding', 'persisting', 'indexing', 'finished')",
    )
    op.create_index(
        "idx_ingestion_jobs_queue", "ingestion_jobs", ["status", "next_retry_at"]
    )
    op.create_index(
        "idx_ingestion_jobs_heartbeat", "ingestion_jobs", ["status", "heartbeat_at"]
    )


def downgrade() -> None:
    op.drop_index("idx_ingestion_jobs_heartbeat", table_name="ingestion_jobs")
    op.drop_index("idx_ingestion_jobs_queue", table_name="ingestion_jobs")
    op.drop_constraint("chk_ingestion_job_stage", "ingestion_jobs", type_="check")
    op.drop_constraint("chk_ingestion_job_status", "ingestion_jobs", type_="check")
    op.drop_constraint("uq_ingestion_jobs_idempotency_key", "ingestion_jobs", type_="unique")

    op.drop_column("ingestion_jobs", "next_retry_at")
    op.drop_column("ingestion_jobs", "heartbeat_at")
    op.drop_column("ingestion_jobs", "claim_token")
    op.drop_column("ingestion_jobs", "claim_worker_id")
    op.drop_column("ingestion_jobs", "stage")
    op.drop_column("ingestion_jobs", "idempotency_key")
