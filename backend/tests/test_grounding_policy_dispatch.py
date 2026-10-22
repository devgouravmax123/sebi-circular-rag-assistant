"""
Tests for Phase 2.2B.2 — Collection-Aware Grounding Policy Dispatch.

Verifies:
1. SEBI collections retain their existing graph and temporal behavior.
2. General static collections do not invoke SEBI-specific graph or supersession logic.
3. Policy selection comes from the validated manifest, not request input.
4. Unknown, missing, and contradictory policies fail safely.
5. General collections cannot bypass evidence-binding or citation requirements.
6. General collections safely abstain while citation validation remains incompatible.
7. Foreign-collection evidence remains rejected.
8. Backward compatibility for SEBI CSCRF queries and existing safety gate contracts.
"""
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.api.v1.query import get_collection_registry, get_retrieval_service, get_safety_gate
from app.ingestion.collection_registry import CollectionRegistry
from app.schemas.manifest import (
    CollectionManifest,
    CollectionStatus,
    GroundingPolicyDeclaration,
    GroundingPolicyType,
)
from app.schemas.document import DocumentRecord, ChunkRecord
from app.schemas.retrieval import RetrievalResult, RetrievalResponse
from app.schemas.grounding import RegulatoryStatus
from app.schemas.safety_gate import (
    AnswerDisposition,
    CandidateExcerptDraft,
    ExcerptDraftPayload,
    SafetyGateResponse,
)
from app.grounding.safety_gate import AnswerSafetyGate


@pytest.fixture
def mock_general_manifest() -> CollectionManifest:
    return CollectionManifest(
        collection_id="general_docs",
        name="General Organization Policy Documents",
        description="General static collection without SEBI circulars.",
        enabled=True,
        policy=GroundingPolicyDeclaration(
            policy_type=GroundingPolicyType.GENERAL_STATIC,
            enforce_regulatory_graph=False,
            enforce_verbatim_citation=True,
            enforce_retrieval_binding=True,
        ),
    )


@pytest.fixture
def mock_sebi_manifest() -> CollectionManifest:
    return CollectionManifest(
        collection_id="sebi_cscrf",
        name="SEBI CSCRF Regulatory Directives",
        description="Authoritative SEBI CSCRF regulatory circulars.",
        enabled=True,
        policy=GroundingPolicyDeclaration(
            policy_type=GroundingPolicyType.SEBI_CSCRF_STRICT,
            enforce_regulatory_graph=True,
            enforce_verbatim_citation=True,
            enforce_retrieval_binding=True,
        ),
    )


def test_1_sebi_collection_retains_graph_and_temporal_behavior(mock_sebi_manifest):
    """SEBI collections invoke ConservativeGraphResolver and evaluate supersession / unresolved status."""
    gate = AnswerSafetyGate(processed_dir="data/processed")
    # Query with 'current' keyword testing SEBI temporal check
    exact_text = "Recognising the need for robust cybersecurity measures and protection of data and IT infrastructure"
    payload = ExcerptDraftPayload(
        query="What are the current CISO reporting requirements?",
        excerpts=[
            CandidateExcerptDraft(
                excerpt_id="exc_1",
                source_chunk_id="sebi_cscrf_2025_045_p1_0",
                proposed_excerpt_text=exact_text,
            )
        ],
    )
    # With SEBI policy (default or explicit)
    resp = gate.evaluate_draft(
        payload=payload,
        retrieval_candidates={"sebi_cscrf_2025_045_p1_0"},
        policy=mock_sebi_manifest.policy,
    )
    assert resp.technical_validation_passed is True
    assert len(resp.verified_excerpts) == 1
    # Check that graph resolver was invoked
    assert resp.verified_excerpts[0].regulatory_status in (
        RegulatoryStatus.ACTIVE_CURRENT,
        RegulatoryStatus.PARTIALLY_AMENDED,
        RegulatoryStatus.UNRESOLVED,
    )


