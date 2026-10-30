"""
Grounding package exports for regulatory evidence resolution and citation verification.
"""
from app.grounding.resolver import (
    ConservativeGraphResolver,
    detect_provisions_in_text,
    check_edge_affects_provision,
)
from app.grounding.validator import CitationValidator
from app.grounding.safety_gate import AnswerSafetyGate
from app.schemas.citation import (
    CitationItem,
    CitationStatus,
    CitationValidationResult,
    ClaimItem,
    ClaimSupportStatus,
    ClaimValidationResult,
    ValidationPackageReport,
)
from app.schemas.safety_gate import (
    AnswerDisposition,
    CandidateExcerptDraft,
    ExcerptDraftPayload,
    ValidatedExcerptItem,
    SafetyGateResponse,
)
from app.grounding.scope import (
    ScopeMatchStatus,
    ScopeEvaluationResult,
    evaluate_query_collection_scope,
)

__all__ = [
    "ConservativeGraphResolver",
    "detect_provisions_in_text",
    "check_edge_affects_provision",
    "CitationValidator",
    "AnswerSafetyGate",
    "CitationItem",
    "CitationStatus",
    "CitationValidationResult",
    "ClaimItem",
    "ClaimSupportStatus",
    "ClaimValidationResult",
    "ValidationPackageReport",
    "AnswerDisposition",
    "CandidateExcerptDraft",
    "ExcerptDraftPayload",
    "ValidatedExcerptItem",
    "SafetyGateResponse",
    "ScopeMatchStatus",
    "ScopeEvaluationResult",
    "evaluate_query_collection_scope",
]
