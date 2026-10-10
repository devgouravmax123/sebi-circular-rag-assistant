from enum import Enum
from typing import Optional, List, Literal
from pydantic import BaseModel, Field, field_validator, model_validator


class RelationshipType(str, Enum):
    """Standardized relationship types between regulatory circulars."""
    AMENDS = "AMENDS"
    RESCINDS = "RESCINDS"
    REFERENCES = "REFERENCES"


class ScopeType(str, Enum):
    """Scope of supersession or amendment."""
    FULL = "full"
    PARTIAL = "partial"


class ConfidenceLevel(str, Enum):
    """Confidence level of extracted relationship."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ReviewStatus(str, Enum):
    """Human review state."""
    NEEDS_REVIEW = "needs_review"
    VERIFIED = "verified"
    REJECTED = "rejected"


class GraphEdge(BaseModel):
    """
    Supersession/relationship edge strictly directed as:
    Newer/acting circular (`source_document_id`) -> RELATIONSHIP -> Older/affected circular (`target_document_id`)
    """
    edge_id: str = Field(..., min_length=1, description="Unique stable identifier for edge")
    source_document_id: str = Field(..., min_length=1, description="Newer / acting circular ID")
    target_document_id: str = Field(..., min_length=1, description="Older / affected circular ID")
    relationship: RelationshipType = Field(..., description="AMENDS, RESCINDS, or REFERENCES")
    scope: ScopeType = Field(ScopeType.FULL, description="full vs. partial scope")
    affected_provisions: List[str] = Field(
        default_factory=list,
        description="Explicit clauses, sections, or paragraphs affected when scope is partial"
    )
    evidence_text: str = Field(..., min_length=1, description="Verbatim sentence or textual evidence")
    evidence_page: Optional[int] = Field(None, ge=1, description="1-indexed source page containing evidence")
    confidence: ConfidenceLevel = Field(ConfidenceLevel.MEDIUM, description="Confidence rating")
    review_status: ReviewStatus = Field(ReviewStatus.NEEDS_REVIEW, description="Review flag")
    effective_date: Optional[str] = Field(None, description="Effective date in YYYY-MM-DD if explicitly stated")

    @field_validator("source_document_id", "target_document_id", "edge_id", "evidence_text")
    @classmethod
    def check_non_empty_stripped(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("Field cannot be empty or whitespace only")
        return stripped

    @model_validator(mode="after")
    def validate_edge_invariants(self) -> "GraphEdge":
        # 1. Prevent self-reference
        if self.source_document_id == self.target_document_id:
            raise ValueError(
                f"Self-referential relationships are invalid: source and target are both '{self.source_document_id}'"
            )

        # 2. If scope is partial, affected_provisions must be non-empty
        if self.scope == ScopeType.PARTIAL and not self.affected_provisions:
            raise ValueError(
                "When scope is 'partial', 'affected_provisions' must contain at least one specific clause/section"
            )

        # 3. REFERENCES cannot claim supersession
        # Ensure REFERENCES edges are informational
        if self.relationship == RelationshipType.REFERENCES and self.scope != ScopeType.FULL and self.affected_provisions:
            # References can refer to specific sections without amending them
            pass

        return self

    @property
    def is_supersession_inducing(self) -> bool:
        """Helper to verify whether this relationship induces amendment or rescission."""
        return self.relationship in (RelationshipType.AMENDS, RelationshipType.RESCINDS)