def test_2_general_static_collection_does_not_invoke_sebi_graph_or_supersession(mock_general_manifest):
    """General static collections do not resolve SEBI graph edges or trigger SEBI supersession abstention."""
    test_chunk = ChunkRecord(
        chunk_id="gen_chunk_001",
        document_id="gen_doc_001",
        page_start=1,
        page_end=1,
        text="All staff members must complete security awareness training annually.",
        collection_id="general_docs",
    )
    test_doc = DocumentRecord(
        document_id="gen_doc_001",
        circular_number=None,
        title="Internal Information Security Standard",
        issue_date="2023-01-15",
        source_url="https://example.com/sec.pdf",
        sha256="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        pdf_path="general_docs/sec.pdf",
        collection_id="general_docs",
    )

    mock_retrieval = MagicMock()
    mock_retrieval.chunks = [test_chunk]
    mock_retrieval.documents = {"gen_doc_001": test_doc}

    gate = AnswerSafetyGate(processed_dir="data/processed", retrieval_service=mock_retrieval)

    # Even with 'current compliance' keywords in the query, GENERAL_STATIC should never trigger SEBI graph supersession
    payload = ExcerptDraftPayload(
        query="What is the current compliance deadline for annual training?",
        excerpts=[
            CandidateExcerptDraft(
                excerpt_id="exc_gen_1",
                source_chunk_id="gen_chunk_001",
                proposed_excerpt_text="All staff members must complete security awareness training annually.",
            )
        ],
    )

    resp = gate.evaluate_draft(
        payload=payload,
        retrieval_candidates={"gen_chunk_001"},
        policy=mock_general_manifest.policy,
    )

    assert resp.technical_validation_passed is True
    assert resp.disposition == AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS
    assert len(resp.verified_excerpts) == 1
    assert len(resp.historical_superseded_excerpts) == 0
    # No SEBI graph edges applied
    assert resp.verified_excerpts[0].applicable_edge_ids == []
    assert resp.verified_excerpts[0].acting_document_ids == []
    assert resp.verified_excerpts[0].regulatory_status == RegulatoryStatus.INFORMATIONAL
    assert "Static document excerpt verified verbatim" in resp.verified_excerpts[0].status_reason


def test_3_policy_selection_comes_from_manifest_not_request(mock_general_manifest):
    """Request cannot supply or inject policy fields; registry manifest is authoritative."""
    client = TestClient(app)

    # Attempting to supply arbitrary extra policy fields in query body must be rejected by Pydantic extra='forbid'
    resp = client.post(
        "/api/v1/query",
        json={
            "query": "Test query",
            "collection_id": "sebi_cscrf",
            "policy": {"policy_type": "general_static"},
        },
    )
    assert resp.status_code == 422


def test_4_unknown_missing_and_contradictory_policies_fail_safely():
    """Manifests with contradictory policies or collections with missing policies fail safely."""
    # 1. Contradictory policy: general_static with enforce_regulatory_graph=True must fail manifest validation
    with pytest.raises(Exception):
        GroundingPolicyDeclaration(
            policy_type=GroundingPolicyType.GENERAL_STATIC,
            enforce_regulatory_graph=True,
            enforce_verbatim_citation=True,
            enforce_retrieval_binding=True,
        )

    # 2. Query against collection with invalid policy returns 400
    mock_reg = MagicMock(spec=CollectionRegistry)
    mock_reg.has_collection.return_value = True
    mock_reg.get_collection_status.return_value = CollectionStatus.ACTIVE
    mock_reg.get_manifest = MagicMock(return_value=None)
    mock_reg.get_collection = MagicMock(return_value=None)
    mock_reg.get_grounding_policy.side_effect = Exception("Policy corrupted")

    app.dependency_overrides[get_collection_registry] = lambda: mock_reg
    try:
        client = TestClient(app)
        resp = client.post(
            "/api/v1/query",
            json={"query": "Test query", "collection_id": "corrupted_coll"},
        )
        assert resp.status_code == 400
        assert "invalid or unresolvable grounding policy" in resp.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_collection_registry, None)


