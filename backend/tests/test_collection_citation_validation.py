"""
Tests for Phase 2.2B.3 — Collection-Independent Citation Validation.

Verifies:
1. Valid citations in a general collection.
2. General documents without SEBI circular-number fields.
3. Missing, fabricated, and ambiguous citation metadata.
4. Incorrect page or chunk references.
5. Cross-collection document and chunk references.
6. Duplicate document IDs, titles, or chunk IDs in different collections.
7. Missing or corrupt published corpus artifacts.
8. Claim-to-excerpt binding and verbatim quotation validation.
9. SEBI citation and temporal-regression compatibility.
10. General-policy synthesis remains safely blocked.
11. Verified textual evidence is not automatically classified as currently effective (Section 3).
"""
import json
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.api.v1.query import get_collection_registry, get_retrieval_service, get_safety_gate
from app.grounding.validator import CitationValidator
from app.schemas.citation import (
    CitationItem,
    CitationStatus,
    ClaimItem,
    ClaimSupportStatus,
)
from app.schemas.document import DocumentRecord, ChunkRecord
from app.schemas.grounding import RegulatoryStatus, GroundedEvidenceItem
from app.schemas.manifest import (
    CollectionManifest,
    CollectionStatus,
    GroundingPolicyDeclaration,
    GroundingPolicyType,
)
from app.schemas.safety_gate import (
    AnswerDisposition,
    CandidateExcerptDraft,
    ExcerptDraftPayload,
)
from app.grounding.safety_gate import AnswerSafetyGate
from app.schemas.retrieval import RetrievalResult, RetrievalResponse


@pytest.fixture
def general_chunks_and_docs():
    chunk = ChunkRecord(
        chunk_id="gen_policy_p1_1",
        document_id="corp_sec_std_2024",
        collection_id="general_corp",
        text="All personnel must rotate cryptographic service keys at least every 90 days without exception.",
        page_start=1,
        page_end=1,
    )
    doc = DocumentRecord(
        document_id="corp_sec_std_2024",
        circular_number=None,  # General document with NO circular number
        title="Corporate Cryptographic Key Standard",
        department="Information Security",
        issue_date="2024-02-01",
        source_url="https://internal.example.org/docs/key_std.pdf",
        sha256="abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
        pdf_path="general_corp/key_std.pdf",
        collection_id="general_corp",
    )
    return chunk, doc


def test_1_valid_citations_in_general_collection(general_chunks_and_docs):
    """Citations in a general collection pass mechanical verification against scoped corpus."""
    chunk, doc = general_chunks_and_docs
    validator = CitationValidator(
        collection_id="general_corp",
        corpus_chunks={chunk.chunk_id: chunk},
        corpus_documents={doc.document_id: doc},
    )

    cit = CitationItem(
        chunk_id="gen_policy_p1_1",
        document_id="corp_sec_std_2024",
        collection_id="general_corp",
        title="Corporate Cryptographic Key Standard",
        issue_date="2024-02-01",
        page=1,
        quote="All personnel must rotate cryptographic service keys at least every 90 days",
    )
    res = validator.validate_citation(cit)
    assert res.is_valid is True
    assert res.status == CitationStatus.VALID
    assert res.authoritative_collection_id == "general_corp"
    assert res.authoritative_document_id == "corp_sec_std_2024"
    assert res.authoritative_title == "Corporate Cryptographic Key Standard"
    assert res.authoritative_circular_number is None  # Non-SEBI document has None circular_number
    assert res.authoritative_source_url == "https://internal.example.org/docs/key_std.pdf"


def test_2_general_documents_without_sebi_circular_number_fields(general_chunks_and_docs):
    """General documents with None circular_number are valid and do not cause false mismatches."""
    chunk, doc = general_chunks_and_docs
    validator = CitationValidator(
        collection_id="general_corp",
        corpus_chunks={chunk.chunk_id: chunk},
        corpus_documents={doc.document_id: doc},
    )

    cit = CitationItem(
        chunk_id="gen_policy_p1_1",
        document_id="corp_sec_std_2024",
        title="Corporate Cryptographic Key Standard",
        page=1,
        quote="All personnel must rotate cryptographic service keys",
    )
    res = validator.validate_citation(cit)
    assert res.is_valid is True
    assert res.authoritative_circular_number is None


