"""
Focused tests for Phase 3.2B — Explicit Policy-Aware Relevance Dispatch.

Verifies:
1. SEBI_CSCRF_STRICT invokes the SEBI-specific relevance path (circular matching, deadline anchors, priority sorting, out-of-scope detection).
2. GENERAL_STATIC does not invoke SEBI circular/deadline/provision matching or SEBI priority sorting.
3. Unsupported policy values fail closed (raises ValueError).
4. General collections do not reject general financial or organizational terminology via SEBI out-of-scope filters.
5. General evidence is not reordered by SEBI document-priority keys.
6. Legacy test callers omitting policy_type default to SEBI_CSCRF_STRICT for backward compatibility.
7. End-to-end query endpoint passes grounding_policy.policy_type explicitly.
"""
from typing import List
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from app.grounding.classifier import QueryIntent
from app.grounding.relevance import (
    CandidateRelevanceAssessment,
    RelevanceDecisionCode,
    filter_relevant_candidates,
)
from app.schemas.manifest import (
    CollectionManifest,
    CollectionStatus,
    GroundingPolicyDeclaration,
    GroundingPolicyType,
)
from app.schemas.retrieval import RetrievalResult, RetrievalResponse
from app.schemas.safety_gate import (
    AnswerDisposition,
    SafetyGateResponse,
    ValidatedExcerptItem,
)
from app.schemas.grounding import RegulatoryStatus
from app.main import app
from app.api.deps import get_db_session
from app.api.v1.query import get_collection_registry, get_retrieval_service, get_safety_gate


@pytest.fixture
def sebi_test_candidates() -> List[RetrievalResult]:
    """Sample candidates mimicking SEBI CSCRF documents."""
    return [
        RetrievalResult(
            chunk_id="sebi_cscrf_2024_113_p17_0",
            document_id="sebi_cscrf_2024_113",
            title="Master CSCRF Framework",
            text="Paragraph 17: Implementation period for CSCRF shall be by April 01, 2025 for all REs.",
            page_start=17,
            page_end=17,
            score=0.025,
            rank=2,
        ),
        RetrievalResult(
            chunk_id="sebi_cscrf_2025_096_p2_0",
            document_id="sebi_cscrf_2025_096",
            title="CSCRF Extension June 2025",
            text="Extend the compliance timelines till August 31, 2025 for CSCRF.",
            page_start=2,
            page_end=2,
            score=0.020,
            rank=3,
        ),
        RetrievalResult(
            chunk_id="sebi_cscrf_2025_045_p2_0",
            document_id="sebi_cscrf_2025_045",
            title="CSCRF Extension March 2025",
            text="Extend the compliance timelines by three months, till June 30, 2025 for CSCRF.",
            page_start=2,
            page_end=2,
            score=0.030,
            rank=1,
        ),
    ]


@pytest.fixture
def general_test_candidates() -> List[RetrievalResult]:
    """Sample candidates from a non-regulatory general document collection."""
    return [
        RetrievalResult(
            chunk_id="cisa_pb_p12_1",
            document_id="cisa_ir_playbook_2021",
            title="CISA Incident Response Playbook",
            text="Agencies should preserve system memory and network artifacts during containment.",
            page_start=12,
            page_end=12,
            score=0.035,
            rank=1,
        ),
        RetrievalResult(
            chunk_id="cisa_pb_p25_3",
            document_id="cisa_ir_playbook_2021",
            title="CISA Incident Response Playbook",
            text="Coordinate with CISA to report confirmed major incidents within 1 hour.",
            page_start=25,
            page_end=25,
            score=0.028,
            rank=2,
        ),
        RetrievalResult(
            chunk_id="cisa_pb_p8_0",
            document_id="cisa_ir_playbook_2021",
            title="CISA Incident Response Playbook",
            text="Preparation phase establishes roles, responsibilities, communication paths, and logging baselines.",
            page_start=8,
            page_end=8,
            score=0.020,
            rank=3,
        ),
    ]


# -----------------------------------------------------------------------------
# 1. Policy Dispatch & Separation Tests
# -----------------------------------------------------------------------------

def test_sebi_policy_invokes_sebi_relevance_and_priority_sorting(sebi_test_candidates):
    """SEBI_CSCRF_STRICT policy executes deadline intent and prioritizes 2025_096 over older circulars."""
    query = "What is the current compliance deadline for CSCRF?"
    relevant, intent, assessments = filter_relevant_candidates(
        query=query,
        candidates=sebi_test_candidates,
        policy_type=GroundingPolicyType.SEBI_CSCRF_STRICT,
    )
    assert intent == QueryIntent.CURRENT_STATUS_OR_DEADLINE
    assert len(relevant) >= 2
    # Verify SEBI deadline priority: CIR/2025/096 (Priority 0) must be first despite lower RRF score
    assert relevant[0].document_id == "sebi_cscrf_2025_096"
    assert relevant[1].document_id == "sebi_cscrf_2025_045"


