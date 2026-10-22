"""
Domain schemas and manifest models for Search Index Publication (Phase 8B.6).
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field


class SnapshotManifest(BaseModel):
    """
    Immutable manifest metadata for an atomically published search index snapshot.
    """
    snapshot_id: str = Field(..., description="Unique snapshot identifier, e.g. snap_20261011_120000")
    collection_id: str = Field(default="sebi_cscrf", description="Owning collection identifier")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    format_version: str = Field(default="1.0.0", description="Index format schema version")
    embedding_model: str = Field(default="BAAI/bge-small-en-v1.5")
    embedding_dim: int = Field(default=384)
    total_documents: int = Field(..., ge=0)
    total_chunks: int = Field(..., ge=0)
    document_ids: List[str] = Field(default_factory=list)
    document_versions: Dict[str, str] = Field(
        default_factory=dict, description="Map of document_id -> content_sha256"
    )
    chunk_ids: List[str] = Field(default_factory=list)
    corpus_fingerprint: str = Field(..., description="SHA-256 digest over ordered chunk text")
    artifact_checksums: Dict[str, str] = Field(
        default_factory=dict, description="SHA-256 checksums of snapshot files (dense_index.npy, chunks.json, etc.)"
    )
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ActiveSnapshotPointer(BaseModel):
    """
    Pointer stored in active_snapshot.json indicating the active snapshot.
    """
    snapshot_id: str
    collection_id: str = Field(default="sebi_cscrf", description="Owning collection identifier")
    activated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    manifest_checksum: str
    total_documents: int
    total_chunks: int


class PublicationResult(BaseModel):
    """
    Result returned after index building and publication.
    """
    success: bool
    snapshot_id: Optional[str] = None
    total_documents: int = 0
    total_chunks: int = 0
    newly_published_documents: List[str] = Field(default_factory=list)
    message: str = ""
    error_details: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class IndexStatusResponse(BaseModel):
    """
    Administrative response detailing current active index status and pending documents.
    """
    has_active_snapshot: bool
    active_snapshot_id: Optional[str] = None
    total_documents: int = 0
    total_chunks: int = 0
    published_document_ids: List[str] = Field(default_factory=list)
    pending_document_ids: List[str] = Field(
        default_factory=list, description="Documents persisted in storage but not yet published"
    )
    last_activated_at: Optional[datetime] = None
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    format_version: str = "1.0.0"
