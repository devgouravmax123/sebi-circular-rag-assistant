"""expand_regulatory_edge_management
Revision ID: 0003_expand_regulatory_edge_management
Revises: 0002_add_ingestion_job_queue_fields
Create Date: 2026-10-11 19:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003_expand_regulatory_edge_management"
down_revision: Union[str, None] = "0002_add_ingestion_job_queue_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Drop existing check constraints on relationship and review_status
    op.drop_constraint("chk_edge_relationship_type", "regulatory_edges", type_="check")
    op.drop_constraint("chk_edge_review_status", "regulatory_edges", type_="check")

    # 2. Add audit and provenance columns
    op.add_column(
        "regulatory_edges",
        sa.Column("provenance", sa.String(length=32), nullable=False, server_default="imported"),
    )
    op.add_column(
        "regulatory_edges",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "regulatory_edges",
        sa.Column("reviewed_by", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "regulatory_edges",
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "regulatory_edges",
        sa.Column("review_rationale", sa.Text(), nullable=True),
    )
    op.add_column(
        "regulatory_edges",
        sa.Column("review_history", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
    )

    # 3. Add updated check constraints and indexes
    op.create_check_constraint(
        "chk_edge_relationship_type",
        "regulatory_edges",
        "relationship IN ('AMENDS', 'SUPERSEDES', 'REPLACES', 'RESCINDS', 'REFERENCES', 'CLARIFIES')",
    )
    op.create_check_constraint(
        "chk_edge_review_status",
        "regulatory_edges",
        "review_status IN ('proposed', 'needs_review', 'verified', 'rejected', 'revoked')",
    )
    op.create_check_constraint(
        "chk_edge_provenance",
        "regulatory_edges",
        "provenance IN ('imported', 'user_proposed', 'system_extracted')",
    )
    op.create_index("idx_edges_relationship", "regulatory_edges", ["relationship"])


def downgrade() -> None:
    op.drop_index("idx_edges_relationship", table_name="regulatory_edges")
    op.drop_constraint("chk_edge_provenance", "regulatory_edges", type_="check")
    op.drop_constraint("chk_edge_review_status", "regulatory_edges", type_="check")
    op.drop_constraint("chk_edge_relationship_type", "regulatory_edges", type_="check")

    op.drop_column("regulatory_edges", "review_history")
    op.drop_column("regulatory_edges", "review_rationale")
    op.drop_column("regulatory_edges", "reviewed_at")
    op.drop_column("regulatory_edges", "reviewed_by")
    op.drop_column("regulatory_edges", "updated_at")
    op.drop_column("regulatory_edges", "provenance")

    # Restore legacy check constraints
    op.create_check_constraint(
        "chk_edge_relationship_type",
        "regulatory_edges",
        "relationship IN ('AMENDS', 'RESCINDS', 'REFERENCES')",
    )
    op.create_check_constraint(
        "chk_edge_review_status",
        "regulatory_edges",
        "review_status IN ('needs_review', 'verified', 'rejected')",
    )
