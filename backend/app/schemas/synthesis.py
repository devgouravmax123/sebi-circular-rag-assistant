"""
Pydantic data schemas for Grounded LLM Synthesis (Phase 8E).

Defines:
- SynthesisStatus: Provider execution and answer grounding lifecycle.
- CitationProvenance: Traceable link mapping an individual claim to an authorized corpus chunk.
- ClaimCitation: Claim-level sentence assertion with supporting citations.
- UnresolvedSubquestion: Explicit disclosure for unaddressed parts of multi-part questions.
- SynthesisInput: Provider-neutral input containing user query, verified evidence, and graph relationships.
- SynthesisOutput: Validated synthesis result with claim citations and coverage disclosures.
"""
from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, ConfigDict, model_validator


class SynthesisStatus(str, Enum):
    """
    Status of the LLM synthesis operation.
    """
    FULLY_ANSWERED = "FULLY_ANSWERED"          # Answer is completely supported by grounded evidence
    PARTIALLY_ANSWERED = "PARTIALLY_ANSWERED"      # Some parts supported, but gaps/omissions disclosed
    ABSTAINED = "ABSTAINED"                      # Gate or synthesis safely abstained due to insufficient evidence
    PROVIDER_FAILED = "PROVIDER_FAILED"          # Provider timed out, was unavailable, or errored safely
    UNSUPPORTED_CLAIMS_REJECTED = "UNSUPPORTED_CLAIMS_REJECTED" # Output contained unsupported claims and was rejected


class CitationProvenance(BaseModel):
    """
    Verified citation provenance mapping directly to an authorized retrieved chunk.
    Never invents missing metadata: if page or provision is absent in the source data, it remains None.
    """
    model_config = ConfigDict(extra="forbid")

    source_chunk_id: str = Field(..., description="Unique chunk identifier in corpus")
    document_id: str = Field(..., description="Parent document identifier (e.g. sebi_cscrf_2024_113)")
    circular_number: Optional[str] = Field(None, description="Official circular reference number if present")
    title: Optional[str] = Field(None, description="Official circular title if present")
    issue_date: Optional[str] = Field(None, description="Circular issue date in YYYY-MM-DD if present")
    sha256: Optional[str] = Field(None, description="Document content checksum if present")
    page_start: Optional[int] = Field(None, ge=1, description="1-indexed start page in PDF if present")
    page_end: Optional[int] = Field(None, ge=1, description="1-indexed end page in PDF if present")
    provision_id: Optional[str] = Field(None, description="Mapped provision ID (e.g. PR.IP.S16) if present")
    regulatory_status: str = Field(
        ...,
        description="Conservative regulatory status (e.g. UNRESOLVED, ACTIVE_CURRENT, SUPERSEDED)",
    )
    collection_id: Optional[str] = Field(
        default=None,
        description="Authoritative collection identifier for provenance tracing",
    )


class ClaimCitation(BaseModel):
    """
    Claim-level assertion paired with explicit supporting citations.
    """
    model_config = ConfigDict(extra="forbid")

    claim_text: str = Field(..., min_length=1, description="Factual claim asserted in the synthesized answer")
    supporting_chunk_ids: List[str] = Field(
        ...,
        min_length=1,
        description="Chunk IDs whose verbatim text directly supports this claim",
    )
    citations: List[CitationProvenance] = Field(
        default_factory=list,
        description="Resolved authoritative provenance objects for each supporting chunk ID",
    )


class UnresolvedSubquestion(BaseModel):
    """
    Explicit disclosure for requested topics, conditions, or multi-part questions
    that cannot be fully substantiated from the supplied retrieved evidence.
    """
    model_config = ConfigDict(extra="forbid")

    subquestion: str = Field(..., description="The specific subquestion, requirement, or detail requested")
    reason: str = Field(..., description="Clear explanation of why evidence was insufficient or missing")


class SynthesisEvidenceItem(BaseModel):
    """
    Typed evidence context supplied to the provider-neutral synthesis interface.
    """
    model_config = ConfigDict(extra="forbid")

    chunk_id: str = Field(..., description="Authoritative chunk identifier")
    document_id: str = Field(..., description="Parent circular document identifier")
    text: str = Field(..., description="Verbatim passage text from retrieved chunk")
    page_start: Optional[int] = Field(None, ge=1, description="Starting 1-indexed page in PDF")
    page_end: Optional[int] = Field(None, ge=1, description="Ending 1-indexed page in PDF")
    circular_number: Optional[str] = Field(None, description="Official circular reference number")
    title: Optional[str] = Field(None, description="Official circular title")
    issue_date: Optional[str] = Field(None, description="Issue date in YYYY-MM-DD")
    sha256: Optional[str] = Field(None, description="Document version checksum")
    regulatory_status: str = Field(..., description="Regulatory status determined by graph traversal")
    status_reason: str = Field(..., description="Explanation for assigned regulatory status")
    mapped_provisions: List[str] = Field(default_factory=list, description="Associated provision IDs")
    applicable_edge_ids: List[str] = Field(default_factory=list, description="Graph edge IDs influencing this passage")
    collection_id: Optional[str] = Field(
        default="sebi_cscrf",
        description="Authoritative collection identifier for evidence chunk",
    )


class SynthesisInput(BaseModel):
    """
    Provider-neutral synthesis request payload.
    Contains user query, bounded evidence, and verified relationship metadata.
    """
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=2, max_length=500, description="Normalized user query")
    evidence_items: List[SynthesisEvidenceItem] = Field(
        default_factory=list,
        description="Verified retrieval candidates passed by relevance and safety gate",
    )
    regulatory_warnings: List[str] = Field(
        default_factory=list,
        description="Warnings regarding UNRESOLVED status or scoped amendments",
    )
    domain: Optional[str] = Field(None, description="Regulatory framework domain (e.g. CSCRF)")
    collection_id: Optional[str] = Field(
        default="sebi_cscrf",
        description="Target collection identifier for contextual traceability",
    )
    policy_type: Optional[str] = Field(
        default="sebi_cscrf_strict",
        description="Grounding policy type governing prompt formatting and grounding rules",
    )


class SynthesisOutput(BaseModel):
    """
    Provider-neutral synthesis response payload.
    Strictly forbids unexpected fields.
    """
    model_config = ConfigDict(extra="forbid")

    status: SynthesisStatus = Field(..., description="Synthesis execution and grounding outcome")
    answer: str = Field(..., description="Synthesized plain-text or markdown answer grounded in evidence")
    claims: List[ClaimCitation] = Field(
        default_factory=list,
        description="Claim-level breakdown of the synthesized answer with explicit supporting citations",
    )
    unresolved_subquestions: List[UnresolvedSubquestion] = Field(
        default_factory=list,
        description="Disclosures of subquestions or details not substantiated by retrieved evidence",
    )
    failure_reason: Optional[str] = Field(
        None,
        description="Safe explanation if synthesis failed, abstained, or provider was unavailable",
    )
