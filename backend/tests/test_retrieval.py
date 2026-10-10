import pytest
from pathlib import Path

from app.schemas.document import ChunkRecord, DocumentRecord
from app.retrieval.tokenizer import tokenize_text
from app.retrieval.bm25 import BM25Index
from app.retrieval.service import RetrievalService


def test_tokenizer_case_and_punctuation():
    """Verify deterministic tokenization, lowercasing, and clause/term preservation."""
    text = "SEBI CSCRF Circular CIR/2024/113: Clause 6.10 (RTO 2-hour, RPO 15-min) for Qualified REs."
    tokens = tokenize_text(text)
    assert "sebi" in tokens
    assert "cscrf" in tokens
    assert "circular" in tokens
    assert "cir/2024/113" in tokens or ("cir" in tokens and "2024" in tokens)
    assert "qualified" in tokens
    assert "res" in tokens

    # Test empty or None input
    assert tokenize_text("") == []
    assert tokenize_text(None) == []  # type: ignore


def test_bm25_index_basic_ranking():
    """Verify standard lexical ranking on synthetic chunks."""
    doc_a = DocumentRecord(
        document_id="doc_a",
        title="Cybersecurity Framework Guidelines",
        source_url="https://example.com/doc_a",
        sha256="abc1234567890",
        pdf_path="doc_a.pdf",
    )
    chunks = [
        ChunkRecord(
            chunk_id="chunk_1",
            document_id="doc_a",
            text="Qualified REs must implement dedicated Hardware Security Module HSM for cloud services.",
            page_start=1,
            page_end=1,
        ),
        ChunkRecord(
            chunk_id="chunk_2",
            document_id="doc_a",
            text="Small size and self certification REs can use alternatives to HSM approved by Board.",
            page_start=2,
            page_end=2,
        ),
        ChunkRecord(
            chunk_id="chunk_3",
            document_id="doc_a",
            text="Recovery Time Objective RTO shall be two hours for critical business operations.",
            page_start=3,
            page_end=3,
        ),
    ]

    index = BM25Index().build_from_chunks(chunks, documents={"doc_a": doc_a})

    # Query 1: Cloud HSM
    res_hsm = index.search("hardware security module HSM", top_k=2)
    assert res_hsm.total_results == 2
    assert res_hsm.results[0].chunk_id == "chunk_1"
    assert res_hsm.results[0].title == "Cybersecurity Framework Guidelines"
    assert res_hsm.results[0].score > res_hsm.results[1].score

    # Query 2: RTO
    res_rto = index.search("Recovery Time Objective RTO two hours", top_k=1)
    assert res_rto.total_results == 1
    assert res_rto.results[0].chunk_id == "chunk_3"
    assert res_rto.results[0].page_start == 3


def test_bm25_stable_tie_breaking():
    """Ensure equal scores break ties deterministically by chunk_id ascending."""
    chunks = [
        ChunkRecord(
            chunk_id="chunk_z",
            document_id="doc_1",
            text="identical search keywords",
            page_start=1,
            page_end=1,
        ),
        ChunkRecord(
            chunk_id="chunk_a",
            document_id="doc_1",
            text="identical search keywords",
            page_start=1,
            page_end=1,
        ),
        ChunkRecord(
            chunk_id="chunk_m",
            document_id="doc_1",
            text="identical search keywords",
            page_start=1,
            page_end=1,
        ),
    ]

    index = BM25Index().build_from_chunks(chunks)
    res = index.search("identical search keywords", top_k=3)
    assert res.total_results == 3
    # Equal score -> sort chunk_id ascending: chunk_a, chunk_m, chunk_z
    assert [r.chunk_id for r in res.results] == ["chunk_a", "chunk_m", "chunk_z"]


def test_bm25_edge_cases_empty_corpus_and_queries():
    """Verify safe handling of empty corpora, empty queries, and invalid top_k."""
    empty_index = BM25Index().build_from_chunks([])
    res_empty_corpus = empty_index.search("cybersecurity", top_k=5)
    assert res_empty_corpus.total_results == 0
    assert res_empty_corpus.results == []

    # Valid index with empty or whitespace query
    chunks = [
        ChunkRecord(
            chunk_id="c1",
            document_id="d1",
            text="some valid document text",
            page_start=1,
            page_end=1,
        )
    ]
    index = BM25Index().build_from_chunks(chunks)

    assert index.search("", top_k=5).total_results == 0
    assert index.search("   ", top_k=5).total_results == 0
    assert index.search("valid", top_k=0).total_results == 0
    assert index.search("valid", top_k=-1).total_results == 0


def test_retrieval_service_over_processed_corpus():
    """Verify that RetrievalService loads the real processed SEBI CSCRF corpus (168 chunks)."""
    service = RetrievalService(processed_dir="data/processed")
    doc_count, chunk_count = service.load_and_index_corpus()

    assert doc_count == 5
    assert chunk_count == 168

    # Search for specific SEBI CSCRF concepts
    res_august = service.search("August 31 2025 extension compliance timelines", top_k=3)
    assert res_august.total_results > 0
    # Top result should be from circular 2025/096 which introduced the August 31 extension
    top_doc_ids = [r.document_id for r in res_august.results]
    assert "sebi_cscrf_2025_096" in top_doc_ids

    # Check complete provenance
    top_hit = res_august.results[0]
    assert top_hit.chunk_id.startswith("sebi_cscrf_")
    assert top_hit.page_start >= 1
    assert top_hit.score > 0.0
    assert len(top_hit.text) > 0


def test_graph_safety_separation_from_retrieval():
    """
    Ensure lexical search scores are strictly informational relevance metrics
    and do not carry legal status assertions or automatic supersession flags.
    """
    service = RetrievalService(processed_dir="data/processed")
    res = service.search("ISO 27001 mandatory certification for Qualified REs", top_k=5)
    assert res.total_results > 0

    for item in res.results:
        # Results preserve clean chunk metadata without fabricated legal status
        assert isinstance(item.score, float)
        assert isinstance(item.rank, int)
        assert not hasattr(item, "is_legally_valid")
        assert not hasattr(item, "is_superseded")
