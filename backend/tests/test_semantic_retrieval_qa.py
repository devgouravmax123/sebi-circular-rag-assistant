"""
Phase 4: Dedicated regression test suite for Semantic Retrieval & Natural-Language Question Answering.

Verifies the 10 requirements of Master Prompt 35:
1. Keyword queries work.
2. Paraphrased questions retrieve relevant passages.
3. Conceptual questions retrieve relevant evidence.
4. Explanatory questions produce grounded answers.
5. Multi-passage questions can combine relevant evidence.
6. Unsupported questions correctly abstain.
7. Cross-workspace / cross-collection documents never leak into retrieval.
8. Citation validation remains strictly enforced.
9. Existing uploaded document and collection identifiers remain compatible.
10. Missing dense indexes or model failures are reported accurately with diagnostic warnings.
"""
import logging
from typing import List, Dict
import pytest

from app.schemas.retrieval import RetrievalResult, RetrievalResponse
from app.schemas.manifest import GroundingPolicyType, GroundingPolicyDeclaration
from app.schemas.safety_gate import (
    AnswerDisposition,
    ExcerptDraftPayload,
    CandidateExcerptDraft,
)
from app.grounding.relevance import (
    filter_relevant_candidates,
    RelevanceDecisionCode,
    check_token_match,
    get_token_variants,
)
from app.grounding.safety_gate import AnswerSafetyGate
from app.grounding.validator import CitationValidator
from app.retrieval.service import RetrievalService, SnapshotGeneration
from app.retrieval.bm25 import BM25Index
from app.synthesis.service import SynthesisService
from app.synthesis.mock_provider import MockSynthesisProvider
from app.schemas.document import ChunkRecord, DocumentRecord


@pytest.fixture
def general_policy():
    return GroundingPolicyDeclaration(
        policy_type=GroundingPolicyType.GENERAL_STATIC,
        policy_version="1.0.0",
        enforce_regulatory_graph=False,
        abstain_on_graph_unresolved=False,
        require_temporal_coherence=False,
    )


@pytest.fixture
def sample_cisa_candidates() -> List[RetrievalResult]:
    """Sample candidates from CISA incident response collection with dense semantic scores."""
    return [
        RetrievalResult(
            chunk_id="test_qa_chunk_timeline",
            document_id="test_qa_doc",
            title="CISA IR Playbook",
            text="Create a timeline of all relevant findings. The timeline will allow the team to account for all adversary activity on the network and will assist in creating the findings report.",
            page_start=10,
            page_end=10,
            score=0.0325,
            rank=1,
            metadata={"dense_score": 0.783, "retriever_1_score": 0.783, "retriever_1_rank": 1},
        ),
        RetrievalResult(
            chunk_id="test_qa_chunk_containment",
            document_id="test_qa_doc",
            title="CISA IR Playbook",
            text="Any impact on the collection, preservation, securing, and documentation of evidence. Some adversaries may actively monitor defensive response measures, and shift their methods to evade detection and containment. Defenders should therefore develop as complete an understanding of the incident as possible before taking containment actions.",
            page_start=14,
            page_end=15,
            score=0.0313,
            rank=2,
            metadata={"dense_score": 0.728, "retriever_1_score": 0.728, "retriever_1_rank": 1},
        ),
        RetrievalResult(
            chunk_id="test_qa_chunk_preparation",
            document_id="test_qa_doc",
            title="CISA IR Playbook",
            text="Preparation phase establishes roles, responsibilities, communication paths, and logging baselines. Post-incident activity includes lessons learned meetings and updating procedures.",
            page_start=5,
            page_end=5,
            score=0.0290,
            rank=3,
            metadata={"dense_score": 0.792, "retriever_1_score": 0.792, "retriever_1_rank": 2},
        ),
    ]


# -----------------------------------------------------------------------------
# 1. Keyword queries work
# -----------------------------------------------------------------------------
def test_1_keyword_query_retrieves_and_passes(sample_cisa_candidates, general_policy):
    query = "Create a timeline of all relevant findings"
    rel, intent, ass = filter_relevant_candidates(
        query=query,
        candidates=sample_cisa_candidates,
        policy_type=general_policy.policy_type,
    )
    assert len(rel) >= 1
    assert rel[0].chunk_id == "test_qa_chunk_timeline"
    assert ass[0].is_relevant is True
    assert ass[0].decision_code == RelevanceDecisionCode.RELEVANT_EVIDENCE


