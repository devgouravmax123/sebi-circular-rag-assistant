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
    regulator: str = Field("SEBI", description="Regulatory authority code (e.g. SEBI)")
    domain: str = Field("CSCRF", description="Regulatory domain (e.g. CSCRF, LODR, ICDR)")


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
    text: str = Field(..., description="Text content of the chunk")
    page_start: int = Field(..., ge=1, description="Starting 1-indexed page number")
    page_end: int = Field(..., ge=1, description="Ending 1-indexed page number")
    is_ocr: bool = Field(False, description="Whether chunk contains OCR text")
    chunk_type: str = Field("body", description="Type: body, table, annexure")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Arbitrary extra metadata")
