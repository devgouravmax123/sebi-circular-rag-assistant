"""
Data schemas for citation and claim validation in SEBI CSCRF Regulatory Q&A.
"""
from enum import Enum
from typing import List, Dict, Optional, Any
from pydantic import BaseModel, Field


class CitationStatus(str, Enum):
    """
    Status of mechanical citation validation against registered corpus.
    """
    VALID = "VALID"
    INVALID_CHUNK_NOT_FOUND = "INVALID_CHUNK_NOT_FOUND"
    INVALID_DOC_ID_MISMATCH = "INVALID_DOC_ID_MISMATCH"
    INVALID_PAGE_RANGE = "INVALID_PAGE_RANGE"
    INVALID_QUOTATION = "INVALID_QUOTATION"
    INVALID_METADATA_MISMATCH = "INVALID_METADATA_MISMATCH"
    INVALID_MALFORMED = "INVALID_MALFORMED"


class ClaimSupportStatus(str, Enum):
    """
    Evaluation status of a claim against cited evidence.
    Distinguishes mechanical integrity from semantic truth.
    """
    SUPPORTED_DETERMINISTIC = "SUPPORTED_DETERMINISTIC"  # Verifiable by deterministic rules (e.g. exact quote/provision mapping)
    NEEDS_SEMANTIC_REVIEW = "NEEDS_SEMANTIC_REVIEW"      # Plausible/citation valid, but semantic truth requires review / LLM judge
    UNSUPPORTED = "UNSUPPORTED"                          # Citations invalid, missing, or contradictory to claim
    CONTRADICTORY = "CONTRADICTORY"                      # Claim directly conflicts with verified regulatory graph/corpus evidence
    FLAGGED_UNRESOLVED = "FLAGGED_UNRESOLVED"            # Claim asserts current legal status on historically unresolved evidence


class CitationItem(BaseModel):
    """
    A single citation reference provided by a generator, user, or evaluator.
    """
    chunk_id: str = Field(..., description="Corpus chunk ID being cited")
    document_id: Optional[str] = Field(None, description="Reported parent document ID")
    circular_number: Optional[str] = Field(None, description="Reported circular number")
    issue_date: Optional[str] = Field(None, description="Reported circular issue date (YYYY-MM-DD)")
    page: Optional[int] = Field(None, ge=1, description="Specific page cited")
    page_start: Optional[int] = Field(None, ge=1, description="Reported start page")
    page_end: Optional[int] = Field(None, ge=1, description="Reported end page")
    quote: Optional[str] = Field(None, description="Exact quotation claimed from source chunk")


class CitationValidationResult(BaseModel):
    """
    Mechanical validation report for a single citation.
    """
    citation: CitationItem = Field(..., description="Original citation input")
    status: CitationStatus = Field(..., description="Mechanical validity status")
    is_valid: bool = Field(..., description="True if citation meets all mechanical integrity checks")
    failure_reasons: List[str] = Field(default_factory=list, description="Detailed failure explanations")

    # Authoritative corpus data populated from registered corpus upon successful lookup
    authoritative_document_id: Optional[str] = Field(None, description="True parent document ID from corpus")
    authoritative_circular_number: Optional[str] = Field(None, description="True circular number from corpus")
    authoritative_issue_date: Optional[str] = Field(None, description="True issue date from corpus")
    authoritative_page_start: Optional[int] = Field(None, description="True start page from corpus chunk")
    authoritative_page_end: Optional[int] = Field(None, description="True end page from corpus chunk")


class ClaimItem(BaseModel):
    """
    A discrete claim or sentence in a generated answer paired with cited chunk IDs.
    """
    claim_id: str = Field(..., description="Unique identifier for the claim within answer")
    claim_text: str = Field(..., description="Text of the claim or proposition")
    cited_chunk_ids: List[str] = Field(default_factory=list, description="List of chunk IDs cited to support this claim")
    asserts_current_status: bool = Field(
        False,
        description="Whether this claim asserts current/active regulatory validity or compliance requirement",
    )
    asserts_supersession: bool = Field(
        False,
        description="Whether this claim asserts an amendment, rescission, or supersession relationship",
    )


class ClaimValidationResult(BaseModel):
    """
    Validation report for a discrete answer claim.
    """
    claim: ClaimItem = Field(..., description="Original claim item")
    support_status: ClaimSupportStatus = Field(..., description="Deterministic support or review classification")
    is_supported: bool = Field(False, description="True only if deterministically verifiable and proven")
    citations_mechanically_valid: bool = Field(False, description="True if all cited chunk IDs passed mechanical validation")
    status_reason: str = Field(..., description="Detailed explanation of claim validation outcome")
    applicable_chunk_ids: List[str] = Field(default_factory=list, description="Valid chunk IDs supporting this claim")
    flags: List[str] = Field(default_factory=list, description="Regulatory warnings, e.g. unresolved status, reference misrepresentation")


class ValidationPackageReport(BaseModel):
    """
    Overall structured validation report across citations and claims.
    """
    all_citations_valid: bool = Field(..., description="Whether 100% of cited items passed mechanical validation")
    may_proceed_to_synthesis: bool = Field(..., description="Whether evidence package is safe to proceed without blocking errors")
    total_citations: int = Field(0, ge=0, description="Total citations evaluated")
    valid_citations_count: int = Field(0, ge=0, description="Number of valid citations")
    citation_results: List[CitationValidationResult] = Field(default_factory=list, description="Per-citation results")
    claim_results: List[ClaimValidationResult] = Field(default_factory=list, description="Per-claim results")
    summary_flags: List[str] = Field(default_factory=list, description="Aggregate warnings and flags")