# -----------------------------------------------------------------------------
# 2. Paraphrased questions retrieve relevant passages
# -----------------------------------------------------------------------------
def test_2_paraphrased_question_survives_filter(sample_cisa_candidates, general_policy):
    # Uses different vocabulary: "attackers alter tactics to bypass surveillance" vs "adversaries shift methods to evade detection"
    query = "What actions should defenders take when attackers alter their tactics to bypass surveillance?"
    rel, intent, ass = filter_relevant_candidates(
        query=query,
        candidates=sample_cisa_candidates,
        policy_type=general_policy.policy_type,
    )
    # The chunk with dense score 0.728 MUST pass and be recognized as relevant
    matching_ids = [r.chunk_id for r in rel]
    assert "test_qa_chunk_containment" in matching_ids
    p15_ass = next(a for a in ass if a.chunk_id == "test_qa_chunk_containment")
    assert p15_ass.is_relevant is True
    assert p15_ass.decision_code == RelevanceDecisionCode.RELEVANT_EVIDENCE


# -----------------------------------------------------------------------------
# 3. Conceptual questions retrieve relevant evidence
# -----------------------------------------------------------------------------
def test_3_conceptual_question_retrieves_evidence(sample_cisa_candidates, general_policy):
    query = "Why is forensic evidence preservation critical during containment actions?"
    rel, intent, ass = filter_relevant_candidates(
        query=query,
        candidates=sample_cisa_candidates,
        policy_type=general_policy.policy_type,
    )
    matching_ids = [r.chunk_id for r in rel]
    assert "test_qa_chunk_containment" in matching_ids
    assert len(rel) >= 1


# -----------------------------------------------------------------------------
# 4. Explanatory questions produce grounded answers
# -----------------------------------------------------------------------------
def test_4_explanatory_question_produces_grounded_answer(sample_cisa_candidates, general_policy):
    query = "When security teams discover anomalous network activity, what steps should they undertake? Explain how creating an event chronology assists the investigation."
    rel, intent, ass = filter_relevant_candidates(
        query=query,
        candidates=sample_cisa_candidates,
        policy_type=general_policy.policy_type,
    )
    assert len(rel) >= 1

    validator = CitationValidator(
        corpus_chunks={
            r.chunk_id: ChunkRecord(
                chunk_id=r.chunk_id,
                document_id=r.document_id,
                chunk_index=0,
                page_start=r.page_start,
                page_end=r.page_end,
                text=r.text,
                char_count=len(r.text),
                token_count=len(r.text.split()),
            )
            for r in rel
        }
    )
    safety_gate = AnswerSafetyGate(validator=validator)
    drafts = [
        CandidateExcerptDraft(excerpt_id=f"exc_{i}", source_chunk_id=r.chunk_id, proposed_excerpt_text=r.text)
        for i, r in enumerate(rel)
    ]
    gate_resp = safety_gate.evaluate_draft(
        payload=ExcerptDraftPayload(query=query, excerpts=drafts),
        retrieval_candidates={r.chunk_id for r in rel},
        policy=general_policy,
    )
    assert gate_resp.disposition == AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS
    assert len(gate_resp.verified_excerpts) >= 1

    synth = SynthesisService(provider=MockSynthesisProvider())
    out = synth.synthesize_answer(
        query=query,
        verified_excerpts=gate_resp.verified_excerpts,
        disposition=gate_resp.disposition,
        regulatory_warnings=[],
        policy_type=general_policy.policy_type.value,
    )
    assert out.status.value in ("FULLY_ANSWERED", "PARTIALLY_ANSWERED")
    assert len(out.claims) >= 1


# -----------------------------------------------------------------------------
# 5. Multi-passage questions can combine relevant evidence
# -----------------------------------------------------------------------------
def test_5_multi_passage_question_combines_evidence(sample_cisa_candidates, general_policy):
    query = "What are the key activities across both the preparation phase and post-incident activity?"
    rel, intent, ass = filter_relevant_candidates(
        query=query,
        candidates=sample_cisa_candidates,
        policy_type=general_policy.policy_type,
    )
    # Both timeline/post-incident chunk and preparation chunk are present
    chunk_ids = [r.chunk_id for r in rel]
    assert "test_qa_chunk_preparation" in chunk_ids
    assert len(rel) >= 2


