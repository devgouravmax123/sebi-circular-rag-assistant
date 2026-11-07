"""
Phase 4 — Stage D: Unit Tests for Stage D Benchmark Runner (Phase 4.2B).

Verifies:
- Adapter integration and 79-query loading.
- Generation-pinned snapshot resolution independent of active_snapshot.json.
- Collection-specific evidence matching (chunk for SEBI/General, page range for CISA).
- Clear separation between Recall@k (>= 1 hit) and Full Evidence Coverage (all required items).
- Abstention query candidate reporting without synthesis invocation.
- Cross-collection leakage detection.
- Retrieval failure handling and explicit error recording.
- Rejection of unknown collections.
- Output file protection preventing overwriting of baseline datasets.
"""
from pathlib import Path
import pytest

from evaluation.stage_d_dataset import (
    StageDDatasetAdapter,
    StageDQueryRecord,
    NormalizedOutcome,
    SourceProvenance,
    APPROVED_SNAPSHOT_MAPPINGS,
)
from evaluation.stage_d_benchmark import (
    StageDBenchmarkRunner,
    StageDQueryResult,
    PROTECTED_BASELINE_FILES,
)
from app.schemas.retrieval import RetrievalResult, RetrievalResponse

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def runner():
    return StageDBenchmarkRunner(repo_root=PROJECT_ROOT)


def make_test_query(
    query_id: str = "test_q1",
    collection_id: str = "sebi_cscrf",
    outcome: NormalizedOutcome = NormalizedOutcome.ANSWERABLE,
    expected_docs: list[str] = None,
    expected_chunks: list[str] = None,
    expected_pages: list[int] = None,
) -> StageDQueryRecord:
    """Helper creating a valid StageDQueryRecord for testing."""
    return StageDQueryRecord(
        query_id=query_id,
        collection_id=collection_id,
        query_text="What are the compliance requirements?",
        normalized_outcome=outcome,
        category="clause_fact",
        expected_document_ids=expected_docs or [],
        expected_chunk_ids=expected_chunks or [],
        expected_pages=expected_pages or [],
        key_facts_to_preserve=["mandatory compliance"],
        rationale="Test rationale",
        provenance=SourceProvenance(
            source_dataset="test_dataset",
            source_record_id=query_id,
            original_outcome_label="answerable" if outcome == NormalizedOutcome.ANSWERABLE else "abstention",
            raw_record={},
        ),
    )


def test_runner_adapter_integration_and_counts(runner):
    """Verifies adapter integration, query loading, and pinned snapshot mapping resolution."""
    all_queries = runner.adapter.load_all()
    assert len(all_queries) == 79

    # Verify pinned snapshot mappings
    runner.initialize_retrievers(verify_snapshots=True)
    assert len(runner._pinned_mappings) == 3
    assert runner._pinned_mappings["sebi_cscrf"].snapshot_id == "snap_20261011_104512_23a014"
    assert runner._pinned_mappings["cisa_playbooks"].snapshot_id == "snap_20261030_093345_cd6f0d"
    assert runner._pinned_mappings["general_synthetic"].snapshot_id == "snap_20261022_062310_395f75"


def test_sebi_chunk_and_document_evidence_matching(runner):
    """Verifies chunk-level and document-level Recall@k and reciprocal rank for SEBI."""
    query = make_test_query(
        query_id="q01_sebi",
        collection_id="sebi_cscrf",
        expected_docs=["sebi_cscrf_2024_113"],
        expected_chunks=["sebi_cscrf_2024_113_p80_67"],
        expected_pages=[80],
    )

    # Mock retrieved results with target chunk at rank 1
    retrieved = [
        RetrievalResult(
            chunk_id="sebi_cscrf_2024_113_p80_67",
            document_id="sebi_cscrf_2024_113",
            page_start=80,
            page_end=80,
            text="The reporting of the CISO shall be directly to the MD & CEO",
            score=0.85,
            rank=1,
        ),
        RetrievalResult(
            chunk_id="sebi_cscrf_2024_113_p81_68",
            document_id="sebi_cscrf_2024_113",
            page_start=81,
            page_end=81,
            text="Second chunk text",
            score=0.72,
            rank=2,
        ),
    ]

    result = runner.evaluate_query_result(query, retrieved, top_k=5)
    assert result.retrieval_status == "SUCCESS"
    assert result.primary_recall_at_k[1] == 1.0
    assert result.primary_recall_at_k[3] == 1.0
    assert result.full_evidence_coverage_at_k[1] is True
    assert result.document_recall_at_k[1] == 1.0
    assert result.reciprocal_rank == 1.0
    assert result.cross_collection_leakage_count == 0


