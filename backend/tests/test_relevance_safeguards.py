"""
Regression and validation test suite for Relevance Safeguards and Archetype Queries (Phase 6G.3).

Covers:
1. 8 Target Archetypes:
   - "What is the mandatory compliance deadline today?"
   - "What was the original CSCRF implementation deadline?"
   - "What deadline extension was announced in circular 2025/096?"
   - "What is the ISO 27001 certification requirement under CSCRF?"
   - "To whom does the CISO report directly?"
   - "What is the daily mutual fund NAV cutoff?" (Out of scope -> Abstain)
   - "What are the vendor empanelment testing requirements for COTS?" (No spurious ISO 27001 mapping)
   - "Is the 2024 CSCRF framework legally binding on unregulated fintechs?" (Out of scope -> Abstain)
2. Provision mapping precision:
   - Genuine ISO 27001 clause vs incidental COTS mention.
   - Partial amendment only applies when scope matches.
   - REFERENCES edge never implies amendment or supersession.
3. Citation validation and SafetyGate invariants preserved.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.safety_gate import AnswerDisposition
from app.schemas.grounding import RegulatoryStatus
from app.grounding.resolver import detect_provisions_in_text, ConservativeGraphResolver
from app.schemas.graph import GraphEdge, RelationshipType, ScopeType, ReviewStatus
from app.schemas.graph_collection import GraphEdgeCollection
from app.schemas.cscrf_graph_data import create_verified_cscrf_graph
from app.schemas.retrieval import RetrievalResult
from app.grounding.classifier import QueryIntent, classify_query_intent


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def resolver():
    return ConservativeGraphResolver(graph_collection=create_verified_cscrf_graph())


# ---------------------------------------------------------------------------
# Provision Detection and Scope Tests
# ---------------------------------------------------------------------------

def test_genuine_iso_27001_clause(resolver):
    text = "All MIIs and Qualified REs shall obtain ISO 27001 certification within 1 year of issuance of CSCRF."
    res = RetrievalResult(
        chunk_id="c_iso",
        document_id="sebi_cscrf_2024_113",
        text=text,
        page_start=47,
        page_end=47,
        score=1.0,
        rank=1,
    )
    item = resolver.resolve_item(res)
    assert item.regulatory_status == RegulatoryStatus.PARTIALLY_AMENDED
    assert "sebi_cscrf_2025_119" in item.acting_document_ids
    assert any("ISO 27001" in p.provision_id for p in item.mapped_provisions)


def test_incidental_iso_27001_in_cots_audit_paragraph(resolver):
    # COTS chunk: contains PR.IP.S16 in an audit table or COTS context without operative ISO clause
    cots_text = (
        "Empanelled vendor shall test COTS software vulnerabilities and provide VAPT reports. "
        "Auditors should verify PR.IP.S16 mapping against third-party components."
    )
    provisions = detect_provisions_in_text(cots_text)
    # Must NOT map to ISO 27001
    assert not any("ISO 27001" in p.provision_id for p in provisions)

    res = RetrievalResult(
        chunk_id="sebi_cscrf_2024_113_p114_95",
        document_id="sebi_cscrf_2024_113",
        text=cots_text,
        page_start=114,
        page_end=115,
        score=1.0,
        rank=1,
    )
    item = resolver.resolve_item(res)
    assert item.regulatory_status == RegulatoryStatus.UNRESOLVED
    assert "sebi_cscrf_2025_119" not in item.acting_document_ids


def test_provision_code_in_unrelated_paragraph():
    unrelated_text = "General market operations and secondary market surveillance overview."
    provisions = detect_provisions_in_text(unrelated_text)
    assert len(provisions) == 0


def test_references_edge_does_not_affect_legal_status():
    # Dedicated graph with only REFERENCES relationship
    custom_graph = GraphEdgeCollection()
    custom_graph.add_edge(
        GraphEdge(
            edge_id="ref_edge_isolated",
            source_document_id="sebi_cscrf_2025_060",
            target_document_id="sebi_cscrf_2025_045",
            relationship=RelationshipType.REFERENCES,
            scope=ScopeType.FULL,
            evidence_text="Reference text",
            review_status=ReviewStatus.VERIFIED,
        )
    )
    custom_resolver = ConservativeGraphResolver(custom_graph)
    text = "SEBI issued circular on cybersecurity framework compliance timelines."
    res = RetrievalResult(
        chunk_id="c_ref_test",
        document_id="sebi_cscrf_2025_045",
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
    assert classify_query_intent("What was the original CSCRF implementation deadline?") == QueryIntent.HISTORICAL_DEADLINE
    assert classify_query_intent("What deadline extension was announced in circular 2025/096?") == QueryIntent.CIRCULAR_SPECIFIC_CHANGE
    assert classify_query_intent("What is the ISO 27001 certification requirement under CSCRF?") == QueryIntent.PROVISION_SPECIFIC_REQUIREMENT
    assert classify_query_intent("To whom does the CISO report directly?") == QueryIntent.PROVISION_SPECIFIC_REQUIREMENT
    assert classify_query_intent("What is the daily mutual fund NAV cutoff?") == QueryIntent.OUT_OF_SCOPE_OR_UNESTABLISHED
    assert classify_query_intent("Is the 2024 CSCRF framework legally binding on unregulated fintechs?") == QueryIntent.OUT_OF_SCOPE_OR_UNESTABLISHED


# ---------------------------------------------------------------------------
# End-to-End Archetype API Tests (8 Core Archetypes)
# ---------------------------------------------------------------------------

def test_archetype_1_current_deadline(client):
    response = client.post("/api/v1/query", json={"query": "What is the mandatory compliance deadline today?"})
    assert response.status_code == 200
    data = response.json()
    assert data["disposition"] == AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS
    assert len(data["verified_excerpts"]) > 0
    # Must retrieve CIR/2025/096 extension passage
    assert any("2025_096" in exc["document_id"] for exc in data["verified_excerpts"])
    assert data["verified_excerpts"][0]["document_id"] == "sebi_cscrf_2025_096"
    # Must NOT contain ISO 27001 recommendation as evidence
    for exc in data["verified_excerpts"]:
        assert "Clause 6.11" not in exc.get("status_reason", "")


def test_archetype_2_original_deadline(client):
    response = client.post("/api/v1/query", json={"query": "What was the original CSCRF implementation deadline?"})
    assert response.status_code == 200
    data = response.json()
    assert data["disposition"] == AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS
    assert len(data["verified_excerpts"]) > 0
    # Provenance should include baseline CIR/2024/113
    assert any("2024_113" in exc["document_id"] for exc in data["verified_excerpts"])


def test_archetype_3_circular_096_extension(client):
    response = client.post("/api/v1/query", json={"query": "What deadline extension was announced in circular 2025/096?"})
    assert response.status_code == 200
    data = response.json()
    assert data["disposition"] == AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS
    assert len(data["verified_excerpts"]) > 0
    # Must retrieve circular 2025_096
    assert any("2025_096" in exc["document_id"] for exc in data["verified_excerpts"])


def test_archetype_4_iso_27001_requirement(client):
    response = client.post("/api/v1/query", json={"query": "What is the ISO 27001 certification requirement under CSCRF?"})
    assert response.status_code == 200
    data = response.json()
    assert data["disposition"] in (AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS, AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS)
    assert len(data["verified_excerpts"]) > 0
    # Must have genuine ISO 27001 excerpt
    assert any("iso" in exc["excerpt_text"].lower() for exc in data["verified_excerpts"])


def test_archetype_5_ciso_reporting(client):
    response = client.post("/api/v1/query", json={"query": "To whom does the CISO report directly?"})
    assert response.status_code == 200
    data = response.json()
    assert data["disposition"] == AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS
    assert len(data["verified_excerpts"]) > 0
    assert any("ciso" in exc["excerpt_text"].lower() for exc in data["verified_excerpts"])


def test_archetype_6_nav_cutoff_abstention(client):
    response = client.post("/api/v1/query", json={"query": "What is the daily mutual fund NAV cutoff?"})
    assert response.status_code == 200
    data = response.json()
    assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION
    assert len(data["verified_excerpts"]) == 0
    assert "outside the demonstrated regulatory scope" in data["summary_narrative"]


def test_archetype_7_cots_testing(client):
    response = client.post("/api/v1/query", json={"query": "What are the vendor empanelment testing requirements for COTS?"})
    assert response.status_code == 200
    data = response.json()
    assert data["disposition"] in (AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS, AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS)
    assert len(data["verified_excerpts"]) > 0
    # Any excerpt discussing COTS vendor empanelment is from CIR/2024/113
    assert any("cots" in exc["excerpt_text"].lower() for exc in data["verified_excerpts"])


def test_archetype_8_unregulated_fintech_abstention(client):
    response = client.post("/api/v1/query", json={"query": "Is the 2024 CSCRF framework legally binding on unregulated fintechs?"})
    assert response.status_code == 200
    data = response.json()
    assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION
    assert len(data["verified_excerpts"]) == 0
    assert "outside the demonstrated regulatory scope" in data["summary_narrative"]
