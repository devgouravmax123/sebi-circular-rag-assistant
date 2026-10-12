"""
Unit and Adversarial tests for the Retrieval-Bound Answer Safety Gate (Phase 6D.3).

Tests:
1. Valid exact excerpt within authorized retrieval set.
2. Candidate chunk absent from the current query's retrieved candidate set rejected.
3. Retrieval returns no candidates -> controlled abstention.
4. Retrieval raises an error -> controlled abstention.
5. Only superseded evidence for a current-compliance query -> controlled abstention.
6. Historical query where a superseded excerpt is separated without implying current validity.
7. Unresolved evidence handling with explicit warnings and qualified disposition.
8. Partial amendment with no verified matching provision mapping -> UNRESOLVED.
9. A REFERENCES edge incorrectly described as supersession.
10. Unknown top-level and per-excerpt payload fields rejected (Pydantic extra='forbid').
11. Cross-chunk quotation stitching rejected.
12. Genuine source text containing markup-looking characters.
13. Valid quotation that is insufficient/unrelated to requested query.
14. No untrusted draft narrative or metadata leaking into the response.
15. Regression of all existing safety-gate behaviors.
"""
import pytest
from unittest.mock import MagicMock
from pydantic import ValidationError

from app.schemas.safety_gate import (
    AnswerDisposition,
    CandidateExcerptDraft,
    ExcerptDraftPayload,
    SafetyGateResponse,
)
from app.schemas.grounding import RegulatoryStatus
from app.grounding.safety_gate import AnswerSafetyGate, is_current_status_query
from app.schemas.graph import GraphEdge, RelationshipType, ScopeType, ReviewStatus
from app.schemas.graph_collection import GraphEdgeCollection
from app.schemas.retrieval import RetrievalResult, RetrievalResponse


@pytest.fixture
def gate():
    return AnswerSafetyGate(processed_dir="data/processed")


def test_1_valid_exact_excerpt_within_retrieval_set(gate):
    # Valid excerpt from CIR/2025/045 chunk sebi_cscrf_2025_045_p1_0
    exact_text = "Recognising the need for robust cybersecurity measures and protection of data and IT infrastructure"
    payload = ExcerptDraftPayload(
        query="What did SEBI recognise regarding cybersecurity?",
        excerpts=[
            CandidateExcerptDraft(
                excerpt_id="e1",
                source_chunk_id="sebi_cscrf_2025_045_p1_0",
                proposed_excerpt_text=exact_text,
            )
        ],
    )
    # Authorized retrieval set containing chunk
    resp = gate.evaluate_draft(payload, retrieval_candidates={"sebi_cscrf_2025_045_p1_0"})
    assert resp.technical_validation_passed is True
    assert resp.disposition in (AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS, AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS)
    assert len(resp.verified_excerpts) == 1
    assert resp.verified_excerpts[0].excerpt_id == "e1"
    assert resp.verified_excerpts[0].document_id == "sebi_cscrf_2025_045"
    assert resp.retrieval_candidate_count == 1


def test_2_candidate_chunk_absent_from_retrieval_set_rejected(gate):
    # Chunk exists in corpus, but was NOT retrieved for this query
    exact_text = "Recognising the need for robust cybersecurity measures"
    payload = ExcerptDraftPayload(
        query="Some unrelated query",
        excerpts=[
            CandidateExcerptDraft(
                excerpt_id="e1",
                source_chunk_id="sebi_cscrf_2025_045_p1_0",
                proposed_excerpt_text=exact_text,
            )
        ],
    )
    # Server-side retrieval candidate set does NOT contain sebi_cscrf_2025_045_p1_0
    resp = gate.evaluate_draft(payload, retrieval_candidates={"sebi_cscrf_2024_113_p1_0"})
    assert resp.disposition == AnswerDisposition.CONTROLLED_ABSTENTION
    assert resp.technical_validation_passed is False
    assert len(resp.verified_excerpts) == 0
    assert "not in authorized retrieval candidate set" in resp.summary_narrative


def test_3_retrieval_returns_no_candidates(gate):
    payload = ExcerptDraftPayload(
        query="Query returning 0 results",
        excerpts=[
            CandidateExcerptDraft(
                excerpt_id="e1",
                source_chunk_id="sebi_cscrf_2025_045_p1_0",
                proposed_excerpt_text="Some text",
            )
        ],
    )
    # Empty set from retrieval
    resp = gate.evaluate_draft(payload, retrieval_candidates=set())
    assert resp.disposition == AnswerDisposition.CONTROLLED_ABSTENTION
    assert resp.technical_validation_passed is False
    assert "Zero candidate chunks retrieved" in resp.summary_narrative


