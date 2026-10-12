"""
0001_initial_persistence_foundation

Revision ID: 0001_initial_persistence_foundation
Revises:
Create Date: 2026-10-11 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = "0001_initial_persistence_foundation"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Ensure required extensions exist in PostgreSQL
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";')

    # 2. Regulators
    op.create_table(
        "regulators",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("country", sa.String(length=3), nullable=False, server_default="IND"),
        sa.Column("official_portal_url", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )

    # 3. Regulatory Domains
    op.create_table(
        "regulatory_domains",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("regulator_id", sa.String(length=32), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["regulator_id"], ["regulators.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("regulator_id", "code", name="uq_regulator_domain_code"),
    )

    # 4. Regulatory Documents (Logical Entity)
    op.create_table(
        "regulatory_documents",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("domain_id", sa.String(length=64), nullable=False),
        sa.Column("circular_number", sa.String(length=128), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("department", sa.String(length=128), nullable=True),
        sa.Column("first_issue_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["domain_id"], ["regulatory_domains.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_docs_circular_no", "regulatory_documents", ["circular_number"])

    # 5. Document Versions (Immutable Source Versions)
    op.create_table(
        "document_versions",
        sa.Column("id", sa.String(length=96), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("issue_date", sa.Date(), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("extraction_status", sa.String(length=32), nullable=False, server_default="indexed"),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["document_id"], ["regulatory_documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "version_number", name="uq_doc_version_number"),
        sa.UniqueConstraint("document_id", "sha256", name="uq_doc_version_sha256"),
    )
    op.create_index("idx_doc_versions_sha256", "document_versions", ["sha256"])

    # 6. Provisions
    op.create_table(
        "provisions",
        sa.Column("id", sa.String(length=96), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("provision_ref", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("pdf_page", sa.Integer(), nullable=False),
        sa.Column("printed_page", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["document_id"], ["regulatory_documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "provision_ref", name="uq_doc_provision_ref"),
    )
    op.create_index("idx_provisions_doc_ref", "provisions", ["document_id", "provision_ref"])

    # 7. Chunks
    op.create_table(
        "chunks",
        sa.Column("id", sa.String(length=96), nullable=False),
        sa.Column("version_id", sa.String(length=96), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("pdf_page_start", sa.Integer(), nullable=False),
        sa.Column("pdf_page_end", sa.Integer(), nullable=False),
        sa.Column("printed_page_start", sa.String(length=32), nullable=True),
        sa.Column("printed_page_end", sa.String(length=32), nullable=True),
        sa.Column("is_ocr", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("chunk_type", sa.String(length=32), nullable=False, server_default="body"),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["version_id"], ["document_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version_id", "chunk_index", name="uq_version_chunk_index"),
    )
    op.create_index("idx_chunks_version_id", "chunks", ["version_id"])

    # 8. Chunk Embeddings (pgvector)
    op.create_table(
        "chunk_embeddings",
        sa.Column("chunk_id", sa.String(length=96), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False, server_default="BAAI/bge-small-en-v1.5"),
        sa.Column("embedding_dim", sa.Integer(), nullable=False, server_default="384"),
        sa.Column("embedding", Vector(384), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("embedding_dim = 384", name="chk_embedding_dim_384"),
        sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("chunk_id"),
    )

    # 9. Regulatory Edges
    op.create_table(
        "regulatory_edges",
        sa.Column("id", sa.String(length=96), nullable=False),
        sa.Column("source_document_id", sa.String(length=64), nullable=False),
        sa.Column("target_document_id", sa.String(length=64), nullable=False),
        sa.Column("relationship", sa.String(length=32), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="full"),
        sa.Column("affected_provisions", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("evidence_text", sa.Text(), nullable=False),
        sa.Column("evidence_pdf_page", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.String(length=16), nullable=False, server_default="medium"),
        sa.Column("review_status", sa.String(length=32), nullable=False, server_default="needs_review"),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("source_document_id != target_document_id", name="chk_edge_no_self_loop"),
        sa.CheckConstraint("relationship IN ('AMENDS', 'RESCINDS', 'REFERENCES')", name="chk_edge_relationship_type"),
        sa.CheckConstraint("scope IN ('full', 'partial')", name="chk_edge_scope_type"),
        sa.CheckConstraint("confidence IN ('high', 'medium', 'low')", name="chk_edge_confidence"),
        sa.CheckConstraint("review_status IN ('needs_review', 'verified', 'rejected')", name="chk_edge_review_status"),
        sa.ForeignKeyConstraint(["source_document_id"], ["regulatory_documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_document_id"], ["regulatory_documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_edges_source", "regulatory_edges", ["source_document_id"])
    op.create_index("idx_edges_target", "regulatory_edges", ["target_document_id"])
    op.create_index("idx_edges_review_status", "regulatory_edges", ["review_status"])

    # 10. Ingestion Jobs
    op.create_table(
        "ingestion_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("error_details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_ingestion_jobs_status", "ingestion_jobs", ["status"])


def downgrade() -> None:
    op.drop_table("ingestion_jobs")
    op.drop_table("regulatory_edges")
    op.drop_table("chunk_embeddings")
    op.drop_table("chunks")
    op.drop_table("provisions")
    op.drop_table("document_versions")
    op.drop_table("regulatory_documents")
    op.drop_table("regulatory_domains")
    op.drop_table("regulators")
