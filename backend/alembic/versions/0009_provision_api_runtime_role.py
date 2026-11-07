"""provision_api_runtime_role

Revision ID: 0009_provision_api_runtime_role
Revises: 0008_harden_routine_privileges
Create Date: 2026-11-04 15:30:00.000000

Task 8.1 - API Runtime Role & Scoped Table Privileges:
1. Grants USAGE on schema public and app to app_user.
2. Grants scoped CRUD privileges on public tables to app_user:
   - Multi-tenant / Auth: users, workspaces, workspace_memberships, collections (SELECT, INSERT, UPDATE, DELETE)
   - Query history ledger: query_history (SELECT, INSERT, UPDATE, DELETE)
   - Regulatory documents & graph: regulatory_documents, document_versions, provisions, regulatory_edges (SELECT, INSERT, UPDATE, DELETE)
   - Reference data: regulators, regulatory_domains (SELECT, INSERT, UPDATE)
   - Ingestion jobs: ingestion_jobs (SELECT, INSERT, UPDATE)
   - Chunks & vector embeddings: chunks, chunk_embeddings (SELECT ONLY - writes strictly reserved to worker procedures)
3. Grants USAGE, SELECT on all sequences in schema public and configures ALTER DEFAULT PRIVILEGES for sequences.
4. Preserves least privilege: zero routine EXECUTE grants on worker procedures or trigger functions.
5. Reversible downgrade: revokes all granted table and sequence permissions.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0009_provision_api_runtime_role"
down_revision: Union[str, None] = "0008_harden_routine_privileges"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # Ensure app_user exists (pre-provisioned by bootstrap_neon_roles.sql in production)
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
                CREATE ROLE app_user WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
            END IF;
        END
        $$;
        """
    )

    # 1. Schema USAGE
    op.execute("GRANT USAGE ON SCHEMA public, app TO app_user;")

    # 2. Scoped Table Privileges for API Runtime
    op.execute(
        """
        -- Multi-tenancy and User Management
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
            public.users,
            public.workspaces,
            public.workspace_memberships,
            public.collections
        TO app_user;

        -- Query History Ledger
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
            public.query_history
        TO app_user;

        -- Regulatory Knowledge Base Entities
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
            public.regulatory_documents,
            public.document_versions,
            public.provisions,
            public.regulatory_edges
        TO app_user;

        -- Reference Catalogs
        GRANT SELECT, INSERT, UPDATE ON TABLE
            public.regulators,
            public.regulatory_domains
        TO app_user;

        -- Ingestion Job Tracking (enqueue, status poll, retry/cancel)
        GRANT SELECT, INSERT, UPDATE ON TABLE
            public.ingestion_jobs
        TO app_user;

        -- Vector Search & Retrieval (SELECT ONLY: direct writes prohibited)
        GRANT SELECT ON TABLE
            public.chunks,
            public.chunk_embeddings
        TO app_user;
        """
    )

    # 3. Sequences (for any present or future serial columns)
    op.execute(
        """
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_user;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO app_user;
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE, SELECT ON SEQUENCES FROM app_user;
        REVOKE USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public FROM app_user;

        REVOKE SELECT ON TABLE
            public.chunks,
            public.chunk_embeddings
        FROM app_user;

        REVOKE SELECT, INSERT, UPDATE ON TABLE
            public.ingestion_jobs,
            public.regulators,
            public.regulatory_domains
        FROM app_user;

        REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLE
            public.users,
            public.workspaces,
            public.workspace_memberships,
            public.collections,
            public.query_history,
            public.regulatory_documents,
            public.document_versions,
            public.provisions,
            public.regulatory_edges
        FROM app_user;
        """
    )