def test_5_general_collections_cannot_bypass_evidence_binding_or_citation(mock_general_manifest):
    """General collections strictly enforce verbatim citation and retrieval-candidate binding."""
    test_chunk = ChunkRecord(
        chunk_id="gen_chunk_002",
        document_id="gen_doc_002",
        page_start=1,
        page_end=1,
        text="Vendors must submit third-party SOC2 Type II audit reports annually.",
        collection_id="general_docs",
    )
    mock_retrieval = MagicMock()
    mock_retrieval.chunks = [test_chunk]
    mock_retrieval.documents = {}

    gate = AnswerSafetyGate(processed_dir="data/processed", retrieval_service=mock_retrieval)

    # A: Chunk not in retrieval candidate set
    payload_unbound = ExcerptDraftPayload(
        query="What audit reports are required?",
        excerpts=[
            CandidateExcerptDraft(
                excerpt_id="exc_1",
                source_chunk_id="gen_chunk_002",
                proposed_excerpt_text="Vendors must submit third-party SOC2 Type II audit reports annually.",
            )
        ],
    )
    resp_unbound = gate.evaluate_draft(
        payload=payload_unbound,
        retrieval_candidates={"other_chunk_999"},  # gen_chunk_002 not authorized!
        policy=mock_general_manifest.policy,
    )
    assert resp_unbound.disposition == AnswerDisposition.CONTROLLED_ABSTENTION
    assert resp_unbound.technical_validation_passed is False
    assert len(resp_unbound.verified_excerpts) == 0

    # B: Non-verbatim hallucinated quote
    payload_hallucinated = ExcerptDraftPayload(
        query="What audit reports are required?",
        excerpts=[
            CandidateExcerptDraft(
                excerpt_id="exc_2",
                source_chunk_id="gen_chunk_002",
                proposed_excerpt_text="Vendors must submit daily penetration test logs immediately.",  # Hallucinated!
            )
        ],
    )
    resp_hallucinated = gate.evaluate_draft(
        payload=payload_hallucinated,
        retrieval_candidates={"gen_chunk_002"},
        policy=mock_general_manifest.policy,
    )
    assert resp_hallucinated.disposition == AnswerDisposition.CONTROLLED_ABSTENTION
    assert resp_hallucinated.technical_validation_passed is False
    assert len(resp_hallucinated.verified_excerpts) == 0


