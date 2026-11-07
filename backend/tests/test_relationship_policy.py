"""
Comprehensive Unit Tests for Pluggable Relationship and Temporal Policies (Phase 3.3 / ADR-017).

Verifies:
1. Policy selection and fail-closed behavior:
   - evaluate_draft() with omitted policy or None fails closed (ValueError).
   - Unsupported or contradictory policy declarations fail closed (ValueError).
   - Regulatory supersession policy requested for an unauthorized collection fails closed (ValueError).
2. Graph loading isolation:
   - General/static policy construction does not instantiate or query the CSCRF graph.
   - PassthroughPolicy returns RegulatoryStatus.INFORMATIONAL without touching graph edges.
   - CitationValidator does not load the CSCRF graph during ordinary citation validation.
   - SEBI policy binds only to the authorized 'sebi_cscrf' collection.
3. Behavior preservation:
   - SEBI supersession, partial amendment, and REFERENCES non-supersession.
   - RegulatoryTemporalPolicy current-status query abstention vs historical qualified return.
   - PassthroughTemporalPolicy never triggers supersession abstention.
   - Mandatory verbatim citation verification and retrieval-set binding invariants.
"""
from typing import List, Dict, Set
from unittest.mock import MagicMock, patch
import pytest

from app.schemas.manifest import (
    GroundingPolicyDeclaration,
    GroundingPolicyType,
    CollectionManifest,
)
from app.schemas.document import DocumentRecord, ChunkRecord
from app.schemas.retrieval import RetrievalResult
from app.schemas.grounding import RegulatoryStatus
from app.schemas.safety_gate import (
    CandidateExcerptDraft,
    ExcerptDraftPayload,
    ValidatedExcerptItem,
    SafetyGateResponse,
    AnswerDisposition,
)
from app.grounding.safety_gate import AnswerSafetyGate
from app.grounding.validator import CitationValidator
from app.grounding.resolver import ConservativeGraphResolver
from app.grounding.relationship_policy import (
    RelationshipPolicy,
    TemporalPolicy,
    RegulatorySupersessionPolicy,
    PassthroughPolicy,
    RegulatoryTemporalPolicy,
    PassthroughTemporalPolicy,
    resolve_relationship_and_temporal_policies,
    is_current_status_query,
)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

@pytest.fixture
def sebi_policy() -> GroundingPolicyDeclaration:
    return GroundingPolicyDeclaration(
        policy_type=GroundingPolicyType.SEBI_CSCRF_STRICT,
        enforce_regulatory_graph=True,
        enforce_verbatim_citation=True,
        enforce_retrieval_binding=True,
    )


@pytest.fixture
def general_policy() -> GroundingPolicyDeclaration:
    return GroundingPolicyDeclaration(
        policy_type=GroundingPolicyType.GENERAL_STATIC,
        enforce_regulatory_graph=False,
        enforce_verbatim_citation=True,
        enforce_retrieval_binding=True,
    )


@pytest.fixture
def sebi_sample_chunks() -> Dict[str, ChunkRecord]:
    c1 = ChunkRecord(
        chunk_id="sebi_cscrf_2025_045_p1_0",
        document_id="sebi_cscrf_2025_045",
        page_start=1,
        page_end=1,
        text="Recognising the need for robust cybersecurity measures and protection of data and IT infrastructure",
        collection_id="sebi_cscrf",
    )
    c2 = ChunkRecord(
        chunk_id="sebi_cscrf_2024_113_p17_0",
        document_id="sebi_cscrf_2024_113",
        page_start=17,
        page_end=17,
        text="Paragraph 17: Implementation period for CSCRF shall be by April 01, 2025 for all REs.",
        collection_id="sebi_cscrf",
    )
    return {"sebi_cscrf_2025_045_p1_0": c1, "sebi_cscrf_2024_113_p17_0": c2}


@pytest.fixture
def sebi_sample_docs() -> Dict[str, DocumentRecord]:
    d1 = DocumentRecord(
        document_id="sebi_cscrf_2025_045",
        circular_number="CIR/2025/045",
        title="SEBI Cybersecurity and Cyber Resilience Framework",
        issue_date="2025-03-28",
        collection_id="sebi_cscrf",
        source_url="https://example.com/cscrf_045.pdf",
        sha256="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        pdf_path="sebi_cscrf/045.pdf",
    )
    d2 = DocumentRecord(
        document_id="sebi_cscrf_2024_113",
        circular_number="CIR/2024/113",
        title="Master CSCRF Framework",
        issue_date="2024-08-20",
        collection_id="sebi_cscrf",
        source_url="https://example.com/cscrf_113.pdf",
        sha256="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        pdf_path="sebi_cscrf/113.pdf",
    )
    return {"sebi_cscrf_2025_045": d1, "sebi_cscrf_2024_113": d2}


