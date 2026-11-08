"""
SQLAlchemy 2.x declarative models for ephemeral guest sessions and durable session history (Milestones 11, 12, 13).
"""
from datetime import datetime, timezone
from typing import Optional, List
import uuid

from sqlalchemy import (
    String,
    Text,
    Boolean,
    Integer,
    BigInteger,
    DateTime,
    ForeignKey,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
    relationship as orm_relationship,
)

from app.persistence.models import Base


class GuestSessionModel(Base):
    """
    Server-side tracking for ephemeral guest sessions with quotas, TTL, and cleanup status.
    """
    __tablename__ = "guest_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )
    collections_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    documents_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    bytes_uploaded: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        nullable=False,
    )
    queries_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    cleaned_up_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    history_items: Mapped[List["GuestQueryHistoryModel"]] = orm_relationship(
        "GuestQueryHistoryModel",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="desc(GuestQueryHistoryModel.created_at)",
    )


class GuestQueryHistoryModel(Base):
    """
    Durable session-scoped query history record for ephemeral guest sessions.
    Automatically cascades and purges when the parent guest session is cleaned up or expired.
    """
    __tablename__ = "guest_query_history"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("guest_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    collection_id: Mapped[str] = mapped_column(String(100), nullable=False)
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    disposition: Mapped[str] = mapped_column(String(50), nullable=False)
    answer_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    citations_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    session: Mapped["GuestSessionModel"] = orm_relationship(
        "GuestSessionModel", back_populates="history_items"
    )
