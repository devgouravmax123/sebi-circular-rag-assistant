"""add_guest_sessions

Revision ID: 0010_add_guest_sessions
Revises: 0009_provision_api_runtime_role
Create Date: 2026-11-08 00:45:00.000000

Task: Ephemeral Guest Session Model, Quotas & Durable Session History (Milestones 11, 12, 13):
1. Creates table public.guest_sessions tracking ephemeral guest sessions with quotas and explicit TTL.
2. Tracks session token hash, active status, creation/expiration timestamps, and resource counters.
3. Creates table public.guest_query_history for durable session-scoped query ledger.
4. Grants scoped CRUD privileges on guest_sessions and guest_query_history to app_user and ingestion_worker_role.
5. Preserves backward compatibility and reversibility.
"""
from typing import Sequence, Union
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0010_add_guest_sessions"
down_revision: Union[str, None] = "0009_provision_api_runtime_role"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        op.execute(
            """
            CREATE TABLE IF NOT EXISTS public.guest_sessions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                session_token_hash VARCHAR(64) NOT NULL UNIQUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
                expires_at TIMESTAMPTZ NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                collections_count INTEGER NOT NULL DEFAULT 0,
                documents_count INTEGER NOT NULL DEFAULT 0,
                bytes_uploaded BIGINT NOT NULL DEFAULT 0,
                queries_count INTEGER NOT NULL DEFAULT 0,
                cleaned_up_at TIMESTAMPTZ
            );

            CREATE INDEX IF NOT EXISTS idx_guest_sessions_token_hash
                ON public.guest_sessions(session_token_hash);

            CREATE INDEX IF NOT EXISTS idx_guest_sessions_expiry_active
                ON public.guest_sessions(expires_at, is_active);

            CREATE TABLE IF NOT EXISTS public.guest_query_history (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                session_id UUID NOT NULL REFERENCES public.guest_sessions(id) ON DELETE CASCADE,
                collection_id VARCHAR(100) NOT NULL,
                query_text TEXT NOT NULL,
                disposition VARCHAR(50) NOT NULL,
                answer_text TEXT,
                citations_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
            );

            CREATE INDEX IF NOT EXISTS idx_guest_query_history_session
                ON public.guest_query_history(session_id, created_at DESC);

            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
                    GRANT SELECT, INSERT, UPDATE, DELETE ON public.guest_sessions TO app_user;
                    GRANT SELECT, INSERT, UPDATE, DELETE ON public.guest_query_history TO app_user;
                END IF;
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ingestion_worker_role') THEN
                    GRANT SELECT, UPDATE, DELETE ON public.guest_sessions TO ingestion_worker_role;
                    GRANT SELECT, UPDATE, DELETE ON public.guest_query_history TO ingestion_worker_role;
                END IF;
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sebi_api') THEN
                    GRANT SELECT, INSERT, UPDATE, DELETE ON public.guest_sessions TO sebi_api;
                    GRANT SELECT, INSERT, UPDATE, DELETE ON public.guest_query_history TO sebi_api;
                END IF;
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sebi_worker') THEN
                    GRANT SELECT, UPDATE, DELETE ON public.guest_sessions TO sebi_worker;
                    GRANT SELECT, UPDATE, DELETE ON public.guest_query_history TO sebi_worker;
                END IF;
            END
            $$;
            """
        )
    else:
        # SQLite / other dialects for testing
        op.execute(
            """
            CREATE TABLE IF NOT EXISTS guest_sessions (
                id VARCHAR(36) PRIMARY KEY,
                session_token_hash VARCHAR(64) NOT NULL UNIQUE,
                created_at TIMESTAMP NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                collections_count INTEGER NOT NULL DEFAULT 0,
                documents_count INTEGER NOT NULL DEFAULT 0,
                bytes_uploaded BIGINT NOT NULL DEFAULT 0,
                queries_count INTEGER NOT NULL DEFAULT 0,
                cleaned_up_at TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS guest_query_history (
                id VARCHAR(36) PRIMARY KEY,
                session_id VARCHAR(36) NOT NULL REFERENCES guest_sessions(id) ON DELETE CASCADE,
                collection_id VARCHAR(100) NOT NULL,
                query_text TEXT NOT NULL,
                disposition VARCHAR(50) NOT NULL,
                answer_text TEXT,
                citations_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL
            );
            """
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.guest_query_history CASCADE;")
    op.execute("DROP TABLE IF EXISTS public.guest_sessions CASCADE;")
