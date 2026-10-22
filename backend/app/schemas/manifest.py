"""
Schemas for Phase 1 manifest-driven document ingestion.
Enforces validation for collection manifests, document definitions, and ingestion results.
"""
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, field_validator


class ManifestDocumentItem(BaseModel):
    """Single document entry declared in a collection manifest."""
    document_id: str = Field(..., min_length=1, description="Unique, stable document identifier")
    title: str = Field(..., min_length=1, description="Human-readable document title")
    pdf_path: str = Field(..., min_length=1, description="Repository-relative path to PDF artifact")
    circular_number: Optional[str] = Field(None, description="Official reference number")
    department: Optional[str] = Field(None, description="Issuing department or division")
    issue_date: Optional[str] = Field(None, description="Official issue date in YYYY-MM-DD")
    effective_date: Optional[str] = Field(None, description="Effective date in YYYY-MM-DD")
    document_type: Optional[str] = Field(None, description="Document type (e.g. master_framework, extension)")
    source_url: Optional[str] = Field(None, description="Official source URL")
    expected_sha256: Optional[str] = Field(None, description="Expected SHA-256 for integrity verification")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Arbitrary extra metadata")

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("document_id cannot be empty or whitespace")
        return s

    @field_validator("title")
    @classmethod
    def validate_title(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("title cannot be empty or whitespace")
        return s

    @field_validator("pdf_path")
    @classmethod
    def validate_pdf_path(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("pdf_path cannot be empty or whitespace")
        return s


from enum import Enum
from pydantic import model_validator


class GroundingPolicyType(str, Enum):
    """Supported grounding and regulatory relationship resolution policies."""
    SEBI_CSCRF_STRICT = "sebi_cscrf_strict"
    GENERAL_STATIC = "general_static"


class GroundingPolicyDeclaration(BaseModel):
    """
    Explicit declaration of the grounding and relationship resolution policy for a collection.
    Configures whether graph supersession checks, temporal resolution, or static citation verification apply.
    """
    policy_type: GroundingPolicyType = Field(
        ...,
        description="Type of grounding policy explicitly applicable to this collection",
    )
    enforce_regulatory_graph: bool = Field(
        ...,
        description="Whether this collection resolves and enforces regulatory supersession edges",
    )
    enforce_verbatim_citation: bool = Field(
        default=True,
        description="Whether candidate excerpts must match verbatim against authoritative chunk text (never bypassable)",
    )
    enforce_retrieval_binding: bool = Field(
        default=True,
        description="Whether cited chunks must belong to authorized server-side retrieval set (never bypassable)",
    )

    @field_validator("enforce_verbatim_citation")
    @classmethod
    def validate_verbatim_invariant(cls, v: bool) -> bool:
        if v is not True:
            raise ValueError("enforce_verbatim_citation is a mandatory security invariant and cannot be disabled (must be true)")
        return v

    @field_validator("enforce_retrieval_binding")
    @classmethod
    def validate_binding_invariant(cls, v: bool) -> bool:
        if v is not True:
            raise ValueError("enforce_retrieval_binding is a mandatory security invariant and cannot be disabled (must be true)")
        return v

    @model_validator(mode="after")
    def validate_policy_graph_consistency(self) -> "GroundingPolicyDeclaration":
        if self.policy_type == GroundingPolicyType.SEBI_CSCRF_STRICT and not self.enforce_regulatory_graph:
            raise ValueError("Grounding policy 'sebi_cscrf_strict' requires enforce_regulatory_graph=True")
        if self.policy_type == GroundingPolicyType.GENERAL_STATIC and self.enforce_regulatory_graph:
            raise ValueError("Grounding policy 'general_static' requires enforce_regulatory_graph=False")
        return self


class CollectionStatus(str, Enum):
    """Administrative and operational status of a collection."""
    ACTIVE = "active"
    DISABLED = "disabled"
    UNPUBLISHED = "unpublished"


class CollectionManifest(BaseModel):
    """Collection manifest definition conforming to Phase 1 & Phase 2 data pipeline standards."""
    collection_id: str = Field(..., min_length=1, description="Stable collection identifier")
    name: str = Field(..., min_length=1, description="Human-readable collection name")
    description: Optional[str] = Field(None, description="Scope and purpose description")
    regulator: Optional[str] = Field(None, description="Optional regulator code (e.g. SEBI)")
    domain: Optional[str] = Field(None, description="Optional domain code (e.g. CSCRF)")
    enabled: bool = Field(True, description="Whether collection is enabled for query and retrieval")
    policy: GroundingPolicyDeclaration = Field(
        ...,
        description="Explicit grounding policy declaration",
    )
    enforce_regulatory_graph: Optional[bool] = Field(
        None,
        description="Optional legacy mirror; if specified, must strictly match policy.enforce_regulatory_graph",
    )
    documents: List[ManifestDocumentItem] = Field(default_factory=list, description="Documents in collection")

    @field_validator("collection_id")
    @classmethod
    def validate_collection_id(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("collection_id cannot be empty or whitespace")
        # Validate collection_id format (alphanumeric with underscores or dashes)
        import re
        if not re.match(r"^[a-zA-Z0-9_-]+$", s):
            raise ValueError(f"collection_id '{s}' contains invalid characters; must be alphanumeric, dashes, or underscores.")
        return s

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("name cannot be empty or whitespace")
        return s

    @model_validator(mode="after")
    def sync_and_validate_graph_enforcement(self) -> "CollectionManifest":
        # Ensure single source of truth: policy.enforce_regulatory_graph is authoritative
        policy_graph = self.policy.enforce_regulatory_graph
        if self.enforce_regulatory_graph is not None and self.enforce_regulatory_graph != policy_graph:
            raise ValueError(
                f"Conflicting graph enforcement declarations: top-level enforce_regulatory_graph={self.enforce_regulatory_graph} "
                f"contradicts policy.enforce_regulatory_graph={policy_graph}."
            )
        # Populate top-level mirror for callers expecting the attribute
        self.enforce_regulatory_graph = policy_graph
        return self


class ManifestIngestionOutcome(BaseModel):
    """Result of ingesting an individual document in a manifest."""
    document_id: str
    status: str  # "success", "duplicate", "error"
    message: str
    sha256: str = ""
    page_count: int = 0
    chunk_count: int = 0
    pdf_path: str = ""


class ManifestIngestionReport(BaseModel):
    """Overall execution report for a manifest-driven ingestion run."""
    manifest_path: str
    collection_id: str
    total_declared: int = 0
    success_count: int = 0
    duplicate_count: int = 0
    error_count: int = 0
    document_outcomes: List[ManifestIngestionOutcome] = Field(default_factory=list)
    index_published: bool = False
    snapshot_id: Optional[str] = None
    publication_error: Optional[str] = None
