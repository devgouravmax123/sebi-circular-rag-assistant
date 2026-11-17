"""
FastAPI Request and Response schemas for POST /api/v1/query.
"""
from typing import List, Optional, Any
from pydantic import BaseModel, Field, ConfigDict, field_validator

from app.schemas.safety_gate import (
    AnswerDisposition,
    ValidatedExcerptItem,
    SafetyGateResponse,
)


class QueryRequest(BaseModel):
    """
    Request model for regulatory search and grounded excerpt retrieval.
    Strictly forbids unexpected / unknown fields.
    """
    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        ...,
        min_length=2,
        max_length=500,
        description="Natural language question regarding indexed document provisions (2 to 500 characters).",
    )
    top_k: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Number of candidate passages to retrieve (1 to 10).",
    )
    include_superseded: bool = Field(
        default=False,
        description="Whether to include historical superseded provisions if asking historical questions.",
    )
    regulator: Optional[str] = Field(
        default=None,
        description="Optional authority or regulator code (e.g. 'SEC', 'CISA').",
    )
    domain: Optional[str] = Field(
        default=None,
        description="Optional domain filter.",
    )
    document_id: Optional[str] = Field(
        default=None,
        description="Optional filter targeting a specific document ID.",
    )
    start_date: Optional[str] = Field(
        default=None,
        description="Optional earliest document date (YYYY-MM-DD).",
    )
    end_date: Optional[str] = Field(
        default=None,
        description="Optional latest document date (YYYY-MM-DD).",
    )
    collection_id: str = Field(
        default="general_synthetic",
        description="Target document collection identifier.",
    )

    @field_validator("collection_id")
    @classmethod
    def validate_collection_id(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("collection_id cannot be empty or whitespace.")
        import re
        if not re.match(r"^[a-zA-Z0-9_-]+$", s):
            raise ValueError(
                f"collection_id '{s}' contains invalid characters; must contain only alphanumeric characters, underscores, or hyphens."
            )
        return s

    @field_validator("regulator")
    @classmethod
    def validate_regulator(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            norm = v.strip().upper()
            return norm if norm else None
        return v

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            stripped = v.strip().upper()
            if not stripped:
                return None
            return stripped
        return v

    @field_validator("query")
    @classmethod
    def validate_non_whitespace_query(cls, v: str) -> str:
        stripped = v.strip()
        if len(stripped) < 2:
            raise ValueError("Query must contain at least 2 non-whitespace characters.")
        if len(stripped) > 500:
            raise ValueError("Query cannot exceed 500 characters.")
        return stripped


class QueryResponse(BaseModel):
    """
    Response model for regulatory grounded query API endpoint.
    Strictly distinguishes verified evidence excerpts, historical context, and abstentions.
    """
    query: str = Field(..., description="Normalized user query")
    disposition: AnswerDisposition = Field(..., description="Deterministic response disposition")
    verified_excerpts: List[ValidatedExcerptItem] = Field(
        default_factory=list,
        description="Server-validated historical evidence excerpts. Empty on abstention.",
    )
    historical_superseded_excerpts: List[ValidatedExcerptItem] = Field(
        default_factory=list,
        description="Separated historical excerpts known to be superseded in the corpus (if include_superseded=True).",
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
            "Citenza Document Intelligence Assistant: Operating over indexed collection documents. "
            "Excerpts represent technical document grounding only and do not constitute formal legal advice."
        ),
        description="Notice defining the collection grounding boundary.",
    )
    # Phase 8E: Grounded LLM Synthesis fields (backward compatible)
    answer_text: Optional[str] = Field(
        None,
        description="Synthesized regulatory answer strictly grounded in verified evidence.",
    )
    synthesis_status: Optional[str] = Field(
        None,
        description="Status of answer synthesis (e.g. FULLY_ANSWERED, PARTIALLY_ANSWERED, ABSTAINED, PROVIDER_FAILED).",
    )
    claim_citations: List[Any] = Field(
        default_factory=list,
        description="Structured claim-level citations tying specific answer assertions to source chunks.",
    )
    unresolved_subquestions: List[Any] = Field(
        default_factory=list,
        description="Disclosures of subquestions or details not substantiated by retrieved evidence.",
    )
    synthesis_failure_reason: Optional[str] = Field(
        None,
        description="Safe, sanitized explanation if synthesis failed or could not be completed.",
    )
