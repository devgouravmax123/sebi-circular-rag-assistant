"""
Pydantic schemas for Collection-Aware Ingestion Jobs (Milestone 8 - Task 8.4).
"""
from datetime import datetime
from typing import Optional, Dict, Any, List
import re
import uuid

from pydantic import BaseModel, Field, field_validator


class CollectionIngestionJobCreateRequest(BaseModel):
    """Payload for submitting a document URL to be ingested into an authorized collection."""
    source_url: str = Field(..., description="HTTPS or HTTP URL of PDF to ingest")
    idempotency_key: str = Field(..., min_length=4, max_length=128, description="Client idempotency key")
    document_id: Optional[str] = Field(None, min_length=3, max_length=100, description="Optional logical document ID")
    title: Optional[str] = Field(None, max_length=300, description="Optional document title")
    circular_number: Optional[str] = Field(None, max_length=150, description="Optional circular number")
    max_attempts: int = Field(default=3, ge=1, le=10, description="Maximum retry attempts")

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("source_url cannot be empty.")
        if not s.startswith("https://") and not s.startswith("http://"):
            raise ValueError("source_url must start with https:// or http://.")
        return s

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if not s:
            return None
        if "/" in s or "\\" in s or ".." in s:
            raise ValueError("document_id cannot contain path separators or traversal characters.")
        return s


class CollectionIngestionJobResponse(BaseModel):
    """Public representation of an ingestion job created in a collection."""
    job_id: uuid.UUID
    workspace_id: uuid.UUID
    collection_id: str
    document_id: Optional[str]
    source_url: str
    status: str
    stage: str
    idempotency_key: str
    is_duplicate_registration: bool
    message: str
    created_at: datetime


class CollectionIngestionJobStatusResponse(BaseModel):
    """Safe, sanitized representation of an ingestion job lifecycle status for clients."""
    job_id: uuid.UUID
    workspace_id: uuid.UUID
    collection_id: str
    document_id: Optional[str] = None
    status: str
    stage: str
    idempotency_key: Optional[str] = None
    is_terminal: bool = False
    is_published: bool = False
    error_message: Optional[str] = None
    attempt_count: int = 0
    created_at: datetime
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class CollectionIngestionJobListResponse(BaseModel):
    """List response for ingestion jobs within an authorized workspace collection."""
    jobs: list[CollectionIngestionJobStatusResponse]
    total: int


class CollectionDocumentSummaryResponse(BaseModel):
    """Summary representation of an ingested document within a collection."""
    document_id: str
    workspace_id: uuid.UUID
    collection_id: str
    title: str
    circular_number: Optional[str] = None
    is_published: bool = False
    created_at: Optional[datetime] = None


class CollectionDocumentListResponse(BaseModel):
    """List response for documents within an authorized workspace collection."""
    documents: list[CollectionDocumentSummaryResponse]
    total: int


LEAK_KEYS = {
    "original_document_id",
    "foreign_document_id",
    "internal_document_id",
    "matched_document_id",
    "source_document_id",
    "colliding_document_id",
    "existing_document_id",
}


def sanitize_error_message(message: Optional[str], collection_id: Optional[str] = None) -> Optional[str]:
    """
    Sanitize error message strings to ensure foreign document identifiers
    or unscoped duplicate messages are not leaked across collection or tenant boundaries.
    """
    if not message:
        return message
    col_str = collection_id.strip() if collection_id and collection_id.strip() else "target collection"
    # Replace legacy duplicate message patterns that leak document IDs
    # e.g., "already ingested as 'some_foreign_doc'" or "Identical content SHA-256 already ingested as 'some_doc'"
    msg = re.sub(
        r"(?:Identical content (?:SHA-256 )?)?already ingested as ['\"][^'\"]+['\"]",
        f"Identical content already ingested in collection '{col_str}'",
        message,
        flags=re.IGNORECASE,
    )
    msg = re.sub(
        r"matches existing document ['\"][^'\"]+['\"]",
        f"matches existing document in collection '{col_str}'",
        msg,
        flags=re.IGNORECASE,
    )
    return msg


def sanitize_error_details(details: Any, collection_id: Optional[str] = None) -> Any:
    """
    Recursively sanitize structured error_details dictionaries and lists to strip
    foreign document identifiers, internal IDs, and sanitize nested message strings.
    """
    if details is None:
        return None
    if isinstance(details, dict):
        sanitized = {}
        for k, v in details.items():
            if k in LEAK_KEYS:
                continue
            sanitized[k] = sanitize_error_details(v, collection_id)
        return sanitized
    elif isinstance(details, list):
        return [sanitize_error_details(item, collection_id) for item in details]
    elif isinstance(details, str):
        return sanitize_error_message(details, collection_id)
    return details

