"""
Corpus loader and retrieval service for the processed SEBI circulars.
"""
import json
from pathlib import Path
from typing import List, Dict, Tuple, Optional

from app.schemas.document import ChunkRecord, DocumentRecord
from app.schemas.retrieval import RetrievalResponse
from app.retrieval.bm25 import BM25Index
from app.retrieval.dense import DenseIndex
from app.retrieval.dense_embedder import DenseEmbedder


from app.retrieval.rrf import reciprocal_rank_fusion


class RetrievalService:
    """
    Service orchestrating corpus loading, lexical BM25 indexing, dense vector search, and hybrid fusion.
    """

    def __init__(
        self,
        processed_dir: str | Path = "data/processed",
        indexes_dir: str | Path = "data/indexes",
        dense_embedder: Optional[DenseEmbedder] = None,
    ):
        self.processed_dir = Path(processed_dir)
        self.indexes_dir = Path(indexes_dir)
        self.bm25_index = BM25Index()
        self.dense_index = DenseIndex(embedder=dense_embedder, index_dir=indexes_dir)
        self.documents: Dict[str, DocumentRecord] = {}
        self.chunks: List[ChunkRecord] = []
        self._is_indexed: bool = False
        self._is_dense_indexed: bool = False

    def load_and_index_corpus(self) -> Tuple[int, int]:
        """
        Load all processed document records and chunks under data/processed/ and build the BM25 index.

        Returns:
        - Tuple of (document_count, chunk_count)
        """
        if not self.processed_dir.exists():
            self._is_indexed = True
            return 0, 0

        manifest_path = self.processed_dir / "manifest.json"
        doc_ids: List[str] = []

        if manifest_path.exists():
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest_data = json.load(f)
                doc_ids = list(manifest_data.get("documents", {}).keys())
            except Exception:
                doc_ids = []

        # If no manifest or empty, discover directories
        if not doc_ids:
            doc_ids = [
                d.name for d in self.processed_dir.iterdir()
                if d.is_dir() and not d.name.startswith((".", "_"))
            ]

        self.documents = {}
        self.chunks = []

        # Deterministically sort document IDs
        for doc_id in sorted(doc_ids):
            doc_folder = self.processed_dir / doc_id
            if not doc_folder.is_dir():
                continue

            doc_file = doc_folder / "document.json"
            chunks_file = doc_folder / "chunks.json"

            # Load document metadata if present
            if doc_file.exists():
                try:
                    with open(doc_file, "r", encoding="utf-8") as f:
                        doc_data = json.load(f)
                    self.documents[doc_id] = DocumentRecord(**doc_data)
                except Exception:
                    pass

            # Load chunks
            if chunks_file.exists():
                try:
                    with open(chunks_file, "r", encoding="utf-8") as f:
                        chunks_data = json.load(f)
                    for item in chunks_data:
                        chunk = ChunkRecord(**item)
                        self.chunks.append(chunk)
                except Exception:
                    pass

        # Build BM25 index
        self.bm25_index.build_from_chunks(self.chunks, self.documents)
        self._is_indexed = True
        return len(self.documents), len(self.chunks)

    def load_and_index_dense(self, auto_rebuild: bool = True) -> bool:
        """
        Load persisted dense vector index or build it if missing/stale.
        """
        if not self._is_indexed:
            self.load_and_index_corpus()

        if not self.chunks:
            self._is_dense_indexed = True
            return True

        loaded = self.dense_index.load(self.chunks, self.documents)
        if loaded:
            self._is_dense_indexed = True
            return True

        if auto_rebuild:
            self.dense_index.build_from_chunks(self.chunks, self.documents)
            self.dense_index.save()
            self._is_dense_indexed = True
            return True

        return False

    def search(self, query: str, top_k: int = 5) -> RetrievalResponse:
        """
        Execute lexical BM25 search over the loaded corpus index.
        """
        if not self._is_indexed:
            self.load_and_index_corpus()
        return self.bm25_index.search(query=query, top_k=top_k)

    def search_dense(self, query: str, top_k: int = 5) -> RetrievalResponse:
        """
        Execute dense vector cosine search over the loaded corpus index.
        """
        if not self._is_dense_indexed:
            self.load_and_index_dense()
        return self.dense_index.search(query=query, top_k=top_k)

    def search_hybrid(
        self,
        query: str,
        top_k: int = 5,
        candidate_k: int = 20,
        rrf_k: int = 60,
    ) -> RetrievalResponse:
        """
        Execute hybrid retrieval combining BM25 lexical and dense vector search via Reciprocal Rank Fusion (RRF).

        Args:
            query: Natural language or regulatory search query.
            top_k: Number of fused results to return.
            candidate_k: Number of raw candidate chunks retrieved from each component retriever before fusion.
            rrf_k: Smoothing constant k for RRF (default: 60).

        Returns:
            RetrievalResponse with fused scores and component rank/score diagnostics.
        """
        if not query or not query.strip():
            return RetrievalResponse(query=query, total_results=0, results=[])

        # Retrieve candidate lists from component retrievers
        bm25_resp = self.search(query=query, top_k=candidate_k)
        dense_resp = self.search_dense(query=query, top_k=candidate_k)

        # Fuse candidate lists using RRF
        return reciprocal_rank_fusion(
            ranked_lists=[bm25_resp.results, dense_resp.results],
            k=rrf_k,
            top_k=top_k,
            query=query,
        )
