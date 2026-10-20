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


def test_iso_27001_qualified_re_clarification_ordering_and_content(client):
    """
    Step 2 Regression Tests:
    1. Query for Qualified RE ISO 27001 requirements retrieves CIR/2025/119 Clause 6.11 clarification and CIR/2024/113 baseline.
    2. The clarification text that Qualified REs are 'encouraged and recommended (not mandatory)' is preserved.
    3. The older mandatory wording is accompanied by its verified PARTIALLY_AMENDED regulatory status.
    4. Incidental mentions (such as standards listed in CCI capability p85_71) are excluded when direct operative passages exist.
    5. Clarification for Qualified REs does not alter MII mandatory scope.
    6. Currency remains qualified under conservative pilot status (UNRESOLVED for CIR/2025/119).
    """
    response = client.post(
        "/api/v1/query",
        json={"query": "What are the ISO 27001 requirements for Qualified REs?", "top_k": 5}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["disposition"] == AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS
    excerpts = data["verified_excerpts"]
    assert len(excerpts) >= 2

    # Check top excerpt is the CIR/2025/119 Clause 6.11 clarification
    top_ex = excerpts[0]
    assert top_ex["document_id"] == "sebi_cscrf_2025_119"
    assert "encouraged and recommended (not mandatory)" in top_ex["excerpt_text"].lower()
    assert top_ex["regulatory_status"] == RegulatoryStatus.UNRESOLVED

    # Check that baseline CIR/2024/113 is present and marked as PARTIALLY_AMENDED
    baseline_excerpts = [e for e in excerpts if e["document_id"] == "sebi_cscrf_2024_113"]
    assert len(baseline_excerpts) > 0
    for be in baseline_excerpts:
        assert be["regulatory_status"] == RegulatoryStatus.PARTIALLY_AMENDED
        assert "edge_cscrf_2025_119_amends_2024_113_technical_clarifications" in be["applicable_edge_ids"]

    # Verify incidental standards passage (p85_71 / CCI) is NOT included in the top verified excerpts
    chunk_ids = [e["source_chunk_id"] for e in excerpts]
    assert "sebi_cscrf_2024_113_p85_71" not in chunk_ids

    # Verify MII text distinction: MIIs are mandatory in baseline
    assert any("miis and qualified res shall obtain iso 27001" in e["excerpt_text"].lower() for e in baseline_excerpts)



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

# ---------------------------------------------------------------------------
# Phase 8F.2.3: Targeted Adversarial and Precision Tests
# ---------------------------------------------------------------------------

def test_adversarial_named_external_framework_abstains(client):
    """Questions asking about named external frameworks absent from the corpus must abstain."""
    response = client.post(
        "/api/v1/query",
        json={"query": "What are the technical controls required under DORA TLPT threat-led penetration testing?"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION
    assert len(data["verified_excerpts"]) == 0
    assert data["synthesis_status"] == "ABSTAINED"


def test_adversarial_paraphrased_genuine_question_accepted(client):
    """Genuine in-scope requirement with non-verbatim query phrasing must be accepted."""
    response = client.post(
        "/api/v1/query",
        json={"query": "Must offline isolated backups be stored in air-gapped locations to prevent tampering by ransomware?"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["disposition"] == AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS
    assert len(data["verified_excerpts"]) > 0
    assert any("air-gapped" in e["excerpt_text"].lower() or "isolated" in e["excerpt_text"].lower() for e in data["verified_excerpts"])


def test_adversarial_leading_zero_circular_number_accepted(client):
    """Questions referencing circular numbers with leading zeros (e.g. 096) must match document 96."""
    response = client.post(
        "/api/v1/query",
        json={"query": "What extension was granted under circular 096?"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["disposition"] == AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS
    assert len(data["verified_excerpts"]) > 0
    assert any("96" in e["document_id"] or "timeline" in e["excerpt_text"].lower() for e in data["verified_excerpts"])


def test_adversarial_generic_cyber_terms_documented_related_excerpts(client):
    """
    Broad generic cybersecurity query lacking distinguishing subject anchors must result in
    controlled abstention rather than false acceptance (val_unans_08 contract).
    """
    response = client.post(
        "/api/v1/query",
        json={"query": "How should general risk management and operational controls be implemented across financial systems?"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION
    assert len(data["verified_excerpts"]) == 0
    assert data["synthesis_status"] == "ABSTAINED"
    assert "Insufficient evidence" in data["abstention_reason"]

# ---------------------------------------------------------------------------
# Phase 8F.2.4: Targeted Audit Report & Section-Aware Ranking Regressions
# ---------------------------------------------------------------------------

def test_audit_report_instruction_heavy_query_surfaces_annexure_b(client):
    """
    Long prompt-instruction-heavy audit query must not have its substantive tokens diluted
    by framing terms ('according', 'available', 'pilot', 'excerpt', 'cite', 'evidence', 'establish').
    Must surface >= 5 candidates and rank Annexure-B structured format passages in top positions.
    """
    long_query = (
        "According to the SEBI CSCRF circulars available in this pilot, what specific elements "
        "must be included in an audit report? List only requirements supported by retrieved excerpts, "
        "cite the circular identifier and page numbers, and explicitly identify any information the evidence does not establish."
    )
    response = client.post("/api/v1/query", json={"query": long_query, "top_k": 5})
    assert response.status_code == 200
    data = response.json()
    assert data["retrieval_candidate_count"] >= 5
    assert len(data["verified_excerpts"]) >= 5
    top_excerpts = [e["excerpt_text"].lower() for e in data["verified_excerpts"][:3]]
    # Top verified excerpts must feature Annexure-B operative audit report sections
    assert any(
        "annexure-b" in txt or "annexure b" in txt or "terms of reference" in txt or "executive summary" in txt or "observations" in txt
        for txt in top_excerpts
    )


def test_audit_report_concise_query_prioritizes_annexure_b(client):
    """
    Concise query must prioritize operative Annexure-B audit report template chunks over
    pure circular index tables or general auditor-credential paragraphs.
    """
    concise_query = "What specific elements must be included in an audit report under SEBI CSCRF?"
    response = client.post("/api/v1/query", json={"query": concise_query, "top_k": 5})
    assert response.status_code == 200
    data = response.json()
    assert len(data["verified_excerpts"]) >= 5
    # The top excerpt should not be the circular index table (pages 2-4)
    top_chunk_id = data["verified_excerpts"][0]["source_chunk_id"]
    assert "p2_1" not in top_chunk_id
    # Annexure-B operative content must appear in top results
    first_two_texts = [e["excerpt_text"].lower() for e in data["verified_excerpts"][:2]]
    assert any("annexure" in txt or "audit" in txt or "terms of reference" in txt for txt in first_two_texts)


def test_index_table_deprioritized_for_substantive_requirement_queries(client):
    """
    For substantive requirement queries, circular table of contents / index tables must be
    deprioritized behind operative regulatory provisions.
    """
    query = "What are the specific requirements for root cause analysis in audit reports?"
    response = client.post("/api/v1/query", json={"query": query, "top_k": 5})
    assert response.status_code == 200
    data = response.json()
    assert len(data["verified_excerpts"]) > 0
    top_ex = data["verified_excerpts"][0]
    assert "table of contents" not in top_ex["excerpt_text"].lower()
    assert "date of issuance" not in top_ex["excerpt_text"].lower()


def test_index_focused_query_preserves_circular_index_priority(client):
    """
    Queries explicitly asking about circular titles, dates of issuance, or circular index tables
    must preserve high priority for circular index tables.
    """
    query = "What is listed under Table 1: Index of Circulars with Date of Issuance?"
    response = client.post("/api/v1/query", json={"query": query, "top_k": 5})
    assert response.status_code == 200
    data = response.json()
    assert len(data["verified_excerpts"]) > 0
    first_two = [e["excerpt_text"].lower() for e in data["verified_excerpts"][:2]]
    assert any("date" in txt or "circular" in txt or "index" in txt for txt in first_two)


def test_claim_verifier_rejects_unsupported_audit_claims():
    """
    Inspect that claim verification rejects claims asserting specific audit elements when
    the cited chunk text is merely an index table or does not support the specific assertions.
    """
    from app.schemas.synthesis import (
        SynthesisInput,
        SynthesisOutput,
        SynthesisStatus,
        SynthesisEvidenceItem,
        ClaimCitation,
    )
    from app.synthesis.verifier import sanitize_and_validate_synthesis_output

    fake_index_evidence = SynthesisEvidenceItem(
        chunk_id="sebi_cscrf_2025_119_p2_1",
        document_id="sebi_cscrf_2025_119",
        circular_number="SEBI/HO/ITD-1/ITD_APCIS/P/CIR/2025/119",
        title="Table of Contents and Circular Index",
        issue_date="2025-01-10",
        sha256="abc",
        page_start=2,
        page_end=4,
        text="Table 1: Index of Circulars. Date of Issuance. Circular Title Circular Number.",
        regulatory_status=RegulatoryStatus.ACTIVE_CURRENT.value,
        status_reason="Active baseline",
        mapped_provisions=[],
    )

    unsupported_claim = ClaimCitation(
        claim_text="The audit report must include an executive summary and comprehensive root cause analysis.",
        supporting_chunk_ids=["sebi_cscrf_2025_119_p2_1"],
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