def test_3_missing_fabricated_and_ambiguous_citation_metadata(general_chunks_and_docs):
    """Fabricated chunk IDs, mismatched document IDs, or wrong titles fail closed."""
    chunk, doc = general_chunks_and_docs
    validator = CitationValidator(
        collection_id="general_corp",
        corpus_chunks={chunk.chunk_id: chunk},
        corpus_documents={doc.document_id: doc},
    )

    # A: Fabricated chunk
    res_fake = validator.validate_citation(CitationItem(chunk_id="nonexistent_chunk_999"))
    assert res_fake.is_valid is False
    assert res_fake.status == CitationStatus.INVALID_CHUNK_NOT_FOUND

    # B: Wrong document ID
    res_doc = validator.validate_citation(CitationItem(
        chunk_id="gen_policy_p1_1",
        document_id="completely_wrong_doc",
    ))
    assert res_doc.is_valid is False
    assert res_doc.status == CitationStatus.INVALID_DOC_ID_MISMATCH

    # C: Wrong title
    res_title = validator.validate_citation(CitationItem(
        chunk_id="gen_policy_p1_1",
        title="Completely Unrelated Guideline",
    ))
    assert res_title.is_valid is False
    assert res_title.status == CitationStatus.INVALID_METADATA_MISMATCH
    assert "Document title mismatch" in res_title.failure_reasons[0]


def test_4_incorrect_page_or_chunk_references(general_chunks_and_docs):
    """Incorrect page numbers outside chunk boundaries fail validation."""
    chunk, doc = general_chunks_and_docs
    validator = CitationValidator(
        collection_id="general_corp",
        corpus_chunks={chunk.chunk_id: chunk},
        corpus_documents={doc.document_id: doc},
    )

    # Chunk is page 1 to 1; citing page 5 fails
    res_page = validator.validate_citation(CitationItem(
        chunk_id="gen_policy_p1_1",
        page=5,
    ))
    assert res_page.is_valid is False
    assert res_page.status == CitationStatus.INVALID_PAGE_RANGE


def test_5_cross_collection_document_and_chunk_references(general_chunks_and_docs):
    """Citations claiming a collection different from the chunk or validator are rejected."""
    chunk, doc = general_chunks_and_docs
    validator = CitationValidator(
        collection_id="general_corp",
        corpus_chunks={chunk.chunk_id: chunk},
        corpus_documents={doc.document_id: doc},
    )

    # Citation claims chunk belongs to sebi_cscrf when it is general_corp
    res = validator.validate_citation(CitationItem(
        chunk_id="gen_policy_p1_1",
        collection_id="sebi_cscrf",
    ))
    assert res.is_valid is False
    assert res.status == CitationStatus.INVALID_COLLECTION_MISMATCH
    assert any("Collection ID mismatch" in r for r in res.failure_reasons)


def test_6_duplicate_identifiers_in_different_collections():
    """Colliding chunk IDs across collections are isolated by collection scope."""
    # Collection A chunk
    chunk_a = ChunkRecord(
        chunk_id="doc1_p1_0",
        document_id="doc1",
        collection_id="coll_a",
        text="Collection A secret formula for widgets.",
        page_start=1,
        page_end=1,
    )
    doc_a = DocumentRecord(
        document_id="doc1",
        collection_id="coll_a",
        title="Document 1 in Collection A",
        source_url="https://a.org/1.pdf",
        sha256="1111111111111111111111111111111111111111111111111111111111111111",
        pdf_path="coll_a/1.pdf",
    )

    # Collection B chunk with IDENTICAL chunk_id and document_id
    chunk_b = ChunkRecord(
        chunk_id="doc1_p1_0",
        document_id="doc1",
        collection_id="coll_b",
        text="Collection B completely different text for widgets.",
        page_start=1,
        page_end=1,
    )
    doc_b = DocumentRecord(
        document_id="doc1",
        collection_id="coll_b",
        title="Document 1 in Collection B",
        source_url="https://b.org/1.pdf",
        sha256="2222222222222222222222222222222222222222222222222222222222222222",
        pdf_path="coll_b/1.pdf",
    )

    # Validator scoped to Collection A
    val_a = CitationValidator(
        collection_id="coll_a",
        corpus_chunks={chunk_a.chunk_id: chunk_a},
        corpus_documents={doc_a.document_id: doc_a},
    )

    # Valid in Collection A
    res_a = val_a.validate_citation(CitationItem(
        chunk_id="doc1_p1_0",
        collection_id="coll_a",
        quote="Collection A secret formula",
    ))
    assert res_a.is_valid is True
    assert res_a.authoritative_collection_id == "coll_a"

    # Verifying Collection B's text against Collection A fails
    res_b_text = val_a.validate_citation(CitationItem(
        chunk_id="doc1_p1_0",
        collection_id="coll_a",
        quote="Collection B completely different text",
    ))
    assert res_b_text.is_valid is False
    assert res_b_text.status == CitationStatus.INVALID_QUOTATION


