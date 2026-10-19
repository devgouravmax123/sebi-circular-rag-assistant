"""
Corpus loader and retrieval service for the processed SEBI circulars.
"""
import hashlib
import json
import logging
from pathlib import Path
from typing import List, Dict, Tuple, Optional, Any, TYPE_CHECKING

logger = logging.getLogger(__name__)

from app.schemas.document import ChunkRecord, DocumentRecord
from app.schemas.retrieval import RetrievalResponse, RetrievalFilter
from app.retrieval.bm25 import BM25Index
from app.retrieval.rrf import reciprocal_rank_fusion
from app.core.config import get_settings

if TYPE_CHECKING:
    from app.retrieval.dense import DenseIndex
    from app.retrieval.dense_embedder import DenseEmbedder


class RetrievalService:
    """
    Service orchestrating corpus loading, lexical BM25 indexing, dense vector search, and hybrid fusion.
    """

    def __init__(
        self,
        processed_dir: str | Path = "data/processed",
        indexes_dir: str | Path = "data/indexes",
        dense_embedder: Optional[Any] = None,
        retrieval_mode: Optional[str] = None,
    ):
        settings = get_settings()
        self.processed_dir = Path(processed_dir)
        self.indexes_dir = Path(indexes_dir)
        self.retrieval_mode = (retrieval_mode or settings.retrieval_mode).strip().lower()
        self.bm25_index = BM25Index()
        self._dense_embedder = dense_embedder
        self._dense_index: Optional[Any] = None
        self.documents: Dict[str, DocumentRecord] = {}
        self.chunks: List[ChunkRecord] = []
        self._is_indexed: bool = False
        self._is_dense_indexed: bool = False
        self.active_snapshot_id: Optional[str] = None

    @property
    def is_lexical_only(self) -> bool:
        return self.retrieval_mode == "lexical_only"

    @property
    def dense_index(self) -> Any:
        if self._dense_index is None:
            if self.is_lexical_only:
                raise RuntimeError("DenseIndex is disabled in lexical_only retrieval mode.")
            from app.retrieval.dense import DenseIndex
            self._dense_index = DenseIndex(embedder=self._dense_embedder, index_dir=self.indexes_dir)
        return self._dense_index

    @dense_index.setter
    def dense_index(self, value: Any) -> None:
        self._dense_index = value

    def get_active_snapshot_dir(self) -> Optional[Path]:
        """
        Returns Path to active snapshot directory if active_snapshot.json exists and is valid.
        """
        pointer_file = self.indexes_dir / "active_snapshot.json"
        if not pointer_file.exists():
            return None
        try:
            with open(pointer_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            snap_id = data.get("snapshot_id")
            manifest_chk = data.get("manifest_checksum")
            if not snap_id:
                return None
            snap_dir = self.indexes_dir / "snapshots" / snap_id
            manifest_path = snap_dir / "manifest.json"
            if snap_dir.exists() and manifest_path.exists():
                # Verify manifest integrity if checksum is recorded in pointer
                if manifest_chk:
                    raw_bytes = manifest_path.read_bytes()
                    raw_hash = hashlib.sha256(raw_bytes).hexdigest()
                    if raw_hash != manifest_chk:
                        # Check newline-normalized variants to support cross-platform checkouts (Windows CRLF vs Linux LF)
                        lf_bytes = raw_bytes.replace(b"\r\n", b"\n")
                        crlf_bytes = lf_bytes.replace(b"\n", b"\r\n")
                        lf_hash = hashlib.sha256(lf_bytes).hexdigest()
                        crlf_hash = hashlib.sha256(crlf_bytes).hexdigest()
                        if manifest_chk not in (lf_hash, crlf_hash):
                            logger.error(f"Active snapshot {snap_id} manifest failed checksum verification.")
                            return None
                return snap_dir
        except Exception as e:
            logger.warning(f"Failed to resolve active snapshot directory: {e}")
            pass
        return None

    def reload_if_stale(self) -> bool:
        """
        Checks if active_snapshot.json pointer changed; if so, atomically reloads the snapshot.
        """
        pointer_file = self.indexes_dir / "active_snapshot.json"
        if not pointer_file.exists():
            return False
        try:
            with open(pointer_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            latest_id = data.get("snapshot_id")
            if latest_id and latest_id != self.active_snapshot_id:
                self.load_and_index_corpus()
                self.load_and_index_dense()
                return True
        except Exception:
            pass
        return False

    def load_and_index_corpus(self) -> Tuple[int, int]:
        """
        Load document records and chunks from the active published snapshot if available,
        otherwise fall back to processed_dir (pilot baseline).
        Builds the BM25 index over the loaded chunks.

        Returns:
        - Tuple of (document_count, chunk_count)
        """
        snap_dir = self.get_active_snapshot_dir()
        if snap_dir is not None:
            chunks_path = snap_dir / "chunks.json"
            docs_path = snap_dir / "documents.json"
            if chunks_path.exists() and docs_path.exists():
                try:
                    with open(docs_path, "r", encoding="utf-8") as f:
                        docs_data = json.load(f)
                    with open(chunks_path, "r", encoding="utf-8") as f:
                        chunks_data = json.load(f)

                    self.documents = {k: DocumentRecord(**v) for k, v in docs_data.items()}
                    self.chunks = [ChunkRecord(**c) for c in chunks_data]
                    self.active_snapshot_id = snap_dir.name
                    self.bm25_index.build_from_chunks(self.chunks, self.documents)
                    self._is_indexed = True
                    return len(self.documents), len(self.chunks)
                except Exception:
                    pass

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
        Load persisted dense vector index from active snapshot or indexes_dir.
        In lexical_only mode, dense indexing is safely skipped.
        """
        if self.is_lexical_only:
            self._is_dense_indexed = True
            return True

        if not self._is_indexed:
            self.load_and_index_corpus()

        if not self.chunks:
            self._is_dense_indexed = True
            return True

        snap_dir = self.get_active_snapshot_dir()
        if snap_dir is not None:
            from app.retrieval.dense import DenseIndex
            snap_dense = DenseIndex(embedder=self.dense_index.embedder, index_dir=snap_dir)
            if snap_dense.load(self.chunks, self.documents):
                self.dense_index = snap_dense
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

    def search(
        self,
        query: str,
        top_k: int = 5,
        filters: Optional[RetrievalFilter] = None,
    ) -> RetrievalResponse:
        """
        Execute lexical BM25 search over the loaded corpus index.
        """
        if not self._is_indexed:
            self.load_and_index_corpus()
        return self.bm25_index.search(query=query, top_k=top_k, filters=filters)

    def search_dense(
        self,
        query: str,
        top_k: int = 5,
        filters: Optional[RetrievalFilter] = None,
    ) -> RetrievalResponse:
        """
        Execute dense vector cosine search over the loaded corpus index.
        """
        if self.is_lexical_only:
            raise RuntimeError("Dense search is disabled in lexical_only retrieval mode.")
        if not self._is_dense_indexed:
            self.load_and_index_dense()
        return self.dense_index.search(query=query, top_k=top_k, filters=filters)

    def search_hybrid(
        self,
        query: str,
        top_k: int = 5,
        candidate_k: int = 20,
        rrf_k: int = 60,
        filters: Optional[RetrievalFilter] = None,
    ) -> RetrievalResponse:
        """
        Execute hybrid retrieval combining BM25 lexical and dense vector search via Reciprocal Rank Fusion (RRF).
        In lexical_only mode, executes pure lexical BM25 search directly.

        Args:
            query: Natural language or regulatory search query.
            top_k: Number of fused results to return.
            candidate_k: Number of raw candidate chunks retrieved from each component retriever before fusion.
            rrf_k: Smoothing constant k for RRF (default: 60).
            filters: Optional pre-fusion multi-domain metadata filter criteria.

        Returns:
            RetrievalResponse with fused scores (or BM25 scores in lexical_only mode) and component rank/score diagnostics.
        """
        if not query or not query.strip():
            return RetrievalResponse(query=query, total_results=0, results=[])

        # Lexical-only mode: dispatch directly to lexical BM25 search
        if self.is_lexical_only:
            return self.search(query=query, top_k=top_k, filters=filters)

        # Retrieve candidate lists from component retrievers with identical pre-fusion filters
        bm25_resp = self.search(query=query, top_k=candidate_k, filters=filters)
        dense_resp = self.search_dense(query=query, top_k=candidate_k, filters=filters)

        # Fuse candidate lists using RRF
        return reciprocal_rank_fusion(
            ranked_lists=[bm25_resp.results, dense_resp.results],
            k=rrf_k,
            top_k=top_k,
            query=query,
        )
