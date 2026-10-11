"""
FastAPI Request and Response schemas for POST /api/v1/query.
"""
from typing import List, Optional
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
        description="Natural language question regarding SEBI CSCRF circular provisions (2 to 500 characters).",
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
            "Educational & Pilot Assistant: Operating strictly over the 5-document SEBI CSCRF circular corpus "
            "(CIR/2024/113, CIR/2025/045, CIR/2025/060, CIR/2025/096, CIR/2025/119). "
            "Excerpts represent technical document grounding only and do not establish universal legal currency."
        ),
        description="Authoritative notice defining the 5-document pilot boundary.",
    )