def test_cisa_page_range_matching_without_chunk_fabrication(runner):
    """
    Verifies that CISA operational queries match on PDF page-range containment
    without requiring or fabricating chunk IDs.
    """
    query = make_test_query(
        query_id="cisa_q01",
        collection_id="cisa_playbooks",
        expected_docs=["cisa_ir_playbook_2021"],
        expected_chunks=[],  # CISA has no chunk IDs in source dataset
        expected_pages=[3, 18],
    )

    # Chunk 1 covers page 3 (page_start=3, page_end=4)
    # Chunk 2 covers page 18 (page_start=18, page_end=19)
    retrieved = [
        RetrievalResult(
            chunk_id="cisa_ir_playbook_2021_p3_1",
            document_id="cisa_ir_playbook_2021",
            page_start=3,
            page_end=4,
            text="Presents two playbooks: incident response and vulnerability response.",
            score=0.90,
            rank=1,
        ),
        RetrievalResult(
            chunk_id="cisa_ir_playbook_2021_p18_20",
            document_id="cisa_ir_playbook_2021",
            page_start=18,
            page_end=19,
            text="Notifying CISA within 1 hour as directed by OMB M-20-04.",
            score=0.75,
            rank=2,
        ),
    ]

    result = runner.evaluate_query_result(query, retrieved, top_k=5)
    assert result.retrieval_status == "SUCCESS"
    # At cutoff k=1, page 3 is matched (1 hit), so primary recall is 1.0
    assert result.primary_recall_at_k[1] == 1.0
    # But only 1 of 2 pages is covered at k=1, so full coverage is False
    assert result.full_evidence_coverage_at_k[1] is False

    # At cutoff k=3, both page 3 and 18 are covered
    assert result.primary_recall_at_k[3] == 1.0
    assert result.full_evidence_coverage_at_k[3] is True
    assert result.reciprocal_rank == 1.0
    # Confirms no chunk IDs were fabricated
    assert result.expected_chunk_ids == []


def test_full_evidence_coverage_distinct_from_recall_at_k(runner):
    """
    Proves that Recall@k (>= 1 evidence hit) is explicitly distinguished
    from Full Evidence Coverage (all required chunks retrieved).
    """
    query = make_test_query(
        query_id="broad_multi_chunk",
        collection_id="general_synthetic",
        expected_docs=["acme_security_policy", "acme_infra_handbook"],
        expected_chunks=["acme_security_policy_p1_0", "acme_infra_handbook_p1_0"],
    )

    # Retrieval only returns the first expected chunk, missing the second
    retrieved = [
        RetrievalResult(
            chunk_id="acme_security_policy_p1_0",
            document_id="acme_security_policy",
            page_start=1,
            page_end=2,
            text="Security policy chunk text",
            score=0.88,
            rank=1,
        ),
        RetrievalResult(
            chunk_id="unrelated_doc_chunk",
            document_id="acme_security_policy",
            page_start=2,
            page_end=2,
            text="Unrelated text",
            score=0.50,
            rank=2,
        ),
    ]

    result = runner.evaluate_query_result(query, retrieved, top_k=5)
    # Recall@1 and Recall@3 are 1.0 because at least 1 expected chunk was retrieved
    assert result.primary_recall_at_k[1] == 1.0
    assert result.primary_recall_at_k[3] == 1.0

    # But Full Evidence Coverage is False because acme_infra_handbook_p1_0 is missing
    assert result.full_evidence_coverage_at_k[1] is False
    assert result.full_evidence_coverage_at_k[3] is False
    # Fractional chunk recall is 0.5 (1 of 2 chunks)
    assert result.chunk_recall_at_k[1] == 0.5


def test_abstention_query_handling_and_no_synthesis(runner):
    """
    Verifies that ABSTAIN queries record candidate counts without evaluating
    retrieval recall or invoking synthesis / safety-gate.
    """
    query = make_test_query(
        query_id="q31_unanswerable_crypto",
        collection_id="sebi_cscrf",
        outcome=NormalizedOutcome.ABSTAIN,
        expected_docs=[],
        expected_chunks=[],
    )

    retrieved = [
        RetrievalResult(
            chunk_id="sebi_cscrf_2024_113_p1_0",
            document_id="sebi_cscrf_2024_113",
            page_start=1,
            page_end=1,
            text="Irrelevant text for crypto question",
            score=0.22,
            rank=1,
        )
    ]

    result = runner.evaluate_query_result(query, retrieved, top_k=5)
    assert result.retrieval_status == "SUCCESS"
    assert result.normalized_outcome == NormalizedOutcome.ABSTAIN
    # Recall metrics on abstention queries are not counted
    assert result.primary_recall_at_k[1] == 0.0
    assert result.full_evidence_coverage_at_k[1] is False
    assert result.reciprocal_rank == 0.0
    # Candidate scores are recorded
    assert len(result.candidate_scores) == 1
    assert result.candidate_scores[0] == 0.22
    assert "abstention_eval_note" in result.metadata


