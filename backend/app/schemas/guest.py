"""
Pydantic v2 schemas for ephemeral guest sessions and operations (Milestone 11).
"""
from datetime import datetime
from typing import Optional, List
import uuid

from pydantic import BaseModel, ConfigDict, Field


class GuestQuotas(BaseModel):
    """Resource quotas applied strictly to ephemeral guest sessions to protect infrastructure."""
    max_collections: int = 1
    max_documents: int = 2
    max_upload_bytes: int = 10 * 1024 * 1024  # 10 MB total
    max_queries: int = 10
    ttl_hours: int = 24


class GuestSessionCreateResponse(BaseModel):
    """Response returned upon successful guest session creation."""
    session_id: str
    token: str
    expires_at: datetime
    quotas: GuestQuotas = Field(default_factory=GuestQuotas)

    model_config = ConfigDict(from_attributes=True)


class GuestSessionStatusResponse(BaseModel):
    """Real-time server-side status of an active or expired guest session."""
    session_id: str
    is_active: bool
    expires_at: datetime
    time_remaining_seconds: int
    collections_count: int
    documents_count: int
    bytes_uploaded: int
    queries_count: int
    quotas: GuestQuotas = Field(default_factory=GuestQuotas)

    model_config = ConfigDict(from_attributes=True)


class GuestSessionTerminationResponse(BaseModel):
    """Authoritative server-side confirmation of guest data deletion and session termination."""
    session_id: str
    is_active: bool
    cleaned_up: bool
    deleted_documents_count: int
    deleted_bytes: int
    message: str


class GuestCollectionCreateRequest(BaseModel):
    """Request payload to create a temporary guest collection."""
    name: str = Field(..., min_length=2, max_length=100)
    description: Optional[str] = Field(None, max_length=255)


class GuestCollectionResponse(BaseModel):
    """Response representing a guest-scoped collection."""
    id: str
    name: str
    description: Optional[str] = None
    policy_name: str = "general_static"
    created_at: datetime
    is_temporary: bool = True


class GuestUploadResponse(BaseModel):
    """Response for a temporary PDF document upload in guest mode."""
    job_id: str
    document_id: str
    collection_id: str
    status: str
    message: str


class GuestHistoryItem(BaseModel):
    """Single query history item scoped to an active guest session."""
    id: str
    query_text: str
    collection_id: str
    disposition: str
    answer_text: Optional[str] = None
    created_at: str
    citations_count: int = 0


class GuestHistoryResponse(BaseModel):
    """List of research queries executed within this active guest session."""
    items: List[GuestHistoryItem]
    total: int

