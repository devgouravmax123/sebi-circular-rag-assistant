"""
SQLAlchemy 2.x declarative models for the SEBI Regulatory Intelligence Platform.

Implements normalized persistence for:
1. Regulators and regulatory domains (taxonomy).
2. Logical regulatory documents and immutable document versions.
3. Provisions and clauses.
4. Chunks and pgvector dense embeddings.
5. Regulatory relationships with strict constraints and conservative review defaults.
6. Ingestion jobs for tracking asynchronous processing and retries.
"""
from datetime import datetime, timezone, date
from typing import List, Optional, Any, Dict
import uuid

from sqlalchemy import (
    String,
    Text,
    Integer,
    Boolean,
    Float,
    Date,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    CheckConstraint,
    Index,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship as orm_relationship,
)
from pgvector.sqlalchemy import Vector


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy declarative models."""
    pass


class RegulatorModel(Base):
    """Official regulatory authority (e.g., SEBI, RBI, IRDAI)."""
    __tablename__ = "regulators"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # 'SEBI', 'RBI'
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    country: Mapped[str] = mapped_column(String(3), nullable=False, default="IND")
    official_portal_url: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    domains: Mapped[List["RegulatoryDomainModel"]] = orm_relationship(
        "RegulatoryDomainModel", back_populates="regulator", cascade="all, delete-orphan"
    )


class RegulatoryDomainModel(Base):
    """Regulatory framework domain (e.g., CSCRF, LODR, ICDR)."""
    __tablename__ = "regulatory_domains"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # 'SEBI_CSCRF', 'SEBI_LODR'
    regulator_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("regulators.id", ondelete="RESTRICT"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)  # 'CSCRF'
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    regulator: Mapped["RegulatorModel"] = orm_relationship("RegulatorModel", back_populates="domains")
    documents: Mapped[List["RegulatoryDocumentModel"]] = orm_relationship(
        "RegulatoryDocumentModel", back_populates="domain"
    )

    __table_args__ = (
        UniqueConstraint("regulator_id", "code", name="uq_regulator_domain_code"),
    )


class RegulatoryDocumentModel(Base):
    """
    Logical regulatory document entity representing the concept of a circular or regulation.
    Separate from its immutable physical source versions.
    """
    __tablename__ = "regulatory_documents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # 'sebi_cscrf_2024_113'
    domain_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("regulatory_domains.id", ondelete="RESTRICT"), nullable=False
    )
    circular_number: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    department: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    first_issue_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
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

    domain: Mapped["RegulatoryDomainModel"] = orm_relationship(
        "RegulatoryDomainModel", back_populates="documents"
    )
    versions: Mapped[List["DocumentVersionModel"]] = orm_relationship(
        "DocumentVersionModel", back_populates="document", cascade="all, delete-orphan"
    )
    provisions: Mapped[List["ProvisionModel"]] = orm_relationship(
        "ProvisionModel", back_populates="document", cascade="all, delete-orphan"
    )
    outgoing_edges: Mapped[List["RegulatoryEdgeModel"]] = orm_relationship(
        "RegulatoryEdgeModel",
        foreign_keys="RegulatoryEdgeModel.source_document_id",
        back_populates="source_document",
        cascade="all, delete-orphan",
    )
    incoming_edges: Mapped[List["RegulatoryEdgeModel"]] = orm_relationship(
        "RegulatoryEdgeModel",
        foreign_keys="RegulatoryEdgeModel.target_document_id",
        back_populates="target_document",
        cascade="all, delete-orphan",
    )


class DocumentVersionModel(Base):
    """
    Immutable physical version of a regulatory document corresponding to an exact ingested source PDF.
    Preserves provenance, checksum, source URL, and page boundaries without being overwritten.
    """
    __tablename__ = "document_versions"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)  # 'sebi_cscrf_2024_113_v1'
    document_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("regulatory_documents.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)  # Path to local PDF or object store URI
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)  # SHA-256 checksum of source PDF
    issue_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    effective_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    extraction_status: Mapped[str] = mapped_column(String(32), nullable=False, default="indexed")
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    document: Mapped["RegulatoryDocumentModel"] = orm_relationship(
        "RegulatoryDocumentModel", back_populates="versions"
    )
    chunks: Mapped[List["ChunkModel"]] = orm_relationship(
        "ChunkModel", back_populates="version", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("document_id", "version_number", name="uq_doc_version_number"),
        # Unique across document_id + sha256 to allow identical PDF in separate logical docs if ever cross-listed
        UniqueConstraint("document_id", "sha256", name="uq_doc_version_sha256"),
        Index("idx_doc_versions_sha256", "sha256"),
    )


class ProvisionModel(Base):
    """
    Identified regulatory provision, paragraph, section, or annexure.
    Enables granular provision-level status and citation attribution.
    """
    __tablename__ = "provisions"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)  # 'sebi_cscrf_2024_113_clause_6_11'
    document_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("regulatory_documents.id", ondelete="CASCADE"), nullable=False
    )
    provision_ref: Mapped[str] = mapped_column(String(64), nullable=False)  # 'Clause 6.11', 'Paragraph 17'
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pdf_page: Mapped[int] = mapped_column(Integer, nullable=False)
    printed_page: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    document: Mapped["RegulatoryDocumentModel"] = orm_relationship(
        "RegulatoryDocumentModel", back_populates="provisions"
    )

    __table_args__ = (
        UniqueConstraint("document_id", "provision_ref", name="uq_doc_provision_ref"),
        Index("idx_provisions_doc_ref", "document_id", "provision_ref"),
    )


class ChunkModel(Base):
    """
    Deterministic extracted text chunk with dual-page provenance.
    """
    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)  # 'sebi_cscrf_2024_113_p80_67'
    version_id: Mapped[str] = mapped_column(
        String(96), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    pdf_page_start: Mapped[int] = mapped_column(Integer, nullable=False)
    pdf_page_end: Mapped[int] = mapped_column(Integer, nullable=False)
    printed_page_start: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    printed_page_end: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    is_ocr: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    chunk_type: Mapped[str] = mapped_column(String(32), nullable=False, default="body")
    metadata_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    version: Mapped["DocumentVersionModel"] = orm_relationship(
        "DocumentVersionModel", back_populates="chunks"
    )
    embedding: Mapped[Optional["ChunkEmbeddingModel"]] = orm_relationship(
        "ChunkEmbeddingModel", back_populates="chunk", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("version_id", "chunk_index", name="uq_version_chunk_index"),
        Index("idx_chunks_version_id", "version_id"),
    )


class ChunkEmbeddingModel(Base):
    """
    pgvector dense vector representation of a chunk.
    Stores explicit model name and dimension metadata for migration safety.
    """
    __tablename__ = "chunk_embeddings"

    chunk_id: Mapped[str] = mapped_column(
        String(96), ForeignKey("chunks.id", ondelete="CASCADE"), primary_key=True
    )
    model_name: Mapped[str] = mapped_column(
        String(128), nullable=False, default="BAAI/bge-small-en-v1.5"
    )
    embedding_dim: Mapped[int] = mapped_column(Integer, nullable=False, default=384)
    embedding: Mapped[Any] = mapped_column(Vector(384), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    chunk: Mapped["ChunkModel"] = orm_relationship("ChunkModel", back_populates="embedding")

    __table_args__ = (
        CheckConstraint("embedding_dim = 384", name="chk_embedding_dim_384"),
    )


class RegulatoryEdgeModel(Base):
    """
    Regulatory relationship edge strictly directed as:
    Newer/acting circular (`source_document_id`) -> RELATIONSHIP -> Older/affected circular (`target_document_id`)
    Default review status is conservative: 'proposed'.
    """
    __tablename__ = "regulatory_edges"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    source_document_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("regulatory_documents.id", ondelete="CASCADE"), nullable=False
    )
    target_document_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("regulatory_documents.id", ondelete="CASCADE"), nullable=False
    )
    relationship: Mapped[str] = mapped_column(String(32), nullable=False)  # 'AMENDS', 'SUPERSEDES', 'REPLACES', 'RESCINDS', 'REFERENCES', 'CLARIFIES'
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="full")  # 'full', 'partial'
    affected_provisions: Mapped[List[str]] = mapped_column(JSONB, nullable=False, default=list)
    evidence_text: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_pdf_page: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    confidence: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")  # 'high', 'medium', 'low'
    review_status: Mapped[str] = mapped_column(String(32), nullable=False, default="proposed")  # 'proposed', 'needs_review', 'verified', 'rejected', 'revoked'
    effective_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    # Provenance and Audit Trail
    provenance: Mapped[str] = mapped_column(String(32), nullable=False, default="imported")  # 'imported', 'user_proposed', 'system_extracted'
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    review_rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    review_history: Mapped[List[Dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)

    source_document: Mapped["RegulatoryDocumentModel"] = orm_relationship(
        "RegulatoryDocumentModel",
        foreign_keys="[RegulatoryEdgeModel.source_document_id]",
        back_populates="outgoing_edges",
    )
    target_document: Mapped["RegulatoryDocumentModel"] = orm_relationship(
        "RegulatoryDocumentModel",
        foreign_keys="[RegulatoryEdgeModel.target_document_id]",
        back_populates="incoming_edges",
    )

    __table_args__ = (
        CheckConstraint("source_document_id != target_document_id", name="chk_edge_no_self_loop"),
        CheckConstraint(
            "relationship IN ('AMENDS', 'SUPERSEDES', 'REPLACES', 'RESCINDS', 'REFERENCES', 'CLARIFIES')",
            name="chk_edge_relationship_type",
        ),
        CheckConstraint("scope IN ('full', 'partial')", name="chk_edge_scope_type"),
        CheckConstraint("confidence IN ('high', 'medium', 'low')", name="chk_edge_confidence"),
        CheckConstraint(
            "review_status IN ('proposed', 'needs_review', 'verified', 'rejected', 'revoked')",
            name="chk_edge_review_status",
        ),
        CheckConstraint(
            "provenance IN ('imported', 'user_proposed', 'system_extracted')",
            name="chk_edge_provenance",
        ),
        Index("idx_edges_source", "source_document_id"),
        Index("idx_edges_target", "target_document_id"),
        Index("idx_edges_review_status", "review_status"),
        Index("idx_edges_relationship", "relationship"),
    )


class IngestionJobModel(Base):
    """
    Ingestion job ledger for tracking dynamic document intake, lifecycle, and retries.
    Supports atomic claims via FOR UPDATE SKIP LOCKED, heartbeat lease tracking,
    and exponential backoff retry scheduling.
    """
    __tablename__ = "ingestion_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    document_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    claim_worker_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    claim_token: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    error_details: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_ingestion_jobs_idempotency_key"),
        CheckConstraint("status IN ('queued', 'processing', 'completed', 'duplicate', 'failed')", name="chk_ingestion_job_status"),
        CheckConstraint(
            "stage IN ('pending', 'downloading', 'validating', 'parsing', 'chunking', 'embedding', 'persisting', 'indexing', 'finished')",
            name="chk_ingestion_job_stage",
        ),
        Index("idx_ingestion_jobs_status", "status"),
        Index("idx_ingestion_jobs_queue", "status", "next_retry_at"),
        Index("idx_ingestion_jobs_heartbeat", "status", "heartbeat_at"),
    )