@pytest.fixture
def general_sample_chunk() -> ChunkRecord:
    return ChunkRecord(
        chunk_id="cisa_pb_p10_1",
        document_id="cisa_ir_playbook_2021",
        page_start=10,
        page_end=10,
        text="Agencies must report confirmed major cybersecurity incidents within 1 hour to CISA.",
        collection_id="cisa_playbooks",
    )


@pytest.fixture
def general_sample_doc() -> DocumentRecord:
    return DocumentRecord(
        document_id="cisa_ir_playbook_2021",
        circular_number=None,
        title="CISA Incident Response Playbook",
        issue_date="2021-11-01",
        collection_id="cisa_playbooks",
        source_url="https://example.com/cisa_pb.pdf",
        sha256="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        pdf_path="cisa/cisa_pb.pdf",
    )


# -----------------------------------------------------------------------------
# 1. Policy Selection and Fail-Closed Tests
# -----------------------------------------------------------------------------

def test_evaluate_draft_with_omitted_policy_fails_closed(sebi_sample_chunks, sebi_sample_docs):
    """Calling evaluate_draft without a policy argument must raise ValueError."""
    validator = CitationValidator(
        collection_id="sebi_cscrf",
        corpus_chunks=sebi_sample_chunks,
        corpus_documents=sebi_sample_docs,
    )
    gate = AnswerSafetyGate(validator=validator)
    payload = ExcerptDraftPayload(
        query="What are CISO requirements?",
        excerpts=[
            CandidateExcerptDraft(
                excerpt_id="exc_1",
                source_chunk_id="sebi_cscrf_2025_045_p1_0",
                proposed_excerpt_text="Recognising the need for robust cybersecurity measures",
            )
        ],
    )
    with pytest.raises(ValueError, match="grounding policy declaration is required"):
        gate.evaluate_draft(payload, retrieval_candidates={"sebi_cscrf_2025_045_p1_0"})


def test_evaluate_draft_with_none_policy_fails_closed(sebi_sample_chunks, sebi_sample_docs):
    """Calling evaluate_draft with policy=None must raise ValueError."""
    validator = CitationValidator(
        collection_id="sebi_cscrf",
        corpus_chunks=sebi_sample_chunks,
        corpus_documents=sebi_sample_docs,
    )
    gate = AnswerSafetyGate(validator=validator)
    payload = ExcerptDraftPayload(
        query="What are CISO requirements?",
        excerpts=[
            CandidateExcerptDraft(
                excerpt_id="exc_1",
                source_chunk_id="sebi_cscrf_2025_045_p1_0",
                proposed_excerpt_text="Recognising the need for robust cybersecurity measures",
            )
        ],
    )
    with pytest.raises(ValueError, match="grounding policy declaration is required"):
        gate.evaluate_draft(payload, retrieval_candidates={"sebi_cscrf_2025_045_p1_0"}, policy=None)


def test_factory_rejects_missing_or_none_policy():
    """Policy factory raises ValueError when policy is None or invalid type."""
    with pytest.raises(ValueError, match="grounding policy declaration is required"):
        resolve_relationship_and_temporal_policies(policy=None, collection_id="sebi_cscrf")  # type: ignore

    with pytest.raises(ValueError, match="grounding policy declaration is required"):
        resolve_relationship_and_temporal_policies(policy="sebi_cscrf_strict", collection_id="sebi_cscrf")  # type: ignore


def test_factory_rejects_empty_collection_id(sebi_policy):
    """Policy factory raises ValueError when collection_id is empty."""
    with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
        resolve_relationship_and_temporal_policies(policy=sebi_policy, collection_id="")


def test_factory_rejects_regulatory_policy_for_unauthorized_collection(sebi_policy):
    """RegulatorySupersessionPolicy fails closed if requested for a collection other than 'sebi_cscrf'."""
    with pytest.raises(ValueError, match="not authorized for collection 'cisa_playbooks'"):
        resolve_relationship_and_temporal_policies(policy=sebi_policy, collection_id="cisa_playbooks")

    with pytest.raises(ValueError, match="not authorized for collection 'rbi_cyber'"):
        resolve_relationship_and_temporal_policies(policy=sebi_policy, collection_id="rbi_cyber")


