"""
Regression and validation test suite for Relevance Safeguards and Domain-Agnostic Query Handling.

Covers:
1. Provision Detection and Scope Resolution:
   - Genuine amendment mapping vs incidental mention.
   - Partial amendment only applies when scope matches.
   - REFERENCES edge never implies amendment or supersession.
2. Query Intent Classification:
   - Status, historical, provision-specific, and out-of-scope intent detection.
3. Verification & Safety Gate Invariants:
   - Claim verifier rejects unsupported claims.
   - Out-of-scope topics trigger controlled abstention.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.safety_gate import AnswerDisposition
from app.schemas.grounding import RegulatoryStatus
from app.grounding.resolver import detect_provisions_in_text, ConservativeGraphResolver
from app.schemas.graph import GraphEdge, RelationshipType, ScopeType, ReviewStatus
from app.schemas.graph_collection import GraphEdgeCollection
from app.schemas.retrieval import RetrievalResult
from app.grounding.classifier import QueryIntent, classify_query_intent
from app.schemas.synthesis import (
    SynthesisInput,
    SynthesisOutput,
    SynthesisStatus,
    SynthesisEvidenceItem,
    ClaimCitation,
)
from app.synthesis.verifier import sanitize_and_validate_synthesis_output


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def resolver():
    graph = GraphEdgeCollection()
    graph.add_edge(
        GraphEdge(
            edge_id="edge_test_amend_01",
            source_document_id="doc_clarification_v2",
            target_document_id="doc_baseline_v1",
            relationship=RelationshipType.AMENDS,
            scope=ScopeType.PARTIAL,
            affected_provisions=["ISO 27001"],
            source_clause="Clause 6.11",
            target_clause="Clause 6.11",
            review_status=ReviewStatus.VERIFIED,
            confidence_score=1.0,
            description="Clarification on standard requirements",
            evidence_text="Clause 6.11 is recommended not mandatory for Qualified REs",
        )
    )
    return ConservativeGraphResolver(graph_collection=graph)


# ---------------------------------------------------------------------------
# Provision Detection and Scope Tests
# ---------------------------------------------------------------------------

def test_genuine_iso_27001_clause(resolver):
    text = "All MIIs and Qualified REs shall obtain ISO 27001 certification within 1 year of issuance of CSCRF."
    res = RetrievalResult(
        chunk_id="c_iso",
        document_id="doc_baseline_v1",
        text=text,
        page_start=47,
        page_end=47,
        score=1.0,
        rank=1,
    )
    item = resolver.resolve_item(res)
    assert item.regulatory_status == RegulatoryStatus.PARTIALLY_AMENDED
    assert "doc_clarification_v2" in item.acting_document_ids
    assert any("ISO 27001" in p.provision_id for p in item.mapped_provisions)


def test_incidental_iso_27001_in_cots_audit_paragraph(resolver):
    cots_text = (
        "Empanelled vendor shall test COTS software vulnerabilities and provide VAPT reports. "
        "Auditors should verify PR.IP.S16 mapping against third-party components."
    )
    provisions = detect_provisions_in_text(cots_text)
    assert not any("ISO 27001" in p.provision_id for p in provisions)

    res = RetrievalResult(
        chunk_id="c_cots_chunk",
        document_id="doc_baseline_v1",
        text=cots_text,
        page_start=114,
        page_end=115,
        score=1.0,
        rank=1,
    )
    item = resolver.resolve_item(res)
    assert item.regulatory_status == RegulatoryStatus.UNRESOLVED
    assert "doc_clarification_v2" not in item.acting_document_ids


def test_provision_code_in_unrelated_paragraph():
    unrelated_text = "General system operations and service monitoring overview."
    provisions = detect_provisions_in_text(unrelated_text)
    assert len(provisions) == 0


def test_references_edge_does_not_affect_legal_status():
    custom_graph = GraphEdgeCollection()
    custom_graph.add_edge(
        GraphEdge(
            edge_id="ref_edge_isolated",
            source_document_id="doc_ref_source",
            target_document_id="doc_ref_target",
            relationship=RelationshipType.REFERENCES,
            scope=ScopeType.FULL,
            evidence_text="Reference text",
            review_status=ReviewStatus.VERIFIED,
        )
    )
    custom_resolver = ConservativeGraphResolver(custom_graph)
    text = "Issued guidelines on operational security architecture."
    res = RetrievalResult(
        chunk_id="c_ref_test",
        document_id="doc_ref_target",
        text=text,
        page_start=1,
        page_end=1,
        score=1.0,
        rank=1,
    )
    item = custom_resolver.resolve_item(res)
    assert item.regulatory_status == RegulatoryStatus.INFORMATIONAL
    assert item.regulatory_status != RegulatoryStatus.SUPERSEDED


# ---------------------------------------------------------------------------
# Query Intent Classifier Tests
# ---------------------------------------------------------------------------

def test_classifier_intent_archetypes():
    assert classify_query_intent("What is the mandatory compliance deadline today?") == QueryIntent.CURRENT_STATUS_OR_DEADLINE
    assert classify_query_intent("What was the original implementation deadline?") == QueryIntent.HISTORICAL_DEADLINE
    assert classify_query_intent("What deadline extension was announced in circular 2025/096?") == QueryIntent.CIRCULAR_SPECIFIC_CHANGE
    assert classify_query_intent("What is the ISO 27001 certification requirement under framework?") == QueryIntent.PROVISION_SPECIFIC_REQUIREMENT
    assert classify_query_intent("To whom does the CISO report directly?") == QueryIntent.PROVISION_SPECIFIC_REQUIREMENT
    assert classify_query_intent("Is the framework legally binding on unregulated fintechs?") == QueryIntent.OUT_OF_SCOPE_OR_UNESTABLISHED


# ---------------------------------------------------------------------------
# Out-of-Scope and Adversarial Abstention Tests
# ---------------------------------------------------------------------------

def test_unregulated_fintech_abstention(client):
    response = client.post("/api/v1/query", json={"query": "Is the framework legally binding on unregulated fintechs?"})
    assert response.status_code == 200
    data = response.json()
    assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION
    assert len(data["verified_excerpts"]) == 0


def test_adversarial_named_external_framework_abstains(client):
    response = client.post(
        "/api/v1/query",
        json={"query": "What are the technical controls required under DORA TLPT threat-led penetration testing?"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION
    assert len(data["verified_excerpts"]) == 0
    assert data["synthesis_status"] == "ABSTAINED"


def test_claim_verifier_rejects_unsupported_audit_claims():
    fake_index_evidence = SynthesisEvidenceItem(
        chunk_id="test_doc_p1_0",
        document_id="test_doc",
        circular_number=None,
        title="Table of Contents and Index",
        issue_date="2025-01-10",
        sha256="abc",
        page_start=1,
        page_end=2,
        text="Table 1: Index of Chapters. Chapter Title and Overview.",
        regulatory_status=RegulatoryStatus.ACTIVE_CURRENT.value,
        status_reason="Active baseline",
        mapped_provisions=[],
    )

    unsupported_claim = ClaimCitation(
        claim_text="The audit report must include an executive summary and comprehensive root cause analysis.",
        supporting_chunk_ids=["test_doc_p1_0"],
    )

    synth_input = SynthesisInput(
        query="What elements must be included in an audit report?",
        evidence_items=[fake_index_evidence],
    )

    synth_output = SynthesisOutput(
        status=SynthesisStatus.FULLY_ANSWERED,
        answer="The audit report must include an executive summary and comprehensive root cause analysis.",
        claims=[unsupported_claim],
    )

    validated_output = sanitize_and_validate_synthesis_output(synth_output, synth_input)
    assert validated_output.status == SynthesisStatus.UNSUPPORTED_CLAIMS_REJECTED
    assert len(validated_output.claims) == 0
    assert "Controlled Abstention" in validated_output.answer
