"""
SQLAlchemy 2.x declarative models for collections and tenant asset containers (Milestone 8).
"""
from datetime import datetime, timezone
from typing import List, Optional
import uuid

from sqlalchemy import (
    String,
    Text,
    Boolean,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    CheckConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
    relationship as orm_relationship,
)

from app.persistence.models import Base


class CollectionModel(Base):
    """Collection catalog entity partitioning documents and grounding policies by workspace."""
    __tablename__ = "collections"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    policy_name: Mapped[str] = mapped_column(String(100), nullable=False)
    enforce_regulatory_graph: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    workspace: Mapped["WorkspaceModel"] = orm_relationship("WorkspaceModel", back_populates="collections")

    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_collections_workspace_id"),
        CheckConstraint("policy_name IN ('sebi_cscrf_strict', 'general_static')", name="chk_collection_policy"),
    )
