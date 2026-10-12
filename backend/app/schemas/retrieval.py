"""
Data schemas for lexical and hybrid retrieval results.
"""
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field


class RetrievalFilter(BaseModel):
    """
    Optional filter criteria for multi-domain regulatory retrieval.
    Applied consistently to BM25 and dense retrieval before fusion.
    """
    regulator: Optional[str] = Field(None, description="Regulator authority code (e.g. 'SEBI')")
    domain: Optional[str] = Field(None, description="Regulatory framework domain (e.g. 'CSCRF', 'LODR')")
    document_id: Optional[str] = Field(None, description="Filter to a specific circular/document ID")
    start_date: Optional[str] = Field(None, description="Earliest issue_date in YYYY-MM-DD")
    end_date: Optional[str] = Field(None, description="Latest issue_date in YYYY-MM-DD")


class RetrievalResult(BaseModel):
    """
    Provenance-preserving retrieval result item.
    """
    chunk_id: str = Field(..., description="Unique chunk identifier")
    document_id: str = Field(..., description="Parent circular document identifier")
    title: Optional[str] = Field(None, description="Official title of circular")
    circular_number: Optional[str] = Field(None, description="Official circular number")
    issue_date: Optional[str] = Field(None, description="Official circular issue date (YYYY-MM-DD)")
    effective_date: Optional[str] = Field(None, description="Effective date (YYYY-MM-DD)")
    regulator: str = Field("SEBI", description="Regulator code (e.g. 'SEBI')")
    domain: str = Field("CSCRF", description="Regulatory domain (e.g. 'CSCRF', 'LODR')")
    sha256: Optional[str] = Field(None, description="SHA-256 content checksum of document version")
    page_start: int = Field(..., ge=1, description="Starting 1-indexed page number in PDF")
    page_end: int = Field(..., ge=1, description="Ending 1-indexed page number in PDF")
    text: str = Field(..., description="Chunk text content")
    score: float = Field(..., description="BM25 lexical or fused RRF relevance score")
    rank: int = Field(..., ge=1, description="1-indexed rank in result set")
    is_ocr: bool = Field(False, description="Whether chunk text contains OCR")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional provenance or chunk metadata")


class RetrievalResponse(BaseModel):
    """
    Collection response for a search query.
    """
    query: str = Field(..., description="Original search query")
    total_results: int = Field(..., ge=0, description="Total number of results returned")
    results: List[RetrievalResult] = Field(default_factory=list, description="Ranked retrieval results")