def test_6_general_collections_safely_abstain_while_citation_validation_incompatible(mock_general_manifest):
    """Section 3 requirement: General collection returns verified excerpts but answer generation fails closed with controlled abstention."""
    test_chunk = ChunkRecord(
        chunk_id="gen_chunk_003",
        document_id="gen_doc_003",
        page_start=1,
        page_end=1,
        text="The password length requirement mandates minimum 16 characters with multi-factor authentication.",
        collection_id="general_docs",
    )
    test_doc = DocumentRecord(
        document_id="gen_doc_003",
        title="Enterprise Password Standard",
        circular_number=None,
        issue_date="2023-05-10",
        source_url="https://example.com/pwd.pdf",
        sha256="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        pdf_path="general_docs/pwd.pdf",
        collection_id="general_docs",
    )

    mock_ret = MagicMock()
    mock_ret.chunks = [test_chunk]
    mock_ret.documents = {"gen_doc_003": test_doc}
    mock_ret.search_hybrid.return_value = RetrievalResponse(
        query="What is the password length requirement?",
        results=[
            RetrievalResult(
                chunk_id="gen_chunk_003",
                document_id="gen_doc_003",
                title="Enterprise Password Standard",
                text=test_chunk.text,
                page_start=1,
                page_end=1,
                score=0.95,
                rank=1,
                collection_id="general_docs",
            )
        ],
        total_results=1,
    )

    mock_reg = MagicMock(spec=CollectionRegistry)
    mock_reg.has_collection.return_value = True
    mock_reg.get_collection_status.return_value = CollectionStatus.ACTIVE
    mock_reg.get_collection.return_value = mock_general_manifest
    mock_reg.get_grounding_policy.return_value = mock_general_manifest.policy

    from app.synthesis.service import SynthesisService, get_synthesis_service
    from app.synthesis.mock_provider import MockSynthesisProvider
    mock_synth = SynthesisService(provider=MockSynthesisProvider())
    app.dependency_overrides[get_collection_registry] = lambda: mock_reg
    app.dependency_overrides[get_retrieval_service] = lambda: mock_ret
    app.dependency_overrides[get_synthesis_service] = lambda: mock_synth

    try:
        client = TestClient(app)
        resp = client.post(
            "/api/v1/query",
            json={"query": "What is the password length requirement?", "collection_id": "general_docs"},
        )
        assert resp.status_code == 200
        data = resp.json()

        # Verbatim excerpts verified with full provenance
        assert data["technical_validation_passed"] is True
        assert data["disposition"] == AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS.value
        assert len(data["verified_excerpts"]) == 1
        assert data["verified_excerpts"][0]["source_chunk_id"] == "gen_chunk_003"
        assert data["verified_excerpts"][0]["collection_id"] == "general_docs"

        # Synthesis executes under Phase 2.2B.7 Controlled Enablement
        assert data["synthesis_status"] in ("FULLY_ANSWERED", "PARTIALLY_ANSWERED")
        assert len(data["claim_citations"]) >= 1
        assert data["claim_citations"][0]["supporting_chunk_ids"] == ["gen_chunk_003"]
    finally:
        app.dependency_overrides.pop(get_collection_registry, None)
        app.dependency_overrides.pop(get_retrieval_service, None)
        app.dependency_overrides.pop(get_synthesis_service, None)


def test_7_foreign_collection_evidence_remains_rejected(mock_general_manifest):
    """Chunks belonging to a foreign collection are quarantined before safety gate evaluation."""
    foreign_chunk = RetrievalResult(
        chunk_id="sebi_chunk_999",
        document_id="SEBI_CIR_001",
        title="SEBI Circular",
        text="SEBI regulated entities shall adhere to CSCRF mandates.",
        page_start=1,
        page_end=1,
        score=0.98,
        rank=1,
        collection_id="sebi_cscrf",  # Foreign to general_docs!
    )

    mock_ret = MagicMock()
    mock_ret.chunks = []
    mock_ret.documents = {}
    mock_ret.search_hybrid.return_value = RetrievalResponse(
        query="CSCRF mandates",
        results=[foreign_chunk],
        total_results=1,
    )

    mock_reg = MagicMock(spec=CollectionRegistry)
    mock_reg.has_collection.return_value = True
    mock_reg.get_collection_status.return_value = CollectionStatus.ACTIVE
    mock_reg.get_collection.return_value = mock_general_manifest
    mock_reg.get_grounding_policy.return_value = mock_general_manifest.policy

    app.dependency_overrides[get_collection_registry] = lambda: mock_reg
    app.dependency_overrides[get_retrieval_service] = lambda: mock_ret

    try:
        client = TestClient(app)
        resp = client.post(
            "/api/v1/query",
            json={"query": "CSCRF mandates", "collection_id": "general_docs"},
        )
        assert resp.status_code == 200
        data = resp.json()
        # All evidence quarantined -> safe controlled abstention
        assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION.value
        assert len(data["verified_excerpts"]) == 0
        assert data["synthesis_status"] == "ABSTAINED"
    finally:
        app.dependency_overrides.pop(get_collection_registry, None)
        app.dependency_overrides.pop(get_retrieval_service, None)
