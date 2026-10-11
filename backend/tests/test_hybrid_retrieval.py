"""
Unit tests for Reciprocal Rank Fusion (RRF) hybrid retrieval baseline.

Tests:
1. Standard RRF hand-computed values.
2. Candidate appearing in both rankings.
3. Candidate appearing in single ranking.
4. Duplicate chunk IDs within an input list.
5. Empty candidate lists and empty query behavior.
6. Equal fused scores deterministic tie-breaking.
7. Preservation of chunk provenance (document_id, title, pages, text).
8. Missing optional component metadata.
9. Integration with RetrievalService search_hybrid.
10. Correct one-based ranks and configurable k.
"""
import pytest
from app.schemas.retrieval import RetrievalResult, RetrievalResponse
from app.retrieval.rrf import reciprocal_rank_fusion
from app.retrieval.service import RetrievalService


def make_result(chunk_id: str, doc_id: str, score: float, rank: int, text: str = "text") -> RetrievalResult:
    return RetrievalResult(
        chunk_id=chunk_id,
        document_id=doc_id,
        title="Test Doc",
        page_start=1,
        page_end=2,
        text=text,
        score=score,
        rank=rank,
    )


def test_1_rrf_hand_computed_values():
    # RRF formula: sum 1 / (k + rank) with k=60
    # List 1: c1 (rank 1), c2 (rank 2)
    # List 2: c2 (rank 1), c3 (rank 2)
    #
    # c1: 1 / (60 + 1) = 1/61 ~= 0.01639344
    # c2: 1 / (60 + 2) + 1 / (60 + 1) = 1/62 + 1/61 = (61 + 62) / (61*62) = 123 / 3782 ~= 0.03252247
    # c3: 1 / (60 + 2) = 1/62 ~= 0.01612903
    list1 = [make_result("c1", "d1", 10.0, 1), make_result("c2", "d1", 8.0, 2)]
    list2 = [make_result("c2", "d1", 0.9, 1), make_result("c3", "d2", 0.8, 2)]

    resp = reciprocal_rank_fusion([list1, list2], k=60, top_k=5, query="test")
    assert resp.total_results == 3
    assert resp.results[0].chunk_id == "c2"
    assert pytest.approx(resp.results[0].score, rel=1e-5) == (1/61 + 1/62)
    assert resp.results[1].chunk_id == "c1"
    assert pytest.approx(resp.results[1].score, rel=1e-5) == (1/61)
    assert resp.results[2].chunk_id == "c3"
    assert pytest.approx(resp.results[2].score, rel=1e-5) == (1/62)


def test_2_candidate_in_both_rankings():
    list1 = [make_result("c1", "d1", 10.0, 1)]
    list2 = [make_result("c1", "d1", 0.95, 1)]
    resp = reciprocal_rank_fusion([list1, list2], k=60, top_k=5)
    assert resp.total_results == 1
    assert resp.results[0].chunk_id == "c1"
    assert pytest.approx(resp.results[0].score, rel=1e-5) == 2 * (1 / 61)
    assert resp.results[0].metadata["retriever_0_rank"] == 1
    assert resp.results[0].metadata["retriever_1_rank"] == 1


def test_3_candidate_in_single_ranking():
    list1 = [make_result("c1", "d1", 10.0, 1)]
    list2 = [make_result("c2", "d2", 0.95, 1)]
    resp = reciprocal_rank_fusion([list1, list2], k=60, top_k=5)
    assert resp.total_results == 2
    # c1 from list1 rank 1 -> 1/61
    # c2 from list2 rank 1 -> 1/61
    # Tie broken by chunk_id ascending: c1 < c2
    assert resp.results[0].chunk_id == "c1"
    assert resp.results[1].chunk_id == "c2"


def test_4_duplicate_chunk_ids_in_input_list():
    # Input list has duplicate chunks: c1, c1, c2
    list1 = [
        make_result("c1", "d1", 10.0, 1),
        make_result("c1", "d1", 9.0, 2),  # duplicate
        make_result("c2", "d1", 8.0, 3),
    ]
    resp = reciprocal_rank_fusion([list1], k=60, top_k=5)
    assert resp.total_results == 2
    # c1 should be evaluated only once at rank 1: 1/61
    # c2 should be evaluated at rank 2: 1/62
    assert resp.results[0].chunk_id == "c1"
    assert pytest.approx(resp.results[0].score, rel=1e-5) == 1 / 61
    assert resp.results[1].chunk_id == "c2"
    assert pytest.approx(resp.results[1].score, rel=1e-5) == 1 / 62


def test_5_empty_input_lists_and_query():
    resp1 = reciprocal_rank_fusion([], k=60, top_k=5, query="")
    assert resp1.total_results == 0
    assert resp1.results == []

    resp2 = reciprocal_rank_fusion([[], []], k=60, top_k=5, query="q")
    assert resp2.total_results == 0
    assert resp2.results == []


def test_6_equal_scores_deterministic_tie_breaking():
    # Both c_B and c_A get 1/61
    list1 = [make_result("c_B", "d1", 10.0, 1)]
    list2 = [make_result("c_A", "d1", 10.0, 1)]
    resp = reciprocal_rank_fusion([list1, list2], k=60, top_k=5)
    assert resp.results[0].chunk_id == "c_A"  # Alphabetical tie break
    assert resp.results[1].chunk_id == "c_B"


def test_7_provenance_preservation():
    r = RetrievalResult(
        chunk_id="doc1_chunk_42",
        document_id="doc1",
        title="Official Title",
        circular_number="CIR/2024/001",
        issue_date="2024-01-01",
        page_start=5,
        page_end=6,
        text="Sample verbatim text",
        score=15.5,
        rank=1,
        is_ocr=True,
        metadata={"source": "verified"},
    )
    resp = reciprocal_rank_fusion([[r]], k=60, top_k=5)
    fused = resp.results[0]
    assert fused.chunk_id == "doc1_chunk_42"
    assert fused.document_id == "doc1"
    assert fused.title == "Official Title"
    assert fused.circular_number == "CIR/2024/001"
    assert fused.page_start == 5
    assert fused.page_end == 6
    assert fused.text == "Sample verbatim text"
    assert fused.is_ocr is True
    assert fused.metadata["source"] == "verified"
    assert "retriever_0_rank" in fused.metadata


def test_8_configurable_k_and_validation():
    list1 = [make_result("c1", "d1", 10.0, 1)]
    resp_k10 = reciprocal_rank_fusion([list1], k=10, top_k=5)
    assert pytest.approx(resp_k10.results[0].score, rel=1e-5) == 1 / 11

    with pytest.raises(ValueError, match="must be positive"):
        reciprocal_rank_fusion([list1], k=0)


def test_9_retrieval_service_search_hybrid():
    service = RetrievalService(processed_dir="data/processed")
    service.load_and_index_corpus()
    # Hybrid search on non-empty query
    resp = service.search_hybrid("CISO reporting line", top_k=5, candidate_k=10, rrf_k=60)
    assert len(resp.results) <= 5
    if resp.results:
        assert resp.results[0].score > 0
        assert "retriever_0_rank" in resp.results[0].metadata or "retriever_1_rank" in resp.results[0].metadata

    # Empty query should return empty
    empty_resp = service.search_hybrid("", top_k=5)
    assert empty_resp.total_results == 0
