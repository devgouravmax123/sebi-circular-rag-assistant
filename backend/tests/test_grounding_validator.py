"""
Unit tests for Citation and Evidence Validator (Phase 6C).

Tests:
1. Valid chunk and citation.
2. Fabricated chunk ID.
3. Wrong document ID.
4. Invalid page range.
5. Fabricated direct quotation.
6. Missing metadata.
7. Historical evidence with UNRESOLVED status.
8. Claim asserting current legal status without sufficient evidence.
9. Conflicting regulatory evidence.
10. REFERENCES edge incorrectly presented as supersession.
11. Empty and malformed citation inputs.
12. Deterministic validation output.
"""
import pytest
from pathlib import Path
from app.schemas.citation import (
    CitationItem,
    CitationStatus,
    ClaimItem,
    ClaimSupportStatus,
)
from app.schemas.grounding import (
    GroundedEvidenceItem,
    RegulatoryStatus,
)
from app.schemas.graph import GraphEdge, RelationshipType, ScopeType, ReviewStatus
from app.schemas.graph_collection import GraphEdgeCollection
from app.grounding.validator import CitationValidator


@pytest.fixture
def validator():
    # Uses real registered corpus in data/processed
    return CitationValidator(processed_dir="data/processed")


def test_1_valid_chunk_and_citation(validator):
    # Test with real chunk from 2025_045: sebi_cscrf_2025_045_p1_0 (pages 1 to 2)
    cit = CitationItem(
        chunk_id="sebi_cscrf_2025_045_p1_0",
        document_id="sebi_cscrf_2025_045",
        circular_number="SEBI/HO/ITD-1/ITD_CSC_EXT/P/CIR/2025/45",
        issue_date="2025-03-28",
        page=1,
        quote="Recognising the need for robust cybersecurity measures",
    )
    res = validator.validate_citation(cit)
    assert res.is_valid is True
    assert res.status == CitationStatus.VALID
    assert len(res.failure_reasons) == 0
    assert res.authoritative_document_id == "sebi_cscrf_2025_045"
    assert res.authoritative_page_start == 1
    assert res.authoritative_page_end == 2


def test_2_fabricated_chunk_id(validator):
    cit = CitationItem(
        chunk_id="non_existent_fake_chunk_999",
        document_id="sebi_cscrf_2024_113",
    )
    res = validator.validate_citation(cit)
    assert res.is_valid is False
    assert res.status == CitationStatus.INVALID_CHUNK_NOT_FOUND
    assert any("does not exist" in r for r in res.failure_reasons)


def test_3_wrong_document_id(validator):
    # Real chunk belongs to 2025_045, but citation specifies 2024_113
    cit = CitationItem(
        chunk_id="sebi_cscrf_2025_045_p1_0",
        document_id="sebi_cscrf_2024_113",
    )
    res = validator.validate_citation(cit)
    assert res.is_valid is False
    assert res.status == CitationStatus.INVALID_DOC_ID_MISMATCH
    assert any("Document ID mismatch" in r for r in res.failure_reasons)


def test_4_invalid_page_range(validator):
    # Chunk is on pages 1-2, citation claims page 15 or page_start 10
    cit = CitationItem(
        chunk_id="sebi_cscrf_2025_045_p1_0",
        page=15,
    )
    res = validator.validate_citation(cit)
    assert res.is_valid is False
    assert res.status == CitationStatus.INVALID_PAGE_RANGE
    assert any("falls outside chunk page range" in r for r in res.failure_reasons)

    # Inverted page range
    cit_inverted = CitationItem(
        chunk_id="sebi_cscrf_2025_045_p1_0",
        page_start=2,
        page_end=1,
    )
    res_inv = validator.validate_citation(cit_inverted)
    assert res_inv.is_valid is False
    assert any("Inverted page range" in r for r in res_inv.failure_reasons)


def test_5_fabricated_direct_quotation(validator):
    cit = CitationItem(
        chunk_id="sebi_cscrf_2025_045_p1_0",
        quote="SEBI hereby completely abolishes all cyber audit requirements for stock brokers.",
    )
    res = validator.validate_citation(cit)
    assert res.is_valid is False
    assert res.status == CitationStatus.INVALID_QUOTATION
    assert any("not a verbatim substring" in r for r in res.failure_reasons)


def test_6_missing_metadata(validator):
    # Minimal citation with only chunk_id should succeed and populate authoritative metadata
    cit = CitationItem(chunk_id="sebi_cscrf_2025_045_p1_0")
    res = validator.validate_citation(cit)
    assert res.is_valid is True
    assert res.status == CitationStatus.VALID
    assert res.authoritative_document_id == "sebi_cscrf_2025_045"
    assert res.authoritative_circular_number == "SEBI/HO/ITD-1/ITD_CSC_EXT/P/CIR/2025/45"
    assert res.authoritative_issue_date == "2025-03-28"


