"""
NumPy-based cosine similarity dense vector index with deterministic provenance and persistence.
"""
import json
import hashlib
from pathlib import Path
from typing import List, Dict, Tuple, Optional, Any
import numpy as np

from app.schemas.document import ChunkRecord, DocumentRecord
from app.schemas.retrieval import RetrievalResult, RetrievalResponse, RetrievalFilter
from app.retrieval.dense_embedder import DenseEmbedder, DEFAULT_MODEL_NAME, DEFAULT_EMBEDDING_DIM
from app.retrieval.bm25 import _matches_filter


class DenseIndex:
    """
    In-memory and persisted NumPy cosine-similarity dense vector index.
    """

    def __init__(
        self,
        embedder: Optional[DenseEmbedder] = None,
        index_dir: str | Path = "data/indexes",
    ):
        self.embedder = embedder or DenseEmbedder()
        self.index_dir = Path(index_dir)

        self.chunks: List[ChunkRecord] = []
        self.doc_metadata: Dict[str, DocumentRecord] = {}
        self.embeddings: np.ndarray = np.empty((0, DEFAULT_EMBEDDING_DIM), dtype=np.float32)

    @property
    def corpus_size(self) -> int:
        return len(self.chunks)

    def _compute_corpus_fingerprint(self, chunks: List[ChunkRecord]) -> str:
        """
        Compute SHA-256 fingerprint over ordered chunk IDs and text.
        """
        hasher = hashlib.sha256()
        for c in chunks:
            hasher.update(c.chunk_id.encode("utf-8"))
            hasher.update(c.text.encode("utf-8"))
        return hasher.hexdigest()

    def build_from_chunks(
        self,
        chunks: List[ChunkRecord],
        documents: Optional[Dict[str, DocumentRecord]] = None,
        custom_embeddings: Optional[np.ndarray] = None,
    ) -> "DenseIndex":
        """
        Build the dense index from a list of ChunkRecord objects.
        """
        self.chunks = list(chunks)
        self.doc_metadata = documents if documents is not None else {}

        if not self.chunks:
            self.embeddings = np.empty((0, DEFAULT_EMBEDDING_DIM), dtype=np.float32)
            return self

        if custom_embeddings is not None:
            arr = np.asarray(custom_embeddings, dtype=np.float32)
            if arr.shape[0] != len(self.chunks):
                raise ValueError(
                    f"Custom embeddings row count ({arr.shape[0]}) does not match chunks count ({len(self.chunks)})."
                )
            if not np.all(np.isfinite(arr)):
                raise ValueError("Custom embeddings contain non-finite values.")
            self.embeddings = arr
        else:
            texts = [c.text for c in self.chunks]
            self.embeddings = self.embedder.embed_texts(texts)

        return self

    def save(self, index_name: str = "dense_index") -> Tuple[Path, Path]:
        """
        Save embeddings array (.npy) and metadata (.json) under index_dir.
        """
        self.index_dir.mkdir(parents=True, exist_ok=True)
        npy_path = self.index_dir / f"{index_name}.npy"
        meta_path = self.index_dir / f"{index_name}_meta.json"

        np.save(npy_path, self.embeddings)

        meta = {
            "model_name": self.embedder.model_name,
            "embedding_dim": int(self.embeddings.shape[1]) if self.embeddings.ndim == 2 and self.embeddings.shape[0] > 0 else DEFAULT_EMBEDDING_DIM,
            "num_chunks": len(self.chunks),
            "chunk_ids": [c.chunk_id for c in self.chunks],
            "fingerprint": self._compute_corpus_fingerprint(self.chunks),
        }

        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

        return npy_path, meta_path

    def load(
        self,
        chunks: List[ChunkRecord],
        documents: Optional[Dict[str, DocumentRecord]] = None,
        index_name: str = "dense_index",
    ) -> bool:
        """
        Load index from disk and validate against provided chunks and fingerprint.
        Returns True if successfully loaded, False if stale, missing, or corrupt.
        """
        npy_path = self.index_dir / f"{index_name}.npy"
        meta_path = self.index_dir / f"{index_name}_meta.json"

        if not npy_path.exists() or not meta_path.exists():
            return False

        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)

            if meta.get("model_name") != self.embedder.model_name:
                return False

            if meta.get("num_chunks") != len(chunks):
                return False

            expected_fingerprint = self._compute_corpus_fingerprint(chunks)
            if meta.get("fingerprint") != expected_fingerprint:
                return False

            embeddings = np.load(npy_path)
            if embeddings.shape[0] != len(chunks):
                return False
            if not np.all(np.isfinite(embeddings)):
                return False

            self.chunks = list(chunks)
            self.doc_metadata = documents if documents is not None else {}
            self.embeddings = embeddings.astype(np.float32)
            return True
        except Exception:
            return False

    def search(
        self,
        query: str,
        top_k: int = 5,
        query_vector: Optional[np.ndarray] = None,
        filters: Optional[RetrievalFilter] = None,
    ) -> RetrievalResponse:
        """
        Execute dense vector search using cosine similarity with optional pre-fusion metadata filtering.
        """
        if top_k <= 0 or self.corpus_size == 0 or self.embeddings.shape[0] == 0:
            return RetrievalResponse(query=query, total_results=0, results=[])

        if query_vector is not None:
            q_vec = np.asarray(query_vector, dtype=np.float32).flatten()
        else:
            if not query or not query.strip():
                return RetrievalResponse(query=query, total_results=0, results=[])
            q_vec = self.embedder.embed_query(query)

        if q_vec.size == 0 or not np.all(np.isfinite(q_vec)):
            return RetrievalResponse(query=query, total_results=0, results=[])

        # Normalize query vector if needed
        norm = np.linalg.norm(q_vec)
        if norm > 0:
            q_vec = q_vec / norm

        # Cosine similarity via dot product (since chunks are unit-normalized)
        sim_scores = np.dot(self.embeddings, q_vec)

        # Build candidate score list: (score, chunk_id, idx)
        scores: List[Tuple[float, str, int]] = []
        for idx, score_val in enumerate(sim_scores):
            chunk = self.chunks[idx]
            doc_meta = self.doc_metadata.get(chunk.document_id)

            # Pre-ranking metadata filter
            if not _matches_filter(chunk, doc_meta, filters):
                continue

            scores.append((round(float(score_val), 6), chunk.chunk_id, idx))

        # Deterministic sort: score DESC, chunk_id ASC
        scores.sort(key=lambda x: (-x[0], x[1]))

        top_scores = scores[:top_k]
        results: List[RetrievalResult] = []

        for rank, (score, _, idx) in enumerate(top_scores, start=1):
            chunk = self.chunks[idx]
            doc_meta = self.doc_metadata.get(chunk.document_id)

            result_item = RetrievalResult(
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                title=doc_meta.title if doc_meta else None,
                circular_number=doc_meta.circular_number if doc_meta else None,
                issue_date=doc_meta.issue_date if doc_meta else None,
                effective_date=doc_meta.effective_date if doc_meta else None,
                regulator=getattr(doc_meta, "regulator", "SEBI") if doc_meta else "SEBI",
                domain=getattr(doc_meta, "domain", "CSCRF") if doc_meta else "CSCRF",
                sha256=doc_meta.sha256 if doc_meta else None,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                text=chunk.text,
                score=round(score, 6),
                rank=rank,
                is_ocr=chunk.is_ocr,
                metadata=chunk.metadata,
            )
            results.append(result_item)

        return RetrievalResponse(
            query=query,
            total_results=len(results),
            results=results,
        )
