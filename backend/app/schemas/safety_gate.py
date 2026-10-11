"""
Data schemas for the deterministic answer safety gate and draft excerpt verification in SEBI CSCRF.
"""
from enum import Enum
from typing import List, Optional, Dict, Any, Set
from pydantic import BaseModel, Field, ConfigDict, model_validator

from app.schemas.grounding import RegulatoryStatus


class AnswerDisposition(str, Enum):
    """
    Deterministic response disposition emitted by the safety gate.
    Reflects verified excerpt-level grounding rather than a proven legal determination.
    """
    VERIFIED_HISTORICAL_EXCERPTS = "VERIFIED_HISTORICAL_EXCERPTS"  # Excerpts verified from corpus chunks with resolved status
    QUALIFIED_HISTORICAL_EXCERPTS = "QUALIFIED_HISTORICAL_EXCERPTS"# Excerpts verified from corpus with UNRESOLVED pilot status
    CONTROLLED_ABSTENTION = "CONTROLLED_ABSTENTION"                # Insufficient evidence, out of retrieval scope, or all superseded
    REJECTED_UNSAFE = "REJECTED_UNSAFE"                            # Malformed payload, injection, or severe invariant violation


class CandidateExcerptDraft(BaseModel):
    """
    Untrusted candidate excerpt draft submitted by drafting agent / model.
    Strictly forbids extra / unknown fields.
    """
    model_config = ConfigDict(extra="forbid")

    excerpt_id: str = Field(..., min_length=1, description="Unique identifier for candidate excerpt")
    source_chunk_id: str = Field(..., min_length=1, description="Corpus chunk ID claimed to contain excerpt")
    proposed_excerpt_text: str = Field(..., min_length=1, description="Verbatim text proposed from source chunk")


class ExcerptDraftPayload(BaseModel):
    """
    Untrusted draft container payload containing candidate excerpts.
    Strictly forbids extra / unknown fields.
    """
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, description="User query or proposition")
    excerpts: List[CandidateExcerptDraft] = Field(default_factory=list, description="Candidate excerpts to validate")

    @model_validator(mode="after")
    def validate_unique_excerpt_ids(self) -> "ExcerptDraftPayload":
        ids: Set[str] = set()
        for e in self.excerpts:
            if e.excerpt_id in ids:
                raise ValueError(f"Duplicate excerpt_id '{e.excerpt_id}' found in draft payload.")
            ids.add(e.excerpt_id)
        return self


class ValidatedExcerptItem(BaseModel):
    """
    Server-validated excerpt record populated with authoritative corpus and graph provenance.
    """
    excerpt_id: str = Field(..., description="ID of validated excerpt")
    source_chunk_id: str = Field(..., description="Authoritative chunk ID")
    document_id: str = Field(..., description="Authoritative parent circular document ID")
    circular_number: Optional[str] = Field(None, description="Authoritative circular number")
    title: Optional[str] = Field(None, description="Official circular title")
    issue_date: Optional[str] = Field(None, description="Authoritative circular issue date")
    page_start: int = Field(..., ge=1, description="Authoritative start page")
    page_end: int = Field(..., ge=1, description="Authoritative end page")
    excerpt_text: str = Field(..., description="Verified verbatim excerpt text")
    regulatory_status: RegulatoryStatus = Field(..., description="Conservative regulatory status from graph resolver")
    status_reason: str = Field(..., description="Explicit rationale for regulatory status")
    applicable_edge_ids: List[str] = Field(default_factory=list, description="Graph edge IDs influencing this passage")
    acting_document_ids: List[str] = Field(default_factory=list, description="Newer circulars acting on this passage")


class SafetyGateResponse(BaseModel):
    """
    Final deterministic API response contract emitted by the safety gate.
    """
    query: str = Field(..., description="Original user query")
    disposition: AnswerDisposition = Field(..., description="Final deterministic disposition")
    verified_excerpts: List[ValidatedExcerptItem] = Field(
        default_factory=list,
        description="Server-validated historical excerpts. Empty on abstention.",
    )
    historical_superseded_excerpts: List[ValidatedExcerptItem] = Field(
        default_factory=list,
        description="Separated historical excerpts known to be superseded in current corpus.",
    )
    summary_narrative: str = Field(
        ...,
        description="Server-constructed deterministic explanation of disposition and status.",
    )
    technical_validation_passed: bool = Field(
        ...,
        description="True if 100% of rendered excerpts passed server-side mechanical validation. NOT legal authority.",
    )
    retrieval_candidate_count: int = Field(
        0,
        ge=0,
        description="Number of authorized candidate chunks supplied from server-side hybrid retrieval.",
    )
    abstention_reason: Optional[str] = Field(
        None,
        description="Explicit reason for controlled abstention if disposition is CONTROLLED_ABSTENTION.",
    )
    regulatory_warnings: List[str] = Field(
        default_factory=list,
        description="Explicit warnings regarding UNRESOLVED status or scoped amendments.",
    )
    pilot_corpus_scope_limitation: str = Field(
        default=(
            "Educational & Pilot Assistant: Operating strictly over the 5-document SEBI CSCRF circular corpus "
            "(CIR/2024/113, CIR/2025/045, CIR/2025/060, CIR/2025/096, CIR/2025/119). "
            "Excerpts represent technical document grounding only and do not establish universal legal currency."
        ),
        description="Authoritative notice defining the 5-document pilot boundary.",
    )