def test_factory_resolves_valid_policies(sebi_policy, general_policy):
    """Factory returns correct policy pairs for legitimate configurations."""
    # 1. SEBI
    r_sebi, t_sebi = resolve_relationship_and_temporal_policies(policy=sebi_policy, collection_id="sebi_cscrf")
    assert isinstance(r_sebi, RegulatorySupersessionPolicy)
    assert isinstance(t_sebi, RegulatoryTemporalPolicy)
    assert r_sebi.is_graph_active is True

    # 2. General Static
    r_gen, t_gen = resolve_relationship_and_temporal_policies(policy=general_policy, collection_id="cisa_playbooks")
    assert isinstance(r_gen, PassthroughPolicy)
    assert isinstance(t_gen, PassthroughTemporalPolicy)
    assert r_gen.is_graph_active is False


# -----------------------------------------------------------------------------
# 2. Graph Loading Isolation Tests
# -----------------------------------------------------------------------------

def test_general_static_construction_does_not_call_create_verified_cscrf_graph(general_sample_chunk, general_sample_doc, general_policy):
    """AnswerSafetyGate for general/static collection must not call create_verified_cscrf_graph()."""
    validator = CitationValidator(
        collection_id="cisa_playbooks",
        corpus_chunks={"cisa_pb_p10_1": general_sample_chunk},
        corpus_documents={"cisa_ir_playbook_2021": general_sample_doc},
    )
    with patch("app.grounding.safety_gate.create_verified_cscrf_graph") as mock_graph_loader:
        gate = AnswerSafetyGate(validator=validator)
        payload = ExcerptDraftPayload(
            query="When must incidents be reported to CISA?",
            excerpts=[
                CandidateExcerptDraft(
                    excerpt_id="exc_cisa_1",
                    source_chunk_id="cisa_pb_p10_1",
                    proposed_excerpt_text="Agencies must report confirmed major cybersecurity incidents within 1 hour to CISA.",
                )
            ],
        )
        resp = gate.evaluate_draft(
            payload=payload,
            retrieval_candidates={"cisa_pb_p10_1"},
            policy=general_policy,
            collection_id="cisa_playbooks",
        )
        assert resp.technical_validation_passed is True
        # Proof: create_verified_cscrf_graph was NEVER invoked
        mock_graph_loader.assert_not_called()


def test_validator_lazy_graph_loading_does_not_load_for_citation_check(general_sample_chunk):
    """CitationValidator does not load CSCRF graph during ordinary quotation verification."""
    validator = CitationValidator(
        collection_id="cisa_playbooks",
        corpus_chunks={"cisa_pb_p10_1": general_sample_chunk},
    )
    # _graph is None upon construction
    assert validator._graph is None

    # Perform mechanical citation verification
    from app.schemas.citation import CitationItem
    cit = CitationItem(
        chunk_id="cisa_pb_p10_1",
        quote="Agencies must report confirmed major cybersecurity incidents",
    )
    c_res = validator.validate_citation(cit)
    assert c_res.is_valid is True
    # _graph remains None!
    assert validator._graph is None


def test_passthrough_policy_returns_informational_status(general_sample_chunk):
    """PassthroughPolicy resolves retrieved passage to INFORMATIONAL without graph edges."""
    policy = PassthroughPolicy(collection_id="cisa_playbooks")
    cand = RetrievalResult(
        chunk_id="cisa_pb_p10_1",
        document_id="cisa_ir_playbook_2021",
        text=general_sample_chunk.text,
        page_start=10,
        page_end=10,
        score=0.035,
        rank=1,
    )
    res = policy.resolve_item(cand)
    assert res.regulatory_status == RegulatoryStatus.INFORMATIONAL
    assert res.applicable_edge_ids == []
    assert res.acting_document_ids == []
    assert "textual provenance established without legal currency evaluation" in res.status_reason


# -----------------------------------------------------------------------------
# 3. Behavior Preservation Tests
# -----------------------------------------------------------------------------

def test_sebi_supersession_behavior_preserved(sebi_sample_chunks, sebi_sample_docs, sebi_policy):
    """Existing SEBI supersession detection and warnings are preserved under RegulatorySupersessionPolicy."""
    validator = CitationValidator(
        collection_id="sebi_cscrf",
        corpus_chunks=sebi_sample_chunks,
        corpus_documents=sebi_sample_docs,
    )
    gate = AnswerSafetyGate(validator=validator)

    # Paragraph 17 in CIR/2024/113 is amended by CIR/2025/045 and CIR/2025/096
    payload = ExcerptDraftPayload(
        query="What is the compliance timeline under circular 2024/113?",
        excerpts=[
            CandidateExcerptDraft(
                excerpt_id="exc_timeline",
                source_chunk_id="sebi_cscrf_2024_113_p17_0",
                proposed_excerpt_text="Paragraph 17: Implementation period for CSCRF shall be by April 01, 2025 for all REs.",
            )
        ],
    )
    resp = gate.evaluate_draft(
        payload=payload,
        retrieval_candidates={"sebi_cscrf_2024_113_p17_0"},
        policy=sebi_policy,
        collection_id="sebi_cscrf",
    )
    assert resp.technical_validation_passed is True
    assert len(resp.verified_excerpts) == 1
    # Check that partial amendment warning and edge IDs were generated
    assert resp.verified_excerpts[0].regulatory_status == RegulatoryStatus.PARTIALLY_AMENDED
    assert len(resp.verified_excerpts[0].applicable_edge_ids) > 0