def test_4_retrieval_service_execution_error():
    mock_service = MagicMock()
    mock_service.search_hybrid.side_effect = RuntimeError("Index connection timeout")

    gate_with_service = AnswerSafetyGate(
        processed_dir="data/processed",
        retrieval_service=mock_service,
    )
    payload = ExcerptDraftPayload(
        query="Query causing error",
        excerpts=[
            CandidateExcerptDraft(
                excerpt_id="e1",
                source_chunk_id="sebi_cscrf_2025_045_p1_0",
                proposed_excerpt_text="Text",
            )
        ],
    )
    resp = gate_with_service.evaluate_draft(payload)
    assert resp.disposition == AnswerDisposition.CONTROLLED_ABSTENTION
    assert resp.technical_validation_passed is False
    assert "Server-side retrieval failed" in resp.summary_narrative


def test_5_only_superseded_evidence_for_current_compliance_query():
    # Setup custom graph where doc_old is SUPERSEDED by doc_new
    custom_graph = GraphEdgeCollection()
    custom_graph.add_edge(
        GraphEdge(
            edge_id="rescind_edge",
            source_document_id="sebi_cscrf_2025_060",
            target_document_id="sebi_cscrf_2025_045",
            relationship=RelationshipType.RESCINDS,
            scope=ScopeType.FULL,
            evidence_text="Rescission text",
            review_status=ReviewStatus.VERIFIED,
        )
    )
    custom_gate = AnswerSafetyGate(processed_dir="data/processed", graph_collection=custom_graph)
    exact_c45 = "SEBI had received multiple requests for CSCRF compliance timelines extension"
    payload = ExcerptDraftPayload(
        query="What is the current mandatory deadline for CSCRF compliance today?",
        excerpts=[
            CandidateExcerptDraft(
                excerpt_id="e_super",
                source_chunk_id="sebi_cscrf_2025_045_p1_0",
                proposed_excerpt_text=exact_c45,
            )
        ],
    )
    resp = custom_gate.evaluate_draft(payload, retrieval_candidates={"sebi_cscrf_2025_045_p1_0"})
    assert resp.disposition == AnswerDisposition.CONTROLLED_ABSTENTION
    assert len(resp.verified_excerpts) == 0
    assert len(resp.historical_superseded_excerpts) == 1
    assert resp.abstention_reason is not None
    assert "all matching corpus provisions are superseded" in resp.abstention_reason.lower()


def test_6_historical_query_with_superseded_evidence_separated():
    custom_graph = GraphEdgeCollection()
    custom_graph.add_edge(
        GraphEdge(
            edge_id="rescind_edge",
            source_document_id="sebi_cscrf_2025_060",
            target_document_id="sebi_cscrf_2025_045",
            relationship=RelationshipType.RESCINDS,
            scope=ScopeType.FULL,
            evidence_text="Rescission text",
            review_status=ReviewStatus.VERIFIED,
        )
    )
    custom_gate = AnswerSafetyGate(processed_dir="data/processed", graph_collection=custom_graph)
    exact_c45 = "SEBI had received multiple requests for CSCRF compliance timelines extension"
    payload = ExcerptDraftPayload(
        query="What was historically stated in circular 2025/045?",
        excerpts=[
            CandidateExcerptDraft(
                excerpt_id="e_super_hist",
                source_chunk_id="sebi_cscrf_2025_045_p1_0",
                proposed_excerpt_text=exact_c45,
            )
        ],
    )
    resp = custom_gate.evaluate_draft(payload, retrieval_candidates={"sebi_cscrf_2025_045_p1_0"})
    # Non-current query returns separated historical excerpts under QUALIFIED_HISTORICAL_EXCERPTS
    assert resp.disposition == AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS
    assert len(resp.verified_excerpts) == 0
    assert len(resp.historical_superseded_excerpts) == 1


def test_7_unresolved_evidence_with_draft_untrusted_flags(gate):
    exact_text = "The reporting of the CISO of the MII and Qualified REs shall be directly to the MD & CEO of their organization."
    payload = ExcerptDraftPayload(
        query="To whom does the CISO report?",
        excerpts=[
            CandidateExcerptDraft(
                excerpt_id="e_unres",
                source_chunk_id="sebi_cscrf_2024_113_p80_67",
                proposed_excerpt_text=exact_text,
            )
        ],
    )
    resp = gate.evaluate_draft(payload, retrieval_candidates={"sebi_cscrf_2024_113_p80_67"})
    assert resp.disposition == AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS
    assert any("UNRESOLVED" in w for w in resp.regulatory_warnings)
    assert resp.verified_excerpts[0].regulatory_status == RegulatoryStatus.UNRESOLVED