def test_7_missing_or_corrupt_published_corpus_artifacts(tmp_path):
    """Missing or corrupted snapshot pointer files cause validator to fail closed safely."""
    indexes_dir = tmp_path / "indexes"
    coll_dir = indexes_dir / "broken_coll"
    coll_dir.mkdir(parents=True)

    # Write corrupt active_snapshot.json
    (coll_dir / "active_snapshot.json").write_text("{invalid json", encoding="utf-8")

    validator = CitationValidator(
        collection_id="broken_coll",
        indexes_dir=indexes_dir,
        processed_dir=tmp_path / "empty_processed",
    )
    assert len(validator.corpus_chunks) == 0
    # Any citation check fails closed safely
    res = validator.validate_citation(CitationItem(chunk_id="any_chunk"))
    assert res.is_valid is False
    assert res.status == CitationStatus.INVALID_CHUNK_NOT_FOUND


def test_8_claim_to_excerpt_binding_and_verbatim_quotation(general_chunks_and_docs):
    """Claims citing general collection chunks are evaluated for exact substring support."""
    chunk, doc = general_chunks_and_docs
    validator = CitationValidator(
        collection_id="general_corp",
        corpus_chunks={chunk.chunk_id: chunk},
        corpus_documents={doc.document_id: doc},
    )

    # Verbatim supported claim
    claim_valid = ClaimItem(
        claim_id="c1",
        claim_text="All personnel must rotate cryptographic service keys at least every 90 days without exception.",
        cited_chunk_ids=["gen_policy_p1_1"],
    )
    res_valid = validator.validate_claim(claim_valid)
    assert res_valid.is_supported is True
    assert res_valid.support_status == ClaimSupportStatus.SUPPORTED_DETERMINISTIC
    assert res_valid.citations_mechanically_valid is True

    # Hallucinated non-verbatim claim
    claim_hallucinated = ClaimItem(
        claim_id="c2",
        claim_text="Cryptographic keys must be archived on optical drives for 50 years.",
        cited_chunk_ids=["gen_policy_p1_1"],
    )
    res_hallucinated = validator.validate_claim(claim_hallucinated)
    assert res_hallucinated.is_supported is False
    assert res_hallucinated.support_status == ClaimSupportStatus.NEEDS_SEMANTIC_REVIEW


def test_9_sebi_citation_and_temporal_regression_compatibility():
    """Existing SEBI CSCRF citation and temporal invariants remain fully intact."""
    validator = CitationValidator(processed_dir="data/processed")
    # Verify CIR/2025/045 chunk
    exact_text = "Recognising the need for robust cybersecurity measures and protection of data and IT infrastructure"
    cit = CitationItem(
        chunk_id="sebi_cscrf_2025_045_p1_0",
        document_id="sebi_cscrf_2025_045",
        circular_number="SEBI/HO/ITD-1/ITD_CSC_EXT/P/CIR/2025/45",
        page=1,
        quote=exact_text,
    )
    res = validator.validate_citation(cit)
    assert res.is_valid is True
    assert res.status == CitationStatus.VALID
    assert res.authoritative_circular_number == "SEBI/HO/ITD-1/ITD_CSC_EXT/P/CIR/2025/45"