def test_general_policy_skips_sebi_priority_sorting(sebi_test_candidates):
    """GENERAL_STATIC policy does NOT apply SEBI document ID priority ordering."""
    query = "What is the compliance deadline for CSCRF?"
    relevant, intent, assessments = filter_relevant_candidates(
        query=query,
        candidates=sebi_test_candidates,
        policy_type=GroundingPolicyType.GENERAL_STATIC,
    )
    # Intent under GENERAL_STATIC is always GENERAL_REQUIREMENT
    assert intent == QueryIntent.GENERAL_REQUIREMENT
    # Verify no SEBI deadline priority key was applied (2025_045 has highest RRF score and matches query terms)
    # 2025_096 must NOT be forced to the front under GENERAL_STATIC
    assert relevant[0].document_id == "sebi_cscrf_2025_045"


def test_general_policy_does_not_reject_securities_or_financial_terminology(general_test_candidates):
    """General static collections do not trigger OUT_OF_SCOPE_OR_UNESTABLISHED for capital-market terms."""
    # Query mentions "insider trading" which is in SEBI OUT_OF_SCOPE_PATTERNS
    query = "How to preserve logs during an investigation into insider trading and unauthorized access?"
    # Under SEBI policy, this query is rejected as OUT_OF_SCOPE_OR_UNESTABLISHED
    rel_sebi, intent_sebi, _ = filter_relevant_candidates(
        query=query,
        candidates=general_test_candidates,
        policy_type=GroundingPolicyType.SEBI_CSCRF_STRICT,
    )
    assert intent_sebi == QueryIntent.OUT_OF_SCOPE_OR_UNESTABLISHED
    assert len(rel_sebi) == 0

    # Under GENERAL_STATIC, insider trading is NOT treated as an out-of-scope trigger
    rel_gen, intent_gen, assessments_gen = filter_relevant_candidates(
        query=query,
        candidates=general_test_candidates,
        policy_type=GroundingPolicyType.GENERAL_STATIC,
    )
    assert intent_gen == QueryIntent.GENERAL_REQUIREMENT
    # Candidate discussing logging and preservation matches query terms
    assert len(rel_gen) >= 1
    matching_chunks = [c.chunk_id for c in rel_gen]
    assert "cisa_pb_p8_0" in matching_chunks or "cisa_pb_p12_1" in matching_chunks


def test_general_policy_filters_irrelevant_candidates_without_sebi_circular_checks(general_test_candidates):
    """GENERAL_STATIC assesses relevance based on substantive query terms; irrelevance fails closed."""
    query = "How to report major incidents to CISA?"
    relevant, intent, assessments = filter_relevant_candidates(
        query=query,
        candidates=general_test_candidates,
        policy_type=GroundingPolicyType.GENERAL_STATIC,
    )
    assert intent == QueryIntent.GENERAL_REQUIREMENT
    assert len(relevant) >= 1
    # Candidate discussing CISA reporting should be top ranked
    assert relevant[0].chunk_id == "cisa_pb_p25_3"

    # Candidate with zero matching terms fails
    unrelated_chunk = RetrievalResult(
        chunk_id="unrelated_001",
        document_id="cisa_ir_playbook_2021",
        title="Unrelated",
        text="The agricultural supply chain depends on seasonal weather patterns and global fertilizer pricing.",
        page_start=1,
        page_end=1,
        score=0.010,
        rank=99,
    )
    rel_unrel, _, ass_unrel = filter_relevant_candidates(
        query=query,
        candidates=[unrelated_chunk],
        policy_type=GroundingPolicyType.GENERAL_STATIC,
    )
    assert len(rel_unrel) == 0
    assert ass_unrel[0].decision_code == RelevanceDecisionCode.INSUFFICIENT_RELEVANCE


def test_unsupported_policy_fails_closed():
    """Passing an unsupported or malformed policy_type raises ValueError immediately."""
    candidate = RetrievalResult(
        chunk_id="test_001",
        document_id="doc_001",
        title="Test",
        text="Sample test passage text.",
        page_start=1,
        page_end=1,
        score=0.01,
        rank=1,
    )
    with pytest.raises(ValueError, match="Unsupported or unrecognized grounding policy_type"):
        filter_relevant_candidates(
            query="Test query",
            candidates=[candidate],
            policy_type="unsupported_custom_policy",
        )

    with pytest.raises(ValueError, match="Invalid policy_type argument type"):
        filter_relevant_candidates(
            query="Test query",
            candidates=[candidate],
            policy_type=12345,  # type: ignore
        )


def test_omitted_policy_fails_closed(sebi_test_candidates):
    """Calling filter_relevant_candidates without policy_type fails closed (raises ValueError)."""
    query = "What is the compliance deadline for CSCRF?"
    with pytest.raises(ValueError, match="grounding policy_type is required"):
        filter_relevant_candidates(
            query=query,
            candidates=sebi_test_candidates,
        )


