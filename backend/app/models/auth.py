"""
SQLAlchemy 2.x declarative models for multi-tenant authentication and workspaces (Milestone 8).
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
    Index,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
    relationship as orm_relationship,
)

from app.persistence.models import Base


class UserModel(Base):
    """Platform user account for workspace membership and authentication."""
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
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

    owned_workspaces: Mapped[List["WorkspaceModel"]] = orm_relationship(
        "WorkspaceModel", back_populates="owner"
    )
    memberships: Mapped[List["WorkspaceMembershipModel"]] = orm_relationship(
        "WorkspaceMembershipModel", back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("uq_users_email_lower", "email", unique=True),
    )


class WorkspaceModel(Base):
    """Multi-tenant isolation boundary owning document collections and memberships."""
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    owner_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
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

    owner: Mapped[Optional["UserModel"]] = orm_relationship(
        "UserModel", back_populates="owned_workspaces"
    )
    memberships: Mapped[List["WorkspaceMembershipModel"]] = orm_relationship(
        "WorkspaceMembershipModel", back_populates="workspace", cascade="all, delete-orphan"
    )
    collections: Mapped[List["CollectionModel"]] = orm_relationship(
        "CollectionModel", back_populates="workspace", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "(is_system = TRUE AND owner_id IS NULL) OR (is_system = FALSE AND owner_id IS NOT NULL)",
            name="chk_workspace_owner",
        ),
    )


class WorkspaceMembershipModel(Base):
    """User membership and RBAC role within a specific workspace."""
    __tablename__ = "workspace_memberships"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(30), nullable=False)  # 'owner', 'admin', 'member', 'viewer'
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

    workspace: Mapped["WorkspaceModel"] = orm_relationship("WorkspaceModel", back_populates="memberships")
    user: Mapped["UserModel"] = orm_relationship("UserModel", back_populates="memberships")

    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_workspace_member"),
        CheckConstraint("role IN ('owner', 'admin', 'member', 'viewer')", name="chk_workspace_membership_role"),
    )

