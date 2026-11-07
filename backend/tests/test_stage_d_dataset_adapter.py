"""
Phase 4 — Stage D: Unit Tests for Stage D Unified Dataset Adapter (Phase 4.2A).

Verifies:
- Combined count of 79 records across the 3 current datasets.
- Per-collection counts: sebi_cscrf (32), cisa_playbooks (15), general_synthetic (32).
- Query-text normalization across 'query_text' and 'question' keys.
- Outcome normalization to ANSWERABLE / ABSTAIN while preserving original labels in provenance.
- Preservation of SEBI-specific provision references and regulatory context.
- Preservation of CISA page/document evidence without inventing chunk IDs.
- Preservation of general expected chunks, pages, and key facts.
- Rejection of duplicate query IDs.
- Rejection of missing/blank query text.
- Rejection of unsupported outcome labels.
- Rejection of invalid page numbers (non-positive, non-int).
- Deterministic ordering by (collection_id, query_id).
- Pinned snapshot mapping verification against disk artifacts.
- Complete independence from data/indexes/active_snapshot.json.
"""
import copy
import json
from pathlib import Path
import pytest

from evaluation.stage_d_dataset import (
    StageDDatasetAdapter,
    StageDQueryRecord,
    NormalizedOutcome,
    StageDValidationError,
    DuplicateQueryIdError,
    SnapshotMappingVerificationError,
    APPROVED_SNAPSHOT_MAPPINGS,
    validate_sebi_record,
    validate_cisa_record,
    validate_general_record,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def adapter():
    return StageDDatasetAdapter(repo_root=PROJECT_ROOT)


def test_actual_dataset_counts_and_collections(adapter):
    """Verifies that all 79 curated queries are loaded and counts match canonical specifications."""
    all_records = adapter.load_all()
    assert len(all_records) == 79

    grouped = adapter.get_queries_by_collection(all_records)
    assert len(grouped["sebi_cscrf"]) == 32
    assert len(grouped["cisa_playbooks"]) == 15
    assert len(grouped["general_synthetic"]) == 32

    # Verify query IDs are globally unique across all 79 records
    all_qids = [r.query_id for r in all_records]
    assert len(all_qids) == len(set(all_qids))


def test_query_text_field_normalization(adapter):
    """Verifies that query text is correctly extracted regardless of whether key is 'query_text' or 'question'."""
    sebi_records = adapter.load_sebi()
    for r in sebi_records:
        assert isinstance(r.query_text, str)
        assert len(r.query_text.strip()) > 0
        # In SEBI source, raw key is query_text
        assert r.query_text == r.provenance.raw_record["query_text"]

    cisa_records = adapter.load_cisa()
    for r in cisa_records:
        assert isinstance(r.query_text, str)
        assert len(r.query_text.strip()) > 0
        # In CISA source, raw key is question
        assert r.query_text == r.provenance.raw_record["question"]

    general_records = adapter.load_general()
    for r in general_records:
        assert isinstance(r.query_text, str)
        assert len(r.query_text.strip()) > 0
        # In General source, raw key is question
        assert r.query_text == r.provenance.raw_record["question"]


def test_outcome_normalization_and_label_preservation(adapter):
    """Verifies that outcome labels map cleanly to NormalizedOutcome while preserving original labels."""
    all_records = adapter.load_all()
    for r in all_records:
        assert isinstance(r.normalized_outcome, NormalizedOutcome)
        raw_label = r.provenance.original_outcome_label
        assert raw_label in ("answerable", "unanswerable", "abstention")

        if r.normalized_outcome == NormalizedOutcome.ANSWERABLE:
            assert raw_label == "answerable"
        else:
            assert raw_label in ("unanswerable", "abstention")


def test_sebi_domain_specific_assertions_preserved(adapter):
    """Verifies that SEBI regulatory fields, provision refs, and chunk labels are 100% preserved."""
    sebi_records = adapter.load_sebi()
    assert len(sebi_records) == 32

    for r in sebi_records:
        assert r.collection_id == "sebi_cscrf"
        assert r.sebi_payload is not None
        assert r.cisa_payload is None
        assert r.general_payload is None

        # Check that answerable queries have gold chunks and document labels
        if r.normalized_outcome == NormalizedOutcome.ANSWERABLE:
            assert len(r.expected_document_ids) > 0
            assert len(r.expected_chunk_ids) > 0
            assert all(d.startswith("sebi_cscrf_") for d in r.expected_document_ids)
            assert all(c.startswith("sebi_cscrf_") for c in r.expected_chunk_ids)

        # Check evidence item structure and provision references
        payload = r.sebi_payload
        assert isinstance(payload.evidence_items, list)
        assert isinstance(payload.provision_refs, list)
        assert isinstance(payload.annotation_status, dict)

    # Check a specific query with known provision ref
    ciso_q = next(r for r in sebi_records if r.query_id == "q01_ciso_reporting_line")
    assert "Annexure-J / GV.GO.S1 Clause 1" in ciso_q.sebi_payload.provision_refs
    assert 80 in ciso_q.expected_pages


def test_cisa_domain_specific_assertions_preserved(adapter):
    """Verifies that CISA operational fields are preserved and chunk IDs are NOT fabricated."""
    cisa_records = adapter.load_cisa()
    assert len(cisa_records) == 15

    for r in cisa_records:
        assert r.collection_id == "cisa_playbooks"
        assert r.cisa_payload is not None
        assert r.sebi_payload is None
        assert r.general_payload is None

        # Critical architectural invariant: CISA does NOT have expected_chunks in source dataset.
        # Adapter must NOT fabricate chunk IDs.
        assert r.expected_chunk_ids == []

        if r.normalized_outcome == NormalizedOutcome.ANSWERABLE:
            assert len(r.expected_document_ids) > 0
            assert "cisa_ir_playbook_2021" in r.expected_document_ids
            assert len(r.expected_pages) > 0
            assert all(isinstance(p, int) and p >= 1 for p in r.expected_pages)
            assert len(r.key_facts_to_preserve) > 0

    # Verify a known query
    q01 = next(r for r in cisa_records if r.query_id == "cisa_q01_two_playbooks_established")
    assert 3 in q01.expected_pages
    assert "incident response playbook" in q01.key_facts_to_preserve


def test_general_domain_specific_assertions_preserved(adapter):
    """Verifies that synthetic General fields (chunks, key facts, multi-topic claims) are preserved."""
    general_records = adapter.load_general()
    assert len(general_records) == 32

    for r in general_records:
        assert r.collection_id == "general_synthetic"
        assert r.general_payload is not None
        assert r.sebi_payload is None
        assert r.cisa_payload is None

        if r.normalized_outcome == NormalizedOutcome.ANSWERABLE:
            assert len(r.expected_document_ids) > 0
            assert len(r.expected_chunk_ids) > 0
            assert len(r.expected_pages) > 0
            assert len(r.key_facts_to_preserve) > 0

    # Verify broad_q01
    q01 = next(r for r in general_records if r.query_id == "broad_q01_paraphrase_mfa_requirement")
    assert "acme_security_policy_p1_0" in q01.expected_chunk_ids
    assert 1 in q01.expected_pages


def test_duplicate_query_id_detection(adapter, tmp_path):
    """Verifies that DuplicateQueryIdError is raised when duplicate query IDs exist."""
    # Create duplicate SEBI dataset
    orig_sebi = adapter.sebi_dataset_path
    with open(orig_sebi, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Duplicate first query
    data["queries"].append(copy.deepcopy(data["queries"][0]))
    dup_file = tmp_path / "dup_sebi.json"
    with open(dup_file, "w", encoding="utf-8") as f:
        json.dump(data, f)

    with pytest.raises(DuplicateQueryIdError) as exc_info:
        adapter.load_all(sebi_path=dup_file)

    assert "duplicate query_id" in str(exc_info.value).lower()
    assert "q01_ciso_reporting_line" in str(exc_info.value)


def test_validation_rejects_missing_or_blank_query_text():
    """Verifies that missing, empty, or whitespace-only query text is rejected."""
    bad_record = {
        "query_id": "test_q1",
        "query_text": "   ",
        "answerability": "answerable",
        "query_category": "clause_fact",
    }
    with pytest.raises(StageDValidationError) as exc:
        validate_sebi_record(bad_record, "test_source")
    assert "blank 'query_text'" in str(exc.value)

    bad_cisa = {
        "query_id": "cisa_test",
        "question": "",
        "expected_outcome": "answerable",
        "category": "factual_core",
    }
    with pytest.raises(StageDValidationError) as exc:
        validate_cisa_record(bad_cisa, "test_source")
    assert "blank 'question'" in str(exc.value)


def test_validation_rejects_unsupported_outcome():
    """Verifies that unsupported or ambiguous outcome labels raise StageDValidationError."""
    bad_record = {
        "query_id": "test_q2",
        "query_text": "Valid query text here?",
        "answerability": "partial_answer",
        "query_category": "clause_fact",
    }
    with pytest.raises(StageDValidationError) as exc:
        validate_sebi_record(bad_record, "test_source")
    assert "Unsupported outcome label 'partial_answer'" in str(exc.value)


def test_validation_rejects_invalid_page_or_evidence_types():
    """Verifies that non-positive page numbers or invalid types are rejected."""
    # Negative page in CISA
    bad_cisa = {
        "query_id": "cisa_test_neg_page",
        "question": "Valid question?",
        "expected_outcome": "answerable",
        "category": "factual_core",
        "expected_documents": ["cisa_ir_playbook_2021"],
        "expected_pages": [-5],
    }
    with pytest.raises(StageDValidationError) as exc:
        validate_cisa_record(bad_cisa, "test_source")
    assert "expected_pages" in str(exc.value)

    # Zero page in SEBI evidence_items
    bad_sebi = {
        "query_id": "sebi_test_zero_page",
        "query_text": "Valid question?",
        "answerability": "answerable",
        "query_category": "clause_fact",
        "document_labels": {"gold_document_ids": ["sebi_cscrf_2024_113"]},
        "chunk_labels": {
            "gold_chunk_ids": ["sebi_cscrf_2024_113_p1_0"],
            "evidence_items": [{"pdf_page": 0, "chunk_id": "sebi_cscrf_2024_113_p1_0"}],
        },
    }
    with pytest.raises(StageDValidationError) as exc:
        validate_sebi_record(bad_sebi, "test_source")
    assert "pdf_page" in str(exc.value)


def test_deterministic_ordering(adapter):
    """Verifies that load_all returns records in deterministic order sorted by (collection_id, query_id)."""
    run1 = adapter.load_all()
    run2 = adapter.load_all()

    ids1 = [(r.collection_id, r.query_id) for r in run1]
    ids2 = [(r.collection_id, r.query_id) for r in run2]

    assert ids1 == ids2
    # Check that sorting is strictly non-decreasing
    assert ids1 == sorted(ids1)


def test_snapshot_mapping_verification(adapter):
    """Verifies that static snapshot mapping verification succeeds against disk artifacts."""
    mappings = adapter.verify_pinned_snapshots(strict=True)
    assert len(mappings) == 3

    # Check SEBI mapping
    sebi_m = mappings["sebi_cscrf"]
    assert sebi_m.snapshot_id == "snap_20261011_104512_23a014"
    assert sebi_m.total_documents == 5
    assert sebi_m.total_chunks == 168
    assert sebi_m.verified is True

    # Check CISA mapping
    cisa_m = mappings["cisa_playbooks"]
    assert cisa_m.snapshot_id == "snap_20261030_093345_cd6f0d"
    assert cisa_m.total_documents == 1
    assert cisa_m.total_chunks == 46
    assert cisa_m.verified is True

    # Check General mapping
    gen_m = mappings["general_synthetic"]
    assert gen_m.snapshot_id == "snap_20261022_062310_395f75"
    assert gen_m.total_documents == 3
    assert gen_m.total_chunks == 3
    assert gen_m.verified is True


def test_snapshot_mapping_rejects_missing_directory(tmp_path):
    """Verifies that verify_pinned_snapshots fails clearly when a snapshot directory is missing."""
    empty_adapter = StageDDatasetAdapter(repo_root=tmp_path)
    with pytest.raises(SnapshotMappingVerificationError) as exc:
        empty_adapter.verify_pinned_snapshots(strict=True)
    assert "does not exist" in str(exc.value)


def test_independence_from_active_snapshot_pointer(adapter):
    """
    Verifies that snapshot verification and dataset loading are completely independent
    of data/indexes/active_snapshot.json (which points to a 1-chunk test snapshot).
    """
    active_ptr_path = PROJECT_ROOT / "data" / "indexes" / "active_snapshot.json"
    assert active_ptr_path.exists()
    with open(active_ptr_path, "r", encoding="utf-8") as f:
        ptr_data = json.load(f)

    # Active pointer is known to point to snap_20261102_141049_82bf12 (1 chunk)
    assert ptr_data["snapshot_id"] == "snap_20261102_141049_82bf12"
    assert ptr_data["total_chunks"] == 1

    # But adapter pinned mappings resolve to the 168-chunk baseline
    mappings = adapter.verify_pinned_snapshots(strict=True)
    assert mappings["sebi_cscrf"].snapshot_id == "snap_20261011_104512_23a014"
    assert mappings["sebi_cscrf"].total_chunks == 168