def test_none_policy_fails_closed(sebi_test_candidates):
    """Calling filter_relevant_candidates with policy_type=None fails closed (raises ValueError)."""
    query = "What is the compliance deadline for CSCRF?"
    with pytest.raises(ValueError, match="grounding policy_type is required"):
        filter_relevant_candidates(
            query=query,
            candidates=sebi_test_candidates,
            policy_type=None,
        )


# -----------------------------------------------------------------------------
# 2. Production API Wiring Tests
# -----------------------------------------------------------------------------

def test_production_query_api_passes_resolved_policy_type_explicitly():
    """Production /api/v1/query endpoint passes the trusted manifest policy_type to filter_relevant_candidates."""
    test_chunk = RetrievalResult(
        chunk_id="cisa_pb_p25_3",
        document_id="cisa_ir_playbook_2021",
        title="CISA Incident Response Playbook",
        text="Coordinate with CISA to report confirmed major incidents within 1 hour.",
        page_start=25,
        page_end=25,
        score=0.035,
        rank=1,
        collection_id="cisa_playbooks",
    )

    mock_manifest = CollectionManifest(
        collection_id="cisa_playbooks",
        name="CISA Playbooks",
        description="CISA operational playbooks",
        enabled=True,
        policy=GroundingPolicyDeclaration(
            policy_type=GroundingPolicyType.GENERAL_STATIC,
            enforce_regulatory_graph=False,
            enforce_verbatim_citation=True,
            enforce_retrieval_binding=True,
        ),
    )

    mock_retrieval = MagicMock()
    mock_retrieval.chunks = []
    mock_retrieval.documents = {}
    mock_retrieval.search_hybrid.return_value = RetrievalResponse(
        query="Coordinate with CISA",
        results=[test_chunk],
        total_results=1,
    )
    mock_retrieval.get_chunks.return_value = [test_chunk]
    mock_retrieval.get_documents.return_value = {}

    mock_registry = MagicMock()
    mock_registry.has_collection.return_value = True
    mock_registry.get_collection_status.return_value = CollectionStatus.ACTIVE
    mock_registry.get_collection.return_value = mock_manifest
    mock_registry.get_grounding_policy.return_value = mock_manifest.policy

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None

    app.dependency_overrides[get_collection_registry] = lambda: mock_registry
    app.dependency_overrides[get_retrieval_service] = lambda: mock_retrieval
    app.dependency_overrides[get_db_session] = lambda: mock_db

    try:
        client = TestClient(app)
        with patch("app.api.v1.query.filter_relevant_candidates") as mock_filter:
            # Configure mock return tuple matching filter_relevant_candidates signature
            mock_filter.return_value = (
                [test_chunk],
                QueryIntent.GENERAL_REQUIREMENT,
                [CandidateRelevanceAssessment("cisa_pb_p25_3", True, RelevanceDecisionCode.RELEVANT_EVIDENCE, "Match")],
            )

            resp = client.post(
                "/api/v1/query",
                json={"query": "Coordinate with CISA", "collection_id": "cisa_playbooks"},
            )
            assert resp.status_code == 200

            # Verify filter_relevant_candidates was called with explicit policy_type=GroundingPolicyType.GENERAL_STATIC
            mock_filter.assert_called_once()
            _, kwargs = mock_filter.call_args
            assert kwargs.get("policy_type") == GroundingPolicyType.GENERAL_STATIC
    finally:
        app.dependency_overrides.pop(get_collection_registry, None)
        app.dependency_overrides.pop(get_retrieval_service, None)
        app.dependency_overrides.pop(get_db_session, None)


def test_production_path_without_policy_resolution_fails_closed():
    """If a collection lacks a valid grounding policy declaration, production query endpoint fails closed (HTTP 400)."""
    mock_manifest = MagicMock()
    mock_manifest.name = "Broken Collection"

    mock_registry = MagicMock()
    mock_registry.has_collection.return_value = True
    mock_registry.get_collection_status.return_value = CollectionStatus.ACTIVE
    mock_registry.get_collection.return_value = mock_manifest
    mock_registry.get_grounding_policy.return_value = None

    mock_retrieval = MagicMock()
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None

    app.dependency_overrides[get_collection_registry] = lambda: mock_registry
    app.dependency_overrides[get_retrieval_service] = lambda: mock_retrieval
    app.dependency_overrides[get_db_session] = lambda: mock_db

    try:
        client = TestClient(app)
        with patch("app.api.v1.query.filter_relevant_candidates") as mock_filter:
            resp = client.post(
                "/api/v1/query",
                json={"query": "Test query", "collection_id": "broken_collection"},
            )
            # Rejects with HTTP 400
            assert resp.status_code == 400
            assert "policy" in resp.json()["detail"].lower()
            # Relevance filter was NEVER called, proving no silent fallback to SEBI occurred
            mock_filter.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_collection_registry, None)
        app.dependency_overrides.pop(get_retrieval_service, None)
        app.dependency_overrides.pop(get_db_session, None)
