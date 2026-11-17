from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone


class DocumentRecord(BaseModel):
    """Metadata and status record for an ingested document."""
    document_id: str = Field(..., description="Stable internal unique identifier")
    circular_number: Optional[str] = Field(None, description="Official circular reference number")
    title: str = Field(..., description="Official or source title")
    department: Optional[str] = Field(None, description="Issuing department")
    issue_date: Optional[str] = Field(None, description="Official issue date in YYYY-MM-DD")
    effective_date: Optional[str] = Field(None, description="Effective date in YYYY-MM-DD")
    source_url: str = Field(..., description="Official source URL")
    retrieved_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    sha256: str = Field(..., description="SHA-256 checksum of source PDF")
    pdf_path: str = Field(..., description="Relative path to stored PDF artifact")
    extraction_status: str = Field("pending_review", description="Status of extraction")
    regulator: Optional[str] = Field(None, description="Authority or organization code if applicable")
    domain: Optional[str] = Field(None, description="Domain identifier if applicable")
    collection_id: Optional[str] = Field(None, description="Stable collection identifier")
    workspace_id: Optional[Any] = Field(None, description="Owning workspace identifier")


def resolve_collection_identity(document_id: str, declared_collection_id: Optional[str] = None) -> Optional[str]:
    """
    Deterministically resolves collection identity for a document.
    Returns declared_collection_id if provided and non-empty, otherwise None.
    """
    if declared_collection_id and declared_collection_id.strip():
        return declared_collection_id.strip()
    return None


class PageContent(BaseModel):
    """Extracted text and metadata for a single PDF page."""
    page_number: int = Field(..., ge=1, description="1-indexed physical page number")
    text: str = Field(..., description="Extracted raw text from page")
    is_ocr: bool = Field(False, description="Whether text was derived from OCR fallback")
    extraction_quality: str = Field("ok", description="Quality flag: ok, sparse, garbled, empty")
    char_count: int = Field(0, description="Character count on page")


class ChunkRecord(BaseModel):
    """Deterministic chunk record for indexing and provenance."""
    chunk_id: str = Field(..., description="Stable chunk ID: {document_id}_p{page_start}_{index}")
    document_id: str = Field(..., description="Parent document identifier")
    collection_id: Optional[str] = Field(None, description="Collection identifier matching parent document")
    text: str = Field(..., description="Text content of the chunk")
    page_start: int = Field(..., ge=1, description="Starting 1-indexed page number")
    page_end: int = Field(..., ge=1, description="Ending 1-indexed page number")
    is_ocr: bool = Field(False, description="Whether chunk contains OCR text")
    chunk_type: str = Field("body", description="Type: body, table, annexure")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Arbitrary extra metadata")
