import pytest
import numpy as np
from pathlib import Path
from unittest.mock import MagicMock

from app.schemas.document import ChunkRecord, DocumentRecord
from app.retrieval.dense_embedder import DenseEmbedder
from app.retrieval.dense import DenseIndex
from app.retrieval.service import RetrievalService


class MockEmbedder(DenseEmbedder):
    """Deterministic mock embedder for fast, reproducible unit tests without downloading models."""

    def __init__(self, dim: int = 384):
        super().__init__(model_name="mock-model", normalize_embeddings=True)
        self.dim = dim

    def _load_model(self):
        pass

    def embed_texts(self, texts, batch_size=32):
        if not texts:
            return np.empty((0, self.dim), dtype=np.float32)
        vectors = []
        for i, text in enumerate(texts):
            # Deterministic embedding based on hash of text
            vec = np.zeros(self.dim, dtype=np.float32)
            for ch in text:
                vec[ord(ch) % self.dim] += 1.0
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            vectors.append(vec)
        return np.array(vectors, dtype=np.float32)

    def embed_query(self, query: str):
        if not query or not query.strip():
            return np.empty((0,), dtype=np.float32)
        vec = np.zeros(self.dim, dtype=np.float32)
        for ch in query:
            vec[ord(ch) % self.dim] += 1.0
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec


def test_dense_embedder_empty_query():
    """Verify embed_query returns empty array on empty or whitespace strings without loading model."""
    embedder = DenseEmbedder(model_name="unloaded-test-model")
    assert embedder.embed_query("").size == 0
    assert embedder.embed_query("   ").size == 0
    assert embedder._model is None


def test_dense_index_basic_ranking_and_provenance(tmp_path):
    """Verify standard cosine ranking and provenance retention on synthetic chunks."""
    doc_a = DocumentRecord.model_validate({
        "document_id": "doc_a",
        "title": "Framework Test",
        "source_url": "https://example.com/doc_a",
        "sha256": "abc1234567890",
        "pdf_path": "doc_a.pdf",
    })
    chunks = [
        ChunkRecord.model_validate({
            "chunk_id": "chunk_1",
            "document_id": "doc_a",
            "text": "Cloud Security Hardware Security Module HSM requirements for Qualified REs.",
            "page_start": 1,
            "page_end": 1,
        }),
        ChunkRecord.model_validate({
            "chunk_id": "chunk_2",
            "document_id": "doc_a",
            "text": "General guidelines for cyber audit reporting and compliance.",
            "page_start": 2,
            "page_end": 2,
        }),
    ]

    mock_embedder = MockEmbedder()
    index = DenseIndex(embedder=mock_embedder, index_dir=tmp_path)
    index.build_from_chunks(chunks, documents={"doc_a": doc_a})

    assert index.corpus_size == 2
    assert index.embeddings.shape == (2, 384)

    # Search
    res = index.search("Hardware Security Module HSM", top_k=2)
    assert res.total_results == 2
    top = res.results[0]
    assert top.chunk_id == "chunk_1"
    assert top.document_id == "doc_a"
    assert top.title == "Framework Test"
    assert top.page_start == 1
    assert top.rank == 1
    assert isinstance(top.score, float)


def test_dense_index_stable_tie_breaking(tmp_path):
    """Ensure equal scores break ties deterministically by chunk_id ascending."""
    chunks = [
        ChunkRecord.model_validate({"chunk_id": "chunk_z", "document_id": "d1", "text": "same text", "page_start": 1, "page_end": 1}),
        ChunkRecord.model_validate({"chunk_id": "chunk_a", "document_id": "d1", "text": "same text", "page_start": 1, "page_end": 1}),
        ChunkRecord.model_validate({"chunk_id": "chunk_m", "document_id": "d1", "text": "same text", "page_start": 1, "page_end": 1}),
    ]
    mock_embedder = MockEmbedder()
    index = DenseIndex(embedder=mock_embedder, index_dir=tmp_path)
    index.build_from_chunks(chunks)

    res = index.search("same text", top_k=3)
    assert res.total_results == 3
    assert [r.chunk_id for r in res.results] == ["chunk_a", "chunk_m", "chunk_z"]