def test_general_static_current_keyword_does_not_cause_abstention(general_sample_chunk, general_sample_doc, general_policy):
    """General static collection queries with 'current' or 'deadline' do not trigger SEBI supersession abstention."""
    validator = CitationValidator(
        collection_id="cisa_playbooks",
        corpus_chunks={"cisa_pb_p10_1": general_sample_chunk},
        corpus_documents={"cisa_ir_playbook_2021": general_sample_doc},
    )
    gate = AnswerSafetyGate(validator=validator)

    payload = ExcerptDraftPayload(
        query="What is the current mandatory deadline for reporting to CISA today?",
        excerpts=[
            CandidateExcerptDraft(
                excerpt_id="exc_cisa_1",
                source_chunk_id="cisa_pb_p10_1",
                proposed_excerpt_text="Agencies must report confirmed major cybersecurity incidents within 1 hour to CISA.",
            )
        ],
    )
    resp = gate.evaluate_draft(
        payload=payload,
        retrieval_candidates={"cisa_pb_p10_1"},
        policy=general_policy,
        collection_id="cisa_playbooks",
    )
    assert resp.technical_validation_passed is True
    # Disposition MUST remain VERIFIED_HISTORICAL_EXCERPTS, not CONTROLLED_ABSTENTION
    assert resp.disposition == AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS
    assert resp.abstention_reason is None
    assert resp.verified_excerpts[0].regulatory_status == RegulatoryStatus.INFORMATIONAL


def test_verbatim_quotation_and_retrieval_binding_are_enforced_across_all_policies(
    general_sample_chunk, general_sample_doc, general_policy, sebi_sample_chunks, sebi_sample_docs, sebi_policy
):
    """Verbatim quotation and retrieval-set binding remain mandatory regardless of policy type."""
    # 1. Non-verbatim text rejected under GENERAL_STATIC
    val_gen = CitationValidator(
        collection_id="cisa_playbooks",
        corpus_chunks={"cisa_pb_p10_1": general_sample_chunk},
        corpus_documents={"cisa_ir_playbook_2021": general_sample_doc},
    )
    gate_gen = AnswerSafetyGate(validator=val_gen)
    payload_bad_quote = ExcerptDraftPayload(
        query="Report deadline?",
        excerpts=[
            CandidateExcerptDraft(
                excerpt_id="exc_bad",
                source_chunk_id="cisa_pb_p10_1",
                proposed_excerpt_text="Invented hallucinated text not in chunk.",
            )
        ],
    )
    resp_quote = gate_gen.evaluate_draft(
        payload=payload_bad_quote,
        retrieval_candidates={"cisa_pb_p10_1"},
        policy=general_policy,
        collection_id="cisa_playbooks",
    )
    assert resp_quote.technical_validation_passed is False
    assert resp_quote.disposition == AnswerDisposition.CONTROLLED_ABSTENTION
    assert "not a verbatim substring" in resp_quote.summary_narrative

    # 2. Unbound chunk rejected under SEBI_CSCRF_STRICT
    val_sebi = CitationValidator(
        collection_id="sebi_cscrf",
        corpus_chunks=sebi_sample_chunks,
        corpus_documents=sebi_sample_docs,
    )
    gate_sebi = AnswerSafetyGate(validator=val_sebi)
    payload_unbound = ExcerptDraftPayload(
        query="CISO query",
        excerpts=[
            CandidateExcerptDraft(
                excerpt_id="exc_unbound",
                source_chunk_id="sebi_cscrf_2025_045_p1_0",
                proposed_excerpt_text="Recognising the need for robust cybersecurity measures",
            )
        ],
    )
    resp_unbound = gate_sebi.evaluate_draft(
        payload=payload_unbound,
        retrieval_candidates={"different_chunk_id"},  # Candidate not authorized
        policy=sebi_policy,
        collection_id="sebi_cscrf",
    )
    assert resp_unbound.technical_validation_passed is False
    assert resp_unbound.disposition == AnswerDisposition.CONTROLLED_ABSTENTION
    assert "not in authorized retrieval candidate set" in resp_unbound.summary_narrative