def test_cross_collection_leakage_detection(runner):
    """Verifies that chunks from an unrelated collection are flagged as cross-collection leakage."""
    runner.initialize_retrievers(verify_snapshots=True)

    query = make_test_query(
        query_id="sebi_leak_test",
        collection_id="sebi_cscrf",
        expected_docs=["sebi_cscrf_2024_113"],
        expected_chunks=["sebi_cscrf_2024_113_p80_67"],
    )

    # Contaminate SEBI results with an Acme doc from general_synthetic
    retrieved = [
        RetrievalResult(
            chunk_id="acme_security_policy_p1_0",
            document_id="acme_security_policy",  # Not a SEBI document!
            page_start=1,
            page_end=1,
            text="Foreign collection text",
            score=0.99,
            rank=1,
        )
    ]

    result = runner.evaluate_query_result(query, retrieved, top_k=5)
    assert result.cross_collection_leakage_count == 1


def test_retrieval_failure_handling(runner):
    """Verifies that exceptions during retrieval are captured cleanly with FAILED status."""
    runner.initialize_retrievers(verify_snapshots=True)

    query = make_test_query(query_id="failing_query", collection_id="sebi_cscrf")

    # Mock execute_retrieval to raise an error
    runner.execute_retrieval = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Simulated index corruption"))

    result = runner.run_query(query)
    assert result.retrieval_status == "FAILED"
    assert "Simulated index corruption" in result.error_message


def test_no_active_pointer_fallback(runner):
    """
    Verifies that runner initialization resolves to the canonical baseline snapshots
    and does not fall back to the 1-chunk test snapshot in data/indexes/active_snapshot.json.
    """
    runner.initialize_retrievers(verify_snapshots=True)
    sebi_gen = runner._generations["sebi_cscrf"]
    # Pinned snapshot must have the 168 baseline chunks, not the 1 chunk from active_snapshot.json
    assert len(sebi_gen.chunks) == 168
    assert len(sebi_gen.documents) == 5


def test_save_results_protects_baseline_files(runner, tmp_path):
    """Verifies that save_results refuses to overwrite any protected baseline or source dataset."""
    from evaluation.stage_d_benchmark import StageDBenchmarkSummary

    summary = StageDBenchmarkSummary(
        timestamp_utc="2026-11-03T18:00:00Z",
        total_queries=0,
        total_answerable=0,
        total_abstention=0,
        total_failed=0,
        cutoffs=[1, 3, 5],
        pinned_snapshots={},
        per_collection={},
        overall_mean_primary_recall_at_k={},
        overall_mean_reciprocal_rank=0.0,
        overall_cross_collection_leakage_rate=0.0,
        per_query_results=[],
    )

    for protected_name in PROTECTED_BASELINE_FILES:
        with pytest.raises(ValueError) as exc:
            runner.save_results(summary, output_path=protected_name)
        assert "Refusing to overwrite protected baseline file" in str(exc.value)


def test_save_results_safe_output_creation(runner, tmp_path):
    """Verifies that save_results successfully writes a valid JSON summary to a designated path."""
    from evaluation.stage_d_benchmark import StageDBenchmarkSummary

    summary = StageDBenchmarkSummary(
        timestamp_utc="2026-11-03T18:00:00Z",
        total_queries=1,
        total_answerable=1,
        total_abstention=0,
        total_failed=0,
        cutoffs=[1, 3, 5],
        pinned_snapshots={"sebi_cscrf": "snap_20261011_104512_23a014"},
        per_collection={},
        overall_mean_primary_recall_at_k={5: 1.0},
        overall_mean_reciprocal_rank=1.0,
        overall_cross_collection_leakage_rate=0.0,
        per_query_results=[],
    )

    out_file = tmp_path / "test_stage_d_summary.json"
    saved = runner.save_results(summary, output_path=out_file)
    assert saved.exists()
    assert saved.stat().st_size > 0


def test_unknown_collection_rejected(runner):
    """Verifies that querying an unknown or unregistered collection raises ValueError."""
    runner.initialize_retrievers(verify_snapshots=True)
    with pytest.raises(ValueError) as exc:
        runner.execute_retrieval(query_text="Sample query", collection_id="non_existent_collection")
    assert "not loaded" in str(exc.value)


def test_general_document_chunk_page_evidence_matching(runner):
    """Verifies evidence matching across chunk, document, and page dimensions for general synthetic."""
    query = make_test_query(
        query_id="broad_q01",
        collection_id="general_synthetic",
        expected_docs=["acme_security_policy"],
        expected_chunks=["acme_security_policy_p1_0"],
        expected_pages=[1],
    )

    retrieved = [
        RetrievalResult(
            chunk_id="acme_security_policy_p1_0",
            document_id="acme_security_policy",
            page_start=1,
            page_end=1,
            text="FIDO2 MFA is mandatory for all personnel.",
            score=0.95,
            rank=1,
        )
    ]

    result = runner.evaluate_query_result(query, retrieved, top_k=5)
    assert result.retrieval_status == "SUCCESS"
    assert result.primary_recall_at_k[1] == 1.0
    assert result.chunk_recall_at_k[1] == 1.0
    assert result.document_recall_at_k[1] == 1.0
    assert result.page_recall_at_k[1] == 1.0
    assert result.full_evidence_coverage_at_k[1] is True
    assert result.reciprocal_rank == 1.0
    assert result.cross_collection_leakage_count == 0


