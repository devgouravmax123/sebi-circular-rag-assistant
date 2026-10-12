"""
Pydantic schemas for the Administrative Ingestion REST API (Phase 8B.5).
"""
from datetime import datetime
from typing import Optional, List, Dict, Any
import uuid
from pydantic import BaseModel, Field, HttpUrl, field_validator

from app.schemas.ingestion_job import JobStatus, ProcessingStage


class RegisterDocumentRequest(BaseModel):
    """Payload for registering a regulatory document for ingestion."""
    source_url: str = Field(..., description="HTTPS URL of the regulatory document to ingest")
    regulator: str = Field(default="SEBI", description="Regulatory authority (e.g. SEBI)")
    domain: str = Field(default="CSCRF", description="Regulatory domain (e.g. CSCRF, LODR)")
    idempotency_key: str = Field(..., min_length=4, max_length=128, description="Client idempotency key")
    document_id: Optional[str] = Field(None, min_length=3, max_length=100, description="Optional logical document ID")
    circular_number: Optional[str] = Field(None, max_length=150, description="Official circular reference number")
    title: Optional[str] = Field(None, max_length=300, description="Document title")
    max_attempts: int = Field(default=3, ge=1, le=10, description="Maximum retry attempts")

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, v: str) -> str:
        v_stripped = v.strip()
        if not v_stripped:
            raise ValueError("source_url must not be empty")
        if not v_stripped.startswith("https://") and not v_stripped.startswith("http://"):
            raise ValueError("source_url must have http:// or https:// scheme")
        return v_stripped

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v_clean = v.strip()
        if not v_clean:
            return None
        # Disallow path traversal or invalid characters
        if "/" in v_clean or "\\" in v_clean or ".." in v_clean:
            raise ValueError("document_id cannot contain slashes or path traversal characters")
        return v_clean


class RegisterDocumentResponse(BaseModel):
    """Response returned upon successful document registration."""
    job_id: uuid.UUID
    idempotency_key: str
    source_url: str
    document_id: Optional[str] = None
    status: JobStatus
    stage: ProcessingStage
    is_duplicate_registration: bool
    message: str
    searchable: bool = False
    publication_status: str = "not_published"
    created_at: datetime


class JobStatusResponse(BaseModel):
    """Status details for an ingestion job."""
    job_id: uuid.UUID
    source_url: str
    idempotency_key: str
    status: JobStatus
    stage: ProcessingStage
    document_id: Optional[str] = None
    sha256: Optional[str] = None
    attempt_count: int
    max_attempts: int
    heartbeat_at: Optional[datetime] = None
    next_retry_at: Optional[datetime] = None
    error_details: Optional[Dict[str, Any]] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    is_terminal: bool
    searchable: bool = False
    publication_status: str = "not_published"
    publication_notice: str = (
        "Newly ingested documents are persisted in storage with pgvector embeddings, but not yet "
        "published to the active query index. Publication requires an administrative sync."
    )


class JobListResponse(BaseModel):
    """Paginated list of ingestion jobs."""
    total: int
    limit: int
    offset: int
    jobs: List[JobStatusResponse]


class RetryJobResponse(BaseModel):
    """Response returned after scheduling a deliberate retry for an eligible failed job."""
    job_id: uuid.UUID
    status: JobStatus
    message: str
    attempt_count: int
    next_retry_at: Optional[datetime] = None