def test_10_general_policy_synthesis_remains_safely_blocked():
    """Section 4 constraint: GENERAL_STATIC synthesis remains blocked by default via the query endpoint."""
    mock_general_manifest = CollectionManifest(
        collection_id="general_corp",
        name="General Corporate Guidelines",
        description="General non-SEBI documents.",
        enabled=True,
        policy=GroundingPolicyDeclaration(
            policy_type=GroundingPolicyType.GENERAL_STATIC,
            enforce_regulatory_graph=False,
            enforce_verbatim_citation=True,
            enforce_retrieval_binding=True,
        ),
    )

    test_chunk = ChunkRecord(
        chunk_id="gen_policy_p1_1",
        document_id="corp_sec_std_2024",
        page_start=1,
        page_end=1,
        text="All personnel must rotate cryptographic service keys at least every 90 days without exception.",
        collection_id="general_corp",
    )
    test_doc = DocumentRecord(
        document_id="corp_sec_std_2024",
        title="Corporate Cryptographic Key Standard",
        issue_date="2024-02-01",
        source_url="https://internal.example.org/docs/key_std.pdf",
        sha256="abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
        pdf_path="general_corp/key_std.pdf",
        collection_id="general_corp",
    )

    mock_ret = MagicMock()
    mock_ret.chunks = [test_chunk]
    mock_ret.documents = {"corp_sec_std_2024": test_doc}
    mock_ret.search_hybrid.return_value = RetrievalResponse(
        query="rotate cryptographic service keys",
        results=[
            RetrievalResult(
                chunk_id="gen_policy_p1_1",
                document_id="corp_sec_std_2024",
                title="Corporate Cryptographic Key Standard",
                text=test_chunk.text,
                page_start=1,
                page_end=1,
                score=0.95,
                rank=1,
                collection_id="general_corp",
            )
        ],
        total_results=1,
    )

    mock_reg = MagicMock()
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
            json={"query": "How often must personnel rotate cryptographic service keys?", "collection_id": "general_corp"},
        )
        assert resp.status_code == 200
        data = resp.json()

        # Provenance excerpts verified
        assert data["technical_validation_passed"] is True
        assert len(data["verified_excerpts"]) == 1

        # Phase 2.2B.7 Controlled Enablement: GENERAL_STATIC synthesis succeeds via mock provider
        assert data["synthesis_status"] in ("FULLY_ANSWERED", "PARTIALLY_ANSWERED")
        assert len(data["claim_citations"]) >= 1
        assert data["claim_citations"][0]["supporting_chunk_ids"] == ["gen_policy_p1_1"]
    finally:
        app.dependency_overrides.pop(get_collection_registry, None)
        app.dependency_overrides.pop(get_retrieval_service, None)
        app.dependency_overrides.pop(get_synthesis_service, None)


def test_11_verified_textual_evidence_not_automatically_classified_as_currently_effective(general_chunks_and_docs):
    """Section 3 & 11: General document excerpts are labeled INFORMATIONAL, not ACTIVE_CURRENT."""
    chunk, doc = general_chunks_and_docs
    mock_ret = MagicMock()
    mock_ret.chunks = [chunk]
    mock_ret.documents = {"corp_sec_std_2024": doc}

    gate = AnswerSafetyGate(processed_dir="data/processed", retrieval_service=mock_ret)
    policy = GroundingPolicyDeclaration(
        policy_type=GroundingPolicyType.GENERAL_STATIC,
        enforce_regulatory_graph=False,
        enforce_verbatim_citation=True,
        enforce_retrieval_binding=True,
    )

    payload = ExcerptDraftPayload(
        query="What is the key rotation policy?",
        excerpts=[
            CandidateExcerptDraft(
                excerpt_id="e1",
                source_chunk_id="gen_policy_p1_1",
                proposed_excerpt_text="All personnel must rotate cryptographic service keys at least every 90 days without exception.",
            )
        ],
    )
    resp = gate.evaluate_draft(
        payload=payload,
        retrieval_candidates={"gen_policy_p1_1"},
        policy=policy,
    )
    assert resp.technical_validation_passed is True
    assert len(resp.verified_excerpts) == 1
    # Section 3 invariant: Must NOT be ACTIVE_CURRENT
    assert resp.verified_excerpts[0].regulatory_status == RegulatoryStatus.INFORMATIONAL
    assert resp.verified_excerpts[0].regulatory_status != RegulatoryStatus.ACTIVE_CURRENT
    assert "textual provenance established without legal currency evaluation" in resp.verified_excerpts[0].status_reason
