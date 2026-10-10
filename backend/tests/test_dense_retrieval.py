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
    doc_a = DocumentRecord(
        document_id="doc_a",
        title="Framework Test",
        source_url="https://example.com/doc_a",
        sha256="abc1234567890",
        pdf_path="doc_a.pdf",
    )
    chunks = [
        ChunkRecord(
            chunk_id="chunk_1",
            document_id="doc_a",
            text="Cloud Security Hardware Security Module HSM requirements for Qualified REs.",
            page_start=1,
            page_end=1,
        ),
        ChunkRecord(
            chunk_id="chunk_2",
            document_id="doc_a",
            text="General guidelines for cyber audit reporting and compliance.",
            page_start=2,
            page_end=2,
        ),
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
        ChunkRecord(chunk_id="chunk_z", document_id="d1", text="same text", page_start=1, page_end=1),
        ChunkRecord(chunk_id="chunk_a", document_id="d1", text="same text", page_start=1, page_end=1),
        ChunkRecord(chunk_id="chunk_m", document_id="d1", text="same text", page_start=1, page_end=1),
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
        ChunkRecord(chunk_id="c1", document_id="d1", text="Sample chunk one", page_start=1, page_end=1),
        ChunkRecord(chunk_id="c2", document_id="d1", text="Sample chunk two", page_start=2, page_end=2),
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
        ChunkRecord(chunk_id="c1", document_id="d1", text="Modified chunk text", page_start=1, page_end=1),
        ChunkRecord(chunk_id="c2", document_id="d1", text="Sample chunk two", page_start=2, page_end=2),
    ]
    assert new_index.load(modified_chunks, index_name="test_index") is False


def test_dense_index_edge_cases(tmp_path):
    """Verify empty corpus, empty query, and invalid dimensions."""
    mock_embedder = MockEmbedder()
    empty_index = DenseIndex(embedder=mock_embedder, index_dir=tmp_path)
    empty_index.build_from_chunks([])
    assert empty_index.search("query").total_results == 0

    chunks = [ChunkRecord(chunk_id="c1", document_id="d1", text="valid", page_start=1, page_end=1)]
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
