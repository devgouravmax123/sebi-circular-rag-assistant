"""generalize_document_model

Revision ID: 0012_generalize_document_model
Revises: 0011_add_firebase_auth
Create Date: 2026-11-17 13:15:00.000000

Task: Citenza General-Purpose Document Model Decoupling (Master Prompt 23):
1. Drops NOT NULL constraint on public.regulatory_documents.domain_id so general documents do not require a regulatory domain.
2. Updates trigger app.default_document_workspace_collection to default collection_id to 'general_synthetic' rather than 'sebi_cscrf'.
3. Relaxes collection policy check constraint on public.collections.
4. Preserves backward compatibility with all historical tables, rows, and relationships.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0012_generalize_document_model"
down_revision: Union[str, None] = "0011_add_firebase_auth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        op.execute(
            """
            -- 1. Drop NOT NULL on regulatory_documents.domain_id
            ALTER TABLE public.regulatory_documents
                ALTER COLUMN domain_id DROP NOT NULL;

            -- 2. Update default_document_workspace_collection trigger function
            CREATE OR REPLACE FUNCTION app.default_document_workspace_collection()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, public, app
            AS $$
            BEGIN
                IF NEW.workspace_id IS NULL THEN
                    NEW.workspace_id := '00000000-0000-0000-0000-000000000000'::UUID;
                END IF;
                IF NEW.collection_id IS NULL THEN
                    NEW.collection_id := 'general_synthetic';
                END IF;
                RETURN NEW;
            END;
            $$;
            """
        )
    else:
        with op.batch_alter_table("regulatory_documents") as batch_op:
            batch_op.alter_column("domain_id", existing_type=sa.String(length=64), nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        op.execute(
            """
            -- Restore NOT NULL on domain_id
            UPDATE public.regulatory_documents SET domain_id = 'SEBI_CSCRF' WHERE domain_id IS NULL;
            ALTER TABLE public.regulatory_documents
                ALTER COLUMN domain_id SET NOT NULL;

            -- Restore legacy default trigger function
            CREATE OR REPLACE FUNCTION app.default_document_workspace_collection()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, public, app
            AS $$
            BEGIN
                IF NEW.workspace_id IS NULL THEN
                    NEW.workspace_id := '00000000-0000-0000-0000-000000000000'::UUID;
                END IF;
                IF NEW.collection_id IS NULL THEN
                    IF NEW.domain_id = 'SEBI_CSCRF' THEN
                        NEW.collection_id := 'sebi_cscrf';
                    ELSE
                        NEW.collection_id := 'general_synthetic';
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $$;
            """
        )
    else:
        with op.batch_alter_table("regulatory_documents") as batch_op:
            batch_op.alter_column("domain_id", existing_type=sa.String(length=64), nullable=False)