def test_per_collection_dispatch(runner):
    """Verifies that run_collection only loads and executes queries for the target collection."""
    runner.initialize_retrievers(verify_snapshots=True)

    # Mock run_query to avoid full query search and simply return a successful result
    dispatched_collections = []
    def mock_run_query(q, top_k=5):
        dispatched_collections.append(q.collection_id)
        return StageDQueryResult(
            query_id=q.query_id,
            collection_id=q.collection_id,
            normalized_outcome=q.normalized_outcome,
            category=q.category,
            pinned_snapshot_id="mock_snap",
            retrieval_status="SUCCESS",
        )

    runner.run_query = mock_run_query
    cisa_results = runner.run_collection("cisa_playbooks", top_k=5)
    assert len(cisa_results) == 15
    assert all(r.collection_id == "cisa_playbooks" for r in cisa_results)
    assert all(cid == "cisa_playbooks" for cid in dispatched_collections)


def test_deterministic_ordering_and_aggregation(runner):
    """Verifies that run_all aggregates results with deterministic collection and query ordering."""
    runner.initialize_retrievers(verify_snapshots=True)

    # Mock execute_retrieval to return mock hits deterministically
    def mock_execute(query_text, collection_id, top_k=5, candidate_k=20, rrf_k=60):
        return RetrievalResponse(
            query=query_text,
            total_results=1,
            results=[
                RetrievalResult(
                    chunk_id="mock_chunk",
                    document_id="mock_doc",
                    page_start=1,
                    page_end=1,
                    text="Mock hit",
                    score=0.9,
                    rank=1,
                )
            ],
        )

    runner.execute_retrieval = mock_execute
    summary = runner.run_all(top_k=5)

    assert summary.total_queries == 79
    assert len(summary.per_query_results) == 79

    # Verify deterministic ordering: sorted by (collection_id, query_id)
    result_keys = [(r.collection_id, r.query_id) for r in summary.per_query_results]
    assert result_keys == sorted(result_keys)

    # Verify per-collection breakdowns
    assert summary.per_collection["sebi_cscrf"].total_queries == 32
    assert summary.per_collection["cisa_playbooks"].total_queries == 15
    assert summary.per_collection["general_synthetic"].total_queries == 32


def test_failed_query_penalizes_recall_denominator(runner):
    """
    Verifies that when a query fails retrieval with FAILED status, it is NOT
    excluded from the answerable denominator and correctly penalizes the mean recall.
    """
    from evaluation.stage_d_benchmark import StageDBenchmarkSummary

    # Suppose 1 query succeeds (recall 1.0) and 1 query fails retrieval (recall 0.0)
    q1 = make_test_query(query_id="q1", outcome=NormalizedOutcome.ANSWERABLE)
    q2 = make_test_query(query_id="q2", outcome=NormalizedOutcome.ANSWERABLE)

    res1 = StageDQueryResult(
        query_id=q1.query_id,
        collection_id=q1.collection_id,
        normalized_outcome=q1.normalized_outcome,
        category=q1.category,
        pinned_snapshot_id="snap1",
        retrieval_status="SUCCESS",
        primary_recall_at_k={5: 1.0},
    )
    res2 = StageDQueryResult(
        query_id=q2.query_id,
        collection_id=q2.collection_id,
        normalized_outcome=q2.normalized_outcome,
        category=q2.category,
        pinned_snapshot_id="snap1",
        retrieval_status="FAILED",
        error_message="Index error",
        primary_recall_at_k={5: 0.0},
    )

    runner._pinned_mappings = {"sebi_cscrf": APPROVED_SNAPSHOT_MAPPINGS["sebi_cscrf"]}
    runner._is_initialized = True

    # Use runner to aggregate these two results
    runner.adapter.load_all = lambda: [q1, q2]
    runner.run_query = lambda q, top_k=5: res1 if q.query_id == "q1" else res2

    summary = runner.run_all(top_k=5)
    # Total answerable queries is 2 (both q1 and q2)
    assert summary.total_answerable == 2
    assert summary.total_failed == 1
    # Mean Recall@5 must be (1.0 + 0.0) / 2 = 0.5, NOT 1.0 / 1 = 1.0
    assert summary.overall_mean_primary_recall_at_k[5] == 0.5


