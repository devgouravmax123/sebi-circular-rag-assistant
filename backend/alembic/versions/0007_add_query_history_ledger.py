"""add_query_history_ledger

Revision ID: 0007_add_query_history_ledger
Revises: 0006_add_auth_and_workspace_schema
Create Date: 2026-10-31 23:25:00.000000

Milestone 8 - Task 8.6:
1. Creates query_history table:
   - id: UUID primary key.
   - user_id: UUID foreign key -> users(id) ON DELETE CASCADE.
   - workspace_id: UUID foreign key -> workspaces(id) ON DELETE SET NULL.
   - collection_id: VARCHAR(100) NOT NULL.
   - query_text: TEXT NOT NULL.
   - query_options: JSONB NOT NULL DEFAULT '{}'::jsonb.
   - disposition: VARCHAR(50) NOT NULL.
   - summary_narrative: TEXT NULL.
   - answer_text: TEXT NULL.
   - claim_citations: JSONB NOT NULL DEFAULT '[]'::jsonb.
   - verified_excerpts_count: INTEGER NOT NULL DEFAULT 0.
   - technical_validation_passed: BOOLEAN NOT NULL DEFAULT FALSE.
   - created_at: TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp().
2. Adds composite foreign key to collections(workspace_id, id) for strict tenant integrity.
3. Adds indexes for user-scoped, workspace-scoped chronological pagination.
4. Preserves worker least privilege (no worker role grants).
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0007_add_query_history_ledger"
down_revision: Union[str, None] = "0006_add_auth_and_workspace_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.query_history (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
            workspace_id UUID REFERENCES public.workspaces(id) ON DELETE SET NULL,
            collection_id VARCHAR(100) NOT NULL,
            query_text TEXT NOT NULL,
            query_options JSONB NOT NULL DEFAULT '{}'::jsonb,
            disposition VARCHAR(50) NOT NULL,
            summary_narrative TEXT,
            answer_text TEXT,
            claim_citations JSONB NOT NULL DEFAULT '[]'::jsonb,
            verified_excerpts_count INTEGER NOT NULL DEFAULT 0,
            technical_validation_passed BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT fk_query_history_collection_workspace
                FOREIGN KEY (workspace_id, collection_id)
                REFERENCES public.collections(workspace_id, id)
                ON DELETE RESTRICT
        );

        -- Performance indexes for user-scoped and workspace-scoped chronological listing
        CREATE INDEX IF NOT EXISTS idx_query_history_user_created 
            ON public.query_history (user_id, created_at DESC);

        CREATE INDEX IF NOT EXISTS idx_query_history_ws_user_created 
            ON public.query_history (workspace_id, user_id, created_at DESC);

        CREATE INDEX IF NOT EXISTS idx_query_history_collection 
            ON public.query_history (collection_id, created_at DESC);

        -- Ensure protect_workspace_owner_membership returns OLD on DELETE
        CREATE OR REPLACE FUNCTION app.protect_workspace_owner_membership()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public, app
        AS $$
        DECLARE
            v_ws_owner_id UUID;
        BEGIN
            SELECT owner_id INTO v_ws_owner_id
            FROM public.workspaces
            WHERE id = OLD.workspace_id;

            IF v_ws_owner_id IS NOT NULL AND OLD.user_id = v_ws_owner_id THEN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'CANNOT_DELETE_OWNER_MEMBERSHIP: Cannot delete membership of workspace owner %.', OLD.user_id;
                ELSIF TG_OP = 'UPDATE' THEN
                    IF NEW.user_id != OLD.user_id THEN
                        RAISE EXCEPTION 'CANNOT_TRANSFER_OWNER_MEMBERSHIP_USER: Modifying owner membership user_id is prohibited.';
                    END IF;
                    IF NEW.role != 'owner' THEN
                        RAISE EXCEPTION 'CANNOT_DEMOTE_WORKSPACE_OWNER: Workspace owner role cannot be altered while owner_id is set.';
                    END IF;
                END IF;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.query_history CASCADE;")

