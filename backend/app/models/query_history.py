"""
SQLAlchemy declarative model for persistent query history (Milestone 8 - Task 8.6).
"""
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
import uuid

from sqlalchemy import (
    String,
    Text,
    Boolean,
    Integer,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
    relationship as orm_relationship,
)

from app.persistence.models import Base


class QueryHistoryModel(Base):
    """
    Persistent query history record scoped to authenticated user, workspace, and collection.
    """
    __tablename__ = "query_history"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True
    )
    collection_id: Mapped[str] = mapped_column(String(100), nullable=False)
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    query_options: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    disposition: Mapped[str] = mapped_column(String(50), nullable=False)
    summary_narrative: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    answer_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    claim_citations: Mapped[List[Dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    verified_excerpts_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    technical_validation_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "collection_id"],
            ["collections.workspace_id", "collections.id"],
            name="fk_query_history_collection_workspace",
            ondelete="RESTRICT",
        ),
    )

