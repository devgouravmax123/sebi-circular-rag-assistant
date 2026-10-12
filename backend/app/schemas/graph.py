"""
Standardized schemas for regulatory relationship edges, review lifecycles, and audit history.
"""
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List, Literal, Dict, Any
from pydantic import BaseModel, Field, field_validator, model_validator


class RelationshipType(str, Enum):
    """
    Standardized relationship types between regulatory circulars / provisions.

    Semantics:
    - AMENDS: Source document/provision modifies, alters, or extends provisions of target.
    - SUPERSEDES: Source document/provision completely or partially overrides/supplants target.
    - REPLACES: Source document completely displaces target.
    - RESCINDS: Source explicitly revokes/repeals target document or clause.
    - REFERENCES: Source mentions or cites target for context without altering legal force.
    - CLARIFIES: Source provides explanatory guidance or interpretation on target provisions.
    """
    AMENDS = "AMENDS"
    SUPERSEDES = "SUPERSEDES"
    REPLACES = "REPLACES"
    RESCINDS = "RESCINDS"
    REFERENCES = "REFERENCES"
    CLARIFIES = "CLARIFIES"


class ScopeType(str, Enum):
    """Scope of supersession, amendment, or reference."""
    FULL = "full"
    PARTIAL = "partial"


class ConfidenceLevel(str, Enum):
    """Confidence level of extracted relationship."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ReviewStatus(str, Enum):
    """
    Human review lifecycle state.

    Lifecycle:
    - PROPOSED / NEEDS_REVIEW: Candidate relationship awaiting human review. Never influences verified status.
    - VERIFIED: Formally inspected and approved by an authorized reviewer.
    - REJECTED: Candidate relationship reviewed and found incorrect/invalid. Retained for auditability.
    - REVOKED: Previously verified relationship invalidated by audited administrative action.
    """
    PROPOSED = "proposed"
    NEEDS_REVIEW = "needs_review"  # Preserved as backward-compatible alias for PROPOSED
    VERIFIED = "verified"
    REJECTED = "rejected"
    REVOKED = "revoked"


class EdgeProvenance(str, Enum):
    """Provenance indicating how the relationship entered the system."""
    IMPORTED = "imported"
    USER_PROPOSED = "user_proposed"
    SYSTEM_EXTRACTED = "system_extracted"


class EdgeReviewHistoryItem(BaseModel):
    """Immutable audit record of a human review decision or state change."""
    action: str = Field(..., description="Action performed: PROPOSE, VERIFY, REJECT, REVOKE")
    from_status: str = Field(..., description="Prior review status")
    to_status: str = Field(..., description="New review status")
    reviewer_id: str = Field(..., min_length=1, description="Identifier of the human reviewer or system actor")
    reviewer_name: Optional[str] = Field(None, description="Optional name of the reviewer")
    rationale: str = Field(..., min_length=1, description="Audit justification or rationale for decision")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp of the decision"
    )


class GraphEdge(BaseModel):
    """
    Regulatory relationship edge strictly directed as:
    Newer/acting circular (`source_document_id`) -> RELATIONSHIP -> Older/affected circular (`target_document_id`)
    """
    edge_id: str = Field(..., min_length=1, description="Unique stable identifier for edge")
    source_document_id: str = Field(..., min_length=1, description="Newer / acting circular ID")
    target_document_id: str = Field(..., min_length=1, description="Older / affected circular ID")
    relationship: RelationshipType = Field(..., description="AMENDS, SUPERSEDES, REPLACES, RESCINDS, REFERENCES, or CLARIFIES")
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

    # Audit & Provenance Fields
    provenance: EdgeProvenance = Field(EdgeProvenance.IMPORTED, description="Origin of this relationship edge")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Creation timestamp in UTC"
    )
    updated_at: Optional[datetime] = Field(None, description="Last update timestamp in UTC")
    reviewed_by: Optional[str] = Field(None, description="Identity of the last authorized reviewer")
    reviewed_at: Optional[datetime] = Field(None, description="Timestamp of the last review decision")
    review_rationale: Optional[str] = Field(None, description="Rationale for the review decision")
    review_history: List[EdgeReviewHistoryItem] = Field(
        default_factory=list,
        description="Chronological, immutable audit trail of review decisions"
    )

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

        return self

    @property
    def is_supersession_inducing(self) -> bool:
        """Helper to verify whether this relationship induces amendment, supersession, or rescission."""
        return self.relationship in (
            RelationshipType.AMENDS,
            RelationshipType.SUPERSEDES,
            RelationshipType.REPLACES,
            RelationshipType.RESCINDS,
        )

    @property
    def is_verified(self) -> bool:
        """Whether this edge is in VERIFIED review status and legally qualified for grounding traversal."""
        return self.review_status == ReviewStatus.VERIFIED


# Request / Response Schemas for Edge APIs

class ProposeEdgeRequest(BaseModel):
    """Payload to propose a new regulatory relationship."""
    edge_id: Optional[str] = Field(None, description="Optional custom edge_id (auto-generated if omitted)")
    source_document_id: str = Field(..., min_length=1, description="Newer / acting circular ID")
    target_document_id: str = Field(..., min_length=1, description="Older / affected circular ID")
    relationship: RelationshipType = Field(..., description="Relationship type")
    scope: ScopeType = Field(ScopeType.FULL, description="full or partial scope")
    affected_provisions: List[str] = Field(default_factory=list, description="Specific clauses/sections if partial")
    evidence_text: str = Field(..., min_length=1, description="Verbatim quote from source document")
    evidence_page: Optional[int] = Field(None, ge=1, description="Source page number containing quote")
    effective_date: Optional[str] = Field(None, description="Effective date in YYYY-MM-DD")
    confidence: ConfidenceLevel = Field(ConfidenceLevel.MEDIUM, description="Initial confidence rating")
    proposer_id: str = Field(..., min_length=1, description="Identifier of the proposing user / analyst")
    rationale: str = Field(..., min_length=1, description="Rationale for proposing this relationship")


class ReviewEdgeRequest(BaseModel):
    """Payload to submit a human review decision."""
    decision: Literal["verify", "reject"] = Field(..., description="'verify' to approve, 'reject' to reject")
    reviewer_id: str = Field(..., min_length=1, description="Identifier of the human reviewer")
    reviewer_name: Optional[str] = Field(None, description="Optional full name or role of reviewer")
    rationale: str = Field(..., min_length=1, description="Detailed regulatory rationale for review decision")


class RevokeEdgeRequest(BaseModel):
    """Payload to revoke an existing relationship."""
    reviewer_id: str = Field(..., min_length=1, description="Identifier of the authorized actor revoking edge")
    reviewer_name: Optional[str] = Field(None, description="Optional name of actor")
    rationale: str = Field(..., min_length=1, description="Audit justification for revoking relationship")


class ReconcileBaselineEdgesRequest(BaseModel):
    """Payload to trigger idempotent baseline edge reconciliation."""
    source_file: Optional[str] = Field(None, description="Optional path to custom baseline graph JSON")
    allow_overwrite: bool = Field(False, description="If true, allows updating non-verified edges")