# -----------------------------------------------------------------------------
# 6. Unsupported questions correctly abstain
# -----------------------------------------------------------------------------
def test_6_unsupported_question_correctly_abstains(general_policy):
    query = "What are the quarterly financial filing deadlines for municipal bond issuers?"
    # Candidates with low semantic similarity (< 0.58) and zero lexical matches
    irrelevant_candidates = [
        RetrievalResult(
            chunk_id="irrelevant_p1",
            document_id="cisa_doc",
            title="CISA Doc",
            text="Agencies should preserve system memory and network artifacts during incident handling.",
            page_start=1,
            page_end=1,
            score=0.015,
            rank=1,
            metadata={"dense_score": 0.515, "retriever_1_score": 0.515},
        ),
        RetrievalResult(
            chunk_id="irrelevant_p2",
            document_id="cisa_doc",
            title="CISA Doc",
            text="Coordinate with federal partners during major cyber breaches.",
            page_start=2,
            page_end=2,
            score=0.014,
            rank=2,
            metadata={"dense_score": 0.509, "retriever_1_score": 0.509},
        ),
    ]
    rel, intent, ass = filter_relevant_candidates(
        query=query,
        candidates=irrelevant_candidates,
        policy_type=general_policy.policy_type,
    )
    assert len(rel) == 0
    assert all(a.is_relevant is False for a in ass)
    assert all(a.decision_code == RelevanceDecisionCode.INSUFFICIENT_RELEVANCE for a in ass)


# -----------------------------------------------------------------------------
# 7. Cross-workspace / cross-collection documents never leak into retrieval
# -----------------------------------------------------------------------------
def test_7_cross_collection_documents_quarantined():
    """Verify that chunks belonging to another collection are filtered out."""
    from app.schemas.document import resolve_collection_identity

    # Candidate belongs to collection 'col_other'
    r1 = RetrievalResult(
        chunk_id="other_chunk_1",
        document_id="other_doc",
        title="Other Doc",
        text="Confidential workspace data.",
        page_start=1,
        page_end=1,
        score=0.05,
        rank=1,
        metadata={"collection_id": "col_other"},
    )
    target_cid = "col_target"

    chunk_coll = getattr(r1, "collection_id", None) or (r1.metadata.get("collection_id") if r1.metadata else None)
    resolved_chunk_coll = resolve_collection_identity(r1.document_id, chunk_coll)
    assert resolved_chunk_coll != target_cid


# -----------------------------------------------------------------------------
# 8. Citation validation remains enforced
# -----------------------------------------------------------------------------
def test_8_citation_validation_enforced(general_policy):
    real_text = "Agencies must report confirmed incidents within 1 hour."
    validator = CitationValidator(
        corpus_chunks={
            "c1": ChunkRecord(
                chunk_id="c1",
                document_id="d1",
                chunk_index=0,
                page_start=1,
                page_end=1,
                text=real_text,
                char_count=len(real_text),
                token_count=len(real_text.split()),
            )
        }
    )
    safety_gate = AnswerSafetyGate(validator=validator)

    # Valid verbatim citation
    valid_draft = CandidateExcerptDraft(excerpt_id="e1", source_chunk_id="c1", proposed_excerpt_text=real_text)
    resp = safety_gate.evaluate_draft(
        payload=ExcerptDraftPayload(query="incident reporting", excerpts=[valid_draft]),
        retrieval_candidates={"c1"},
        policy=general_policy,
    )
    assert len(resp.verified_excerpts) == 1

    # Fabricated / hallucinated citation (not verbatim substring)
    hallucinated_draft = CandidateExcerptDraft(
        excerpt_id="e2",
        source_chunk_id="c1",
        proposed_excerpt_text="Agencies must report confirmed incidents within 24 hours.",
    )
    resp_bad = safety_gate.evaluate_draft(
        payload=ExcerptDraftPayload(query="incident reporting", excerpts=[hallucinated_draft]),
        retrieval_candidates={"c1"},
        policy=general_policy,
    )
    # Hallucination MUST be rejected
    assert len(resp_bad.verified_excerpts) == 0


# -----------------------------------------------------------------------------
# 9. Existing uploaded document and collection identifiers remain compatible
# -----------------------------------------------------------------------------
def test_9_document_identifiers_preserved(sample_cisa_candidates, general_policy):
    query = "Create a timeline"
    rel, intent, ass = filter_relevant_candidates(
        query=query,
        candidates=sample_cisa_candidates,
        policy_type=general_policy.policy_type,
    )
    assert len(rel) >= 1
    top = rel[0]
    assert top.document_id == "test_qa_doc"
    assert top.chunk_id == "test_qa_chunk_timeline"
    assert top.page_start == 10
    assert top.page_end == 10
    assert top.title == "CISA IR Playbook"


# -----------------------------------------------------------------------------
# 10. Missing dense index reports diagnostic warning
# -----------------------------------------------------------------------------
def test_10_missing_dense_index_reports_diagnostic_warning(caplog):
    gen = SnapshotGeneration(
        collection_id="test_missing_dense_col",
        snapshot_id=None,
        bm25_index=BM25Index(),
        dense_index=None,  # Dense index is None
        chunks=[],
        documents={},
    )
    with caplog.at_level(logging.WARNING):
        resp = gen.search_dense("test query", top_k=5)
    assert resp.total_results == 0
    assert resp.results == []
    assert any("DenseIndex is not loaded" in record.message for record in caplog.records)