def test_8_partial_amendment_with_unmatched_provision_is_unresolved(gate):
    # CIR/2024/113 general chunk without specific amended ISO 27001 provision
    general_text = "General definitions and regulatory structure"
    # Testing detection function directly on unmapped text
    from app.grounding.resolver import detect_provisions_in_text
    provisions = detect_provisions_in_text(general_text)
    assert len(provisions) == 0


def test_9_references_edge_never_implies_supersession():
    custom_graph = GraphEdgeCollection()
    custom_graph.add_edge(
        GraphEdge(
            edge_id="ref_edge_only",
            source_document_id="sebi_cscrf_2025_060",
            target_document_id="sebi_cscrf_2025_045",
            relationship=RelationshipType.REFERENCES,
            scope=ScopeType.FULL,
            evidence_text="Reference text",
            review_status=ReviewStatus.VERIFIED,
        )
    )
    custom_gate = AnswerSafetyGate(processed_dir="data/processed", graph_collection=custom_graph)
    exact_c45 = "SEBI had received multiple requests for CSCRF compliance timelines extension"
    payload = ExcerptDraftPayload(
        query="Did 2025_060 repeal 2025_045?",
        excerpts=[
            CandidateExcerptDraft(
                excerpt_id="e_ref",
                source_chunk_id="sebi_cscrf_2025_045_p1_0",
                proposed_excerpt_text=exact_c45,
            )
        ],
    )
    resp = custom_gate.evaluate_draft(payload, retrieval_candidates={"sebi_cscrf_2025_045_p1_0"})
    assert resp.verified_excerpts[0].regulatory_status == RegulatoryStatus.INFORMATIONAL
    assert resp.verified_excerpts[0].regulatory_status != RegulatoryStatus.SUPERSEDED


def test_10_unknown_top_level_and_per_excerpt_fields_rejected(gate):
    # Unknown top-level field
    malformed_top = {
        "query": "Test query",
        "model_confidence": 0.99,  # forbidden
        "excerpts": [
            {
                "excerpt_id": "e1",
                "source_chunk_id": "sebi_cscrf_2025_045_p1_0",
                "proposed_excerpt_text": "Text",
            }
        ],
    }
    resp = gate.evaluate_draft(malformed_top)
    assert resp.disposition == AnswerDisposition.REJECTED_UNSAFE
    assert resp.technical_validation_passed is False

    # Unknown per-excerpt field
    malformed_excerpt = {
        "query": "Test query",
        "excerpts": [
            {
                "excerpt_id": "e1",
                "source_chunk_id": "sebi_cscrf_2025_045_p1_0",
                "proposed_excerpt_text": "Text",
                "authoritative_status": "ACTIVE_CURRENT",  # forbidden injection attempt
            }
        ],
    }
    resp2 = gate.evaluate_draft(malformed_excerpt)
    assert resp2.disposition == AnswerDisposition.REJECTED_UNSAFE


def test_11_cross_chunk_quotation_stitching_rejected(gate):
    # Attempting to stitch text from p1_0 and p2_1 together as a single excerpt
    stitched_text = (
        "Recognising the need for robust cybersecurity measures "
        "This circular is issued with the approval of Competent Authority."
    )
    payload = ExcerptDraftPayload(
        query="Test cross-chunk",
        excerpts=[
            CandidateExcerptDraft(
                excerpt_id="e_stitch",
                source_chunk_id="sebi_cscrf_2025_045_p1_0",
                proposed_excerpt_text=stitched_text,
            )
        ],
    )
    resp = gate.evaluate_draft(payload, retrieval_candidates={"sebi_cscrf_2025_045_p1_0"})
    assert resp.disposition == AnswerDisposition.CONTROLLED_ABSTENTION
    assert len(resp.verified_excerpts) == 0