def test_dense_index_save_and_load_round_trip(tmp_path):
    """Verify serialization to .npy and .json and robust reloading with fingerprint check."""
    chunks = [
        ChunkRecord.model_validate({"chunk_id": "c1", "document_id": "d1", "text": "Sample chunk one", "page_start": 1, "page_end": 1}),
        ChunkRecord.model_validate({"chunk_id": "c2", "document_id": "d1", "text": "Sample chunk two", "page_start": 2, "page_end": 2}),
    ]
    mock_embedder = MockEmbedder()
    index = DenseIndex(embedder=mock_embedder, index_dir=tmp_path)
    index.build_from_chunks(chunks)
    index.save("test_index")

    # Reload into a new index instance
    new_index = DenseIndex(embedder=mock_embedder, index_dir=tmp_path)
    loaded = new_index.load(chunks, index_name="test_index")
    assert loaded is True
    assert new_index.corpus_size == 2
    assert new_index.embeddings.shape == (2, 384)

    # Test corrupted/modified chunks fingerprint mismatch
    modified_chunks = [
        ChunkRecord.model_validate({"chunk_id": "c1", "document_id": "d1", "text": "Modified chunk text", "page_start": 1, "page_end": 1}),
        ChunkRecord.model_validate({"chunk_id": "c2", "document_id": "d1", "text": "Sample chunk two", "page_start": 2, "page_end": 2}),
    ]
    assert new_index.load(modified_chunks, index_name="test_index") is False


def test_dense_index_edge_cases(tmp_path):
    """Verify empty corpus, empty query, and invalid dimensions."""
    mock_embedder = MockEmbedder()
    empty_index = DenseIndex(embedder=mock_embedder, index_dir=tmp_path)
    empty_index.build_from_chunks([])
    assert empty_index.search("query").total_results == 0

    chunks = [ChunkRecord.model_validate({"chunk_id": "c1", "document_id": "d1", "text": "valid", "page_start": 1, "page_end": 1})]
    index = DenseIndex(embedder=mock_embedder, index_dir=tmp_path)
    index.build_from_chunks(chunks)

    assert index.search("").total_results == 0
    assert index.search("   ").total_results == 0
    assert index.search("valid", top_k=0).total_results == 0
    assert index.search("valid", top_k=-1).total_results == 0


def test_retrieval_service_decoupled_bm25_and_dense(tmp_path):
    """Verify that RetrievalService keeps BM25 and Dense search distinct."""
    service = RetrievalService(
        processed_dir="data/processed",
        indexes_dir=tmp_path,
        dense_embedder=MockEmbedder(),
    )
    doc_count, chunk_count = service.load_and_index_corpus()
    assert doc_count == 5
    assert chunk_count == 168

    # BM25 search
    res_bm25 = service.search("August 31 2025 extension compliance timelines", top_k=3)
    assert res_bm25.total_results > 0

    # Dense search with mock embedder
    res_dense = service.search_dense("August 31 2025 extension compliance timelines", top_k=3)
    assert res_dense.total_results > 0
    assert len(res_dense.results) <= 3
    # Check that scores are distinct types / representations
    assert isinstance(res_dense.results[0].score, float)


def test_lazy_dense_indexing_after_lexical_loading_synchronizes_generation(tmp_path):
    """
    Task 12.3E.3.1 Regression:
    Verify that lexical loading followed by dense search lazily builds the dense index,
    and synchronizes the active SnapshotGeneration with both BM25 and Dense components.
    """
    service = RetrievalService(
        processed_dir="data/processed",
        indexes_dir=tmp_path,
        dense_embedder=MockEmbedder(),
    )
    doc_count, chunk_count = service.load_and_index_corpus()
    assert doc_count == 5
    assert chunk_count == 168

    # Active generation before dense loading has bm25 but no dense
    state = service._get_or_create_collection_state("sebi_cscrf")
    assert state.is_dense_indexed is False
    assert state.dense_index is None

    # Trigger lazy dense loading via get_active_generation / search_dense
    res_dense = service.search_dense("compliance timelines", top_k=3)
    assert res_dense.total_results > 0

    # Generation is synchronized
    gen = service.get_active_generation("sebi_cscrf")
    assert gen.dense_index is not None
    assert gen.bm25_index is not None
    assert state.is_dense_indexed is True


def test_repeated_dense_searches_do_not_rebuild_index(tmp_path, monkeypatch):
    """
    Task 12.3E.3.1 Regression:
    Ensure repeated dense searches reuse the in-memory dense index without rebuilding.
    """
    service = RetrievalService(
        processed_dir="data/processed",
        indexes_dir=tmp_path,
        dense_embedder=MockEmbedder(),
    )
    service.load_and_index_corpus()

    build_call_count = 0
    original_build = DenseIndex.build_from_chunks

    def counting_build(self, chunks, documents=None):
        nonlocal build_call_count
        build_call_count += 1
        return original_build(self, chunks, documents)

    monkeypatch.setattr(DenseIndex, "build_from_chunks", counting_build)

    # First search builds dense index
    res1 = service.search_dense("audit guidelines", top_k=2)
    assert res1.total_results > 0
    assert build_call_count == 1

    # Second search must reuse without calling build_from_chunks again
    res2 = service.search_dense("audit guidelines", top_k=2)
    assert res2.total_results > 0
    assert build_call_count == 1

    # Third search with different query must also reuse
    res3 = service.search_dense("extension timelines", top_k=2)
    assert res3.total_results > 0
    assert build_call_count == 1