def test_7_historical_evidence_with_unresolved_status(validator):
    # A claim asserting current status on an UNRESOLVED grounded item
    g_item = GroundedEvidenceItem(
        chunk_id="sebi_cscrf_2025_045_p1_0",
        document_id="sebi_cscrf_2025_045",
        page_start=1,
        page_end=2,
        text="Recognising the need for robust cybersecurity measures",
        retrieval_score=1.0,
        retrieval_rank=1,
        regulatory_status=RegulatoryStatus.UNRESOLVED,
        status_reason="Unresolved pilot status",
    )
    claim = ClaimItem(
        claim_id="cl_1",
        claim_text="This requirement is currently in full force and active law.",
        cited_chunk_ids=["sebi_cscrf_2025_045_p1_0"],
        asserts_current_status=True,
    )
    res = validator.validate_claim(claim, grounded_items=[g_item])
    assert res.support_status == ClaimSupportStatus.FLAGGED_UNRESOLVED
    assert res.is_supported is False
    assert res.citations_mechanically_valid is True
    assert any("UNRESOLVED" in f for f in res.flags)


def test_8_claim_asserting_current_status_without_sufficient_evidence(validator):
    # Claim without assert_current_status but with valid citation needing semantic review
    claim = ClaimItem(
        claim_id="cl_2",
        claim_text="SEBI issued CSCRF guidelines for Regulated Entities.",
        cited_chunk_ids=["sebi_cscrf_2025_045_p1_0"],
        asserts_current_status=False,
    )
    res = validator.validate_claim(claim)
    assert res.citations_mechanically_valid is True
    # Non-verbatim substring entails semantic review
    assert res.support_status == ClaimSupportStatus.NEEDS_SEMANTIC_REVIEW
    assert res.is_supported is False


def test_9_conflicting_regulatory_evidence(validator):
    # Claim asserting current compliance for a superseded document
    g_item = GroundedEvidenceItem(
        chunk_id="sebi_cscrf_2025_045_p1_0",
        document_id="sebi_cscrf_2025_045",
        page_start=1,
        page_end=2,
        text="Old superseded compliance text",
        retrieval_score=1.0,
        retrieval_rank=1,
        regulatory_status=RegulatoryStatus.SUPERSEDED,
        status_reason="Superseded by newer circular",
    )
    claim = ClaimItem(
        claim_id="cl_superseded",
        claim_text="REs must follow this superseded clause today.",
        cited_chunk_ids=["sebi_cscrf_2025_045_p1_0"],
        asserts_current_status=True,
    )
    res = validator.validate_claim(claim, grounded_items=[g_item])
    assert res.support_status == ClaimSupportStatus.CONTRADICTORY
    assert res.is_supported is False
    assert any("superseded" in res.status_reason.lower() for _ in [1])


def test_10_references_edge_incorrectly_presented_as_supersession():
    # Setup custom graph where doc_a REFERENCES doc_b
    graph = GraphEdgeCollection()
    graph.add_edge(
        GraphEdge(
            edge_id="ref_edge_test",
            source_document_id="sebi_cscrf_2025_060",
            target_document_id="sebi_cscrf_2025_045",
            relationship=RelationshipType.REFERENCES,
            scope=ScopeType.FULL,
            evidence_text="Reference text",
            review_status=ReviewStatus.VERIFIED,
        )
    )
    val = CitationValidator(processed_dir="data/processed", graph_collection=graph)
    claim = ClaimItem(
        claim_id="cl_ref_as_amend",
        claim_text="Circular 2025/060 supersedes and repeals Circular 2025/045 in its entirety.",
        cited_chunk_ids=["sebi_cscrf_2025_045_p1_0"],
        asserts_supersession=True,
    )
    res = val.validate_claim(claim)
    assert res.support_status == ClaimSupportStatus.CONTRADICTORY
    assert res.is_supported is False
    assert any("REFERENCES" in f for f in res.flags)


def test_11_empty_and_malformed_citation_inputs(validator):
    # Empty citation list
    claim_empty = ClaimItem(claim_id="cl_empty", claim_text="Some random statement", cited_chunk_ids=[])
    res_empty = validator.validate_claim(claim_empty)
    assert res_empty.support_status == ClaimSupportStatus.UNSUPPORTED
    assert res_empty.is_supported is False

    # Malformed citation dict passed to validate_citation
    res_malformed = validator.validate_citation({"non_existent_key": 123})
    assert res_malformed.is_valid is False
    assert res_malformed.status in (CitationStatus.INVALID_MALFORMED, CitationStatus.INVALID_CHUNK_NOT_FOUND)

    # Empty package
    pkg_rep = validator.validate_package(citations=[], claims=[])
    assert pkg_rep.all_citations_valid is True
    assert pkg_rep.may_proceed_to_synthesis is True
    assert pkg_rep.total_citations == 0


def test_12_deterministic_validation_output(validator):
    # Verbatim quote test
    verbatim_text = "Recognising the need for robust cybersecurity measures"
    claim = ClaimItem(
        claim_id="cl_quote",
        claim_text=verbatim_text,
        cited_chunk_ids=["sebi_cscrf_2025_045_p1_0"],
        asserts_current_status=False,
    )
    res = validator.validate_claim(claim)
    assert res.support_status == ClaimSupportStatus.SUPPORTED_DETERMINISTIC
    assert res.is_supported is True
    assert res.citations_mechanically_valid is True
    assert "exact or verbatim quotation" in res.status_reason