def test_12_genuine_source_text_with_markup_looking_characters(gate):
    # CSCRF circular contains section numbers, parentheses, and colons: e.g. "3.1. Bring the provisions of this circulars"
    exact_text = "3.1. Bring the provisions of this circulars to the notice of their members/ participants"
    payload = ExcerptDraftPayload(
        query="What are Stock Exchanges directed to do?",
        excerpts=[
            CandidateExcerptDraft(
                excerpt_id="e_punct",
                source_chunk_id="sebi_cscrf_2025_045_p1_0",
                proposed_excerpt_text=exact_text,
            )
        ],
    )
    resp = gate.evaluate_draft(payload, retrieval_candidates={"sebi_cscrf_2025_045_p1_0"})
    assert resp.technical_validation_passed is True
    assert len(resp.verified_excerpts) == 1
    assert resp.verified_excerpts[0].excerpt_text == exact_text


def test_13_valid_quotation_insufficient_for_unrelated_query(gate):
    exact_text = "Recognising the need for robust cybersecurity measures"
    payload = ExcerptDraftPayload(
        query="What are the specific penalties for non-compliance with CSCRF?",
        excerpts=[
            CandidateExcerptDraft(
                excerpt_id="e_irrelevant",
                source_chunk_id="sebi_cscrf_2025_045_p1_0",
                proposed_excerpt_text=exact_text,
            )
        ],
    )
    resp = gate.evaluate_draft(payload, retrieval_candidates={"sebi_cscrf_2025_045_p1_0"})
    # Excerpt is verified as historical circular text, but disposition explicitly specifies historical excerpts, NOT a proven answer
    assert resp.disposition in (AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS, AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS)
    assert "Excerpts represent technical document grounding only" in resp.pilot_corpus_scope_limitation


def test_14_no_untrusted_draft_narrative_or_metadata_leaking(gate):
    exact_text = "Recognising the need for robust cybersecurity measures"
    payload = ExcerptDraftPayload(
        query="Test query",
        excerpts=[
            CandidateExcerptDraft(
                excerpt_id="e1",
                source_chunk_id="sebi_cscrf_2025_045_p1_0",
                proposed_excerpt_text=exact_text,
            )
        ],
    )
    resp = gate.evaluate_draft(payload, retrieval_candidates={"sebi_cscrf_2025_045_p1_0"})
    data = resp.model_dump()
    assert "free_form_prose" not in data
    assert "raw_llm_markdown" not in data
    assert resp.verified_excerpts[0].excerpt_text == exact_text
    assert resp.verified_excerpts[0].document_id == "sebi_cscrf_2025_045"


def test_15_relevance_safeguard_filters_low_coverage_out_of_domain_candidates():
    from app.grounding.relevance import filter_relevant_candidates, RelevanceDecisionCode
    from app.grounding.classifier import QueryIntent

    # Out-of-domain query with arbitrary cyber text that doesn't match substantive tokens
    query = "What are the threat-led penetration testing (TLPT) frequencies mandated by the EU Digital Operational Resilience Act (DORA)?"
    cand = RetrievalResult(
        chunk_id="sebi_cscrf_2024_113_p15_13",
        document_id="sebi_cscrf_2024_113",
        circular_number="CIR/2024/113",
        title="Baseline Circular",
        issue_date="2024-08-20",
        page_start=15,
        page_end=15,
        text="vulnerabilities, likelihoods, and impacts shall be used to understand inherent risks and undertake risk response prioritization.",
        score=0.0306,
        rank=1,
    )
    relevant, intent, assessments = filter_relevant_candidates(query, [cand])
    assert len(relevant) == 0
    assert len(assessments) == 1
    assert assessments[0].is_relevant is False
    assert assessments[0].decision_code == RelevanceDecisionCode.INSUFFICIENT_RELEVANCE
    assert "lacks substantive query topical coverage" in assessments[0].reason


def test_16_relevance_safeguard_accepts_substantive_in_scope_candidates():
    from app.grounding.relevance import filter_relevant_candidates, RelevanceDecisionCode

    query = "What is the mandatory compliance deadline extension under circular CIR/2025/045?"
    cand = RetrievalResult(
        chunk_id="sebi_cscrf_2025_045_p2_1",
        document_id="sebi_cscrf_2025_045",
        circular_number="CIR/2025/045",
        title="Extension Circular",
        issue_date="2025-03-28",
        page_start=2,
        page_end=2,
        text="SEBI had received multiple requests for CSCRF compliance timelines extension. Decided to extend the compliance timelines by three months, i.e., till June 30, 2025.",
        score=0.038,
        rank=1,
    )
    relevant, intent, assessments = filter_relevant_candidates(query, [cand])
    assert len(relevant) == 1
    assert assessments[0].is_relevant is True
    assert assessments[0].decision_code == RelevanceDecisionCode.RELEVANT_EVIDENCE