def test_lexical_only_mode_does_not_invoke_dense_loading(tmp_path, monkeypatch):
    """
    Task 12.3E.3.1 Regression:
    In lexical_only mode, dense indexing is never invoked, and search_dense raises RuntimeError.
    """
    service = RetrievalService(
        processed_dir="data/processed",
        indexes_dir=tmp_path,
        dense_embedder=MockEmbedder(),
        retrieval_mode="lexical_only",
    )
    assert service.is_lexical_only is True

    doc_count, chunk_count = service.load_and_index_corpus()
    assert doc_count == 5

    build_called = False

    def fail_build(self, *args, **kwargs):
        nonlocal build_called
        build_called = True
        raise AssertionError("build_from_chunks must not be called in lexical_only mode")

    monkeypatch.setattr(DenseIndex, "build_from_chunks", fail_build)

    # BM25 search works normally
    res_bm25 = service.search("compliance timelines", top_k=3)
    assert res_bm25.total_results > 0
    assert build_called is False

    # Active generation has no dense index
    gen = service.get_active_generation()
    assert gen.dense_index is None

    # Dense search is rejected with RuntimeError
    with pytest.raises(RuntimeError, match="Dense search is disabled in lexical_only retrieval mode"):
        service.search_dense("compliance timelines")


def test_dense_loading_remains_collection_scoped(tmp_path):
    """
    Task 12.3E.3.1 Regression:
    Verify that lazy dense loading on one collection does not index or contaminate another.
    """
    service = RetrievalService(
        processed_dir="data/processed",
        indexes_dir=tmp_path,
        dense_embedder=MockEmbedder(),
    )

    # Manually configure state for collection A and B
    state_a = service._get_or_create_collection_state("coll_alpha")
    state_b = service._get_or_create_collection_state("coll_beta")

    chunk_a = ChunkRecord.model_validate({"chunk_id": "ca1", "document_id": "da1", "text": "Alpha cyber compliance text", "page_start": 1, "page_end": 1, "collection_id": "coll_alpha"})
    chunk_b = ChunkRecord.model_validate({"chunk_id": "cb1", "document_id": "db1", "text": "Beta financial ratios text", "page_start": 1, "page_end": 1, "collection_id": "coll_beta"})

    state_a.chunks = [chunk_a]
    state_a.documents = {"da1": DocumentRecord.model_validate({"document_id": "da1", "title": "Doc Alpha", "sha256": "sha_a", "source_url": "https://example.com/a", "pdf_path": "raw/da1.pdf", "collection_id": "coll_alpha"})}
    state_a.bm25_index.build_from_chunks(state_a.chunks, state_a.documents)
    state_a.is_indexed = True
    service._sync_state_generation(state_a)

    state_b.chunks = [chunk_b]
    state_b.documents = {"db1": DocumentRecord.model_validate({"document_id": "db1", "title": "Doc Beta", "sha256": "sha_b", "source_url": "https://example.com/b", "pdf_path": "raw/db1.pdf", "collection_id": "coll_beta"})}
    state_b.bm25_index.build_from_chunks(state_b.chunks, state_b.documents)
    state_b.is_indexed = True
    service._sync_state_generation(state_b)

    # Search dense on coll_alpha
    res_a = service.search_dense("cyber compliance", collection_id="coll_alpha")
    assert res_a.total_results == 1
    assert res_a.results[0].chunk_id == "ca1"

    # coll_alpha is dense-indexed; coll_beta is NOT yet dense-indexed
    assert state_a.is_dense_indexed is True
    assert state_a.dense_index is not None
    assert state_b.is_dense_indexed is False
    assert state_b.dense_index is None


def test_concurrent_lazy_loading_thread_safety(tmp_path):
    """
    Task 12.3E.3.1 Regression:
    Ensure concurrent threads initiating lazy dense loading do not corrupt state
    or trigger duplicate/conflicting generation updates.
    """
    import threading

    service = RetrievalService(
        processed_dir="data/processed",
        indexes_dir=tmp_path,
        dense_embedder=MockEmbedder(),
    )
    service.load_and_index_corpus()

    results = []
    errors = []

    def worker():
        try:
            res = service.search_dense("extension compliance timelines", top_k=2)
            results.append(res)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Concurrent workers encountered errors: {errors}"
    assert len(results) == 8
    for res in results:
        assert res.total_results > 0
        assert len(res.results) > 0

