"""
Grounded evidence and regulatory status data schemas for SEBI CSCRF.
"""
from enum import Enum
from typing import List, Dict, Optional, Any
from pydantic import BaseModel, Field


class RegulatoryStatus(str, Enum):
    """
    Conservative regulatory status assigned to a retrieved passage after graph traversal.
    """
    UNRESOLVED = "UNRESOLVED"                  # Default conservative state: no verified full/partial amendment or baseline status in scope
    INFORMATIONAL = "INFORMATIONAL"            # Non-superseding background context (e.g. from REFERENCES)
    PARTIALLY_AMENDED = "PARTIALLY_AMENDED"    # Verified partial amendment explicitly affects this provision
    SUPERSEDED = "SUPERSEDED"                  # Entirely superseded by verified full-scope amendment/rescission with established legal effect
    ACTIVE_CURRENT = "ACTIVE_CURRENT"          # Explicitly active / unmodified provision supported by complete verified evidence


class ProvisionMapping(BaseModel):
    """
    Deterministic mapping between a retrieved chunk and a regulatory provision.
    """
    provision_id: str = Field(..., description="Canonical provision identifier (e.g. 'PR.IP.S16', 'Section 2.1')")
    provision_title: Optional[str] = Field(None, description="Human readable title or clause name")
    matched_term: Optional[str] = Field(None, description="Exact phrase or pattern triggering provision detection")


class GroundedEvidenceItem(BaseModel):
    """
    Provenance-preserving grounded evidence item with conservative regulatory status annotation.
    """
    chunk_id: str = Field(..., description="Unique chunk identifier in corpus")
    document_id: str = Field(..., description="Parent document identifier")
    title: Optional[str] = Field(None, description="Official title of circular")
    circular_number: Optional[str] = Field(None, description="Official circular reference number")
    issue_date: Optional[str] = Field(None, description="Official issue date (YYYY-MM-DD)")
    page_start: int = Field(..., ge=1, description="Starting 1-indexed page in PDF")
    page_end: int = Field(..., ge=1, description="Ending 1-indexed page in PDF")
    text: str = Field(..., description="Verbatim chunk text content")
    retrieval_score: float = Field(..., description="Score from retrieval stage (BM25, dense cosine, or RRF)")
    retrieval_rank: int = Field(..., ge=1, description="1-indexed rank in result set")
    is_ocr: bool = Field(False, description="Whether text contains OCR extraction")

    # Grounding annotations
    regulatory_status: RegulatoryStatus = Field(
        RegulatoryStatus.UNRESOLVED,
        description="Conservative regulatory status determined by graph traversal",
    )
    status_reason: str = Field(..., description="Explicit, verifiable explanation for status assignment")
    mapped_provisions: List[ProvisionMapping] = Field(
        default_factory=list,
        description="Specific provisions mapped within this chunk",
    )
    applicable_edge_ids: List[str] = Field(
        default_factory=list,
        description="IDs of verified graph edges influencing this passage",
    )
    acting_document_ids: List[str] = Field(
        default_factory=list,
        description="IDs of newer circulars amending or referencing this passage",
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Diagnostic metadata (e.g. component retriever ranks)",
    )


class GroundedEvidencePackage(BaseModel):
    """
    Package containing all resolved evidence passages for a given query.
    """
    query: str = Field(..., description="Search query")
    items: List[GroundedEvidenceItem] = Field(default_factory=list, description="Resolved evidence items")
    total_retrieved: int = Field(0, ge=0, description="Total retrieved passages before resolution")
    active_count: int = Field(0, ge=0, description="Count of ACTIVE_CURRENT items")
    partially_amended_count: int = Field(0, ge=0, description="Count of PARTIALLY_AMENDED items")
    superseded_count: int = Field(0, ge=0, description="Count of SUPERSEDED items")
    unresolved_count: int = Field(0, ge=0, description="Count of UNRESOLVED items")
    informational_count: int = Field(0, ge=0, description="Count of INFORMATIONAL items")
