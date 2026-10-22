"""
Corpus loader and retrieval service for the processed SEBI circulars.
"""
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import List, Dict, Tuple, Optional, Any, TYPE_CHECKING

logger = logging.getLogger(__name__)

from app.schemas.document import ChunkRecord, DocumentRecord, resolve_collection_identity
from app.schemas.retrieval import RetrievalResponse, RetrievalFilter
from app.retrieval.bm25 import BM25Index
from app.retrieval.rrf import reciprocal_rank_fusion
from app.core.config import get_settings

if TYPE_CHECKING:
    from app.retrieval.dense import DenseIndex
    from app.retrieval.dense_embedder import DenseEmbedder


class CollectionRetrievalState:
    """Holds isolated retrieval state (BM25, Dense, Chunks, Documents) for a single collection."""

    def __init__(self, collection_id: str):
        self.collection_id = collection_id
        self.bm25_index = BM25Index()
        self.dense_index: Optional[Any] = None
        self.documents: Dict[str, DocumentRecord] = {}
        self.chunks: List[ChunkRecord] = []
        self.active_snapshot_id: Optional[str] = None
        self.is_indexed: bool = False
        self.is_dense_indexed: bool = False


class RetrievalService:
    """
    Service orchestrating corpus loading, lexical BM25 indexing, dense vector search, and hybrid fusion.
    Supports collection-isolated retrieval dispatch while maintaining backward compatibility for the SEBI pilot.
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
        self.indexes_dir = Path(indexes_dir).resolve()
        self.retrieval_mode = (retrieval_mode or settings.retrieval_mode).strip().lower()
        self._dense_embedder = dense_embedder

        # Per-collection isolated retrieval state cache
        self._collection_states: Dict[str, CollectionRetrievalState] = {}

        # Default legacy SEBI state references for backward compatibility with existing tests and callers
        self._default_state = self._get_or_create_collection_state("sebi_cscrf")

    def _validate_collection_id(self, collection_id: str) -> str:
        """Validate collection_id against safe character and naming rules."""
        if not collection_id or not isinstance(collection_id, str):
            raise ValueError("collection_id must be a non-empty string.")
        cid = collection_id.strip()
        if not re.match(r"^[a-zA-Z0-9_-]+$", cid):
            raise ValueError(
                f"Invalid collection_id '{cid}': must contain only alphanumeric characters, underscores, and hyphens."
            )
        return cid

    def _validate_path_containment(self, target_path: Path, root_path: Path) -> Path:
        """Enforces that target_path is strictly contained within root_path, preventing path traversal."""
        resolved_target = target_path.resolve()
        resolved_root = root_path.resolve()
        try:
            resolved_target.relative_to(resolved_root)
        except ValueError:
            raise ValueError(f"Path traversal detected: path '{target_path}' is outside approved root '{root_path}'.")
        return resolved_target

    def _get_or_create_collection_state(self, collection_id: str) -> CollectionRetrievalState:
        cid = self._validate_collection_id(collection_id)
        if cid not in self._collection_states:
            self._collection_states[cid] = CollectionRetrievalState(cid)
        return self._collection_states[cid]

    # --- Backward compatibility properties mapping to default 'sebi_cscrf' state ---
    @property
    def bm25_index(self) -> BM25Index:
        return self._default_state.bm25_index

    @bm25_index.setter
    def bm25_index(self, value: BM25Index) -> None:
        self._default_state.bm25_index = value

    @property
    def documents(self) -> Dict[str, DocumentRecord]:
        return self._default_state.documents

    @documents.setter
    def documents(self, value: Dict[str, DocumentRecord]) -> None:
        self._default_state.documents = value

    @property
    def chunks(self) -> List[ChunkRecord]:
        return self._default_state.chunks

    @chunks.setter
    def chunks(self, value: List[ChunkRecord]) -> None:
        self._default_state.chunks = value

    @property
    def active_snapshot_id(self) -> Optional[str]:
        return self._default_state.active_snapshot_id

    @active_snapshot_id.setter
    def active_snapshot_id(self, value: Optional[str]) -> None:
        self._default_state.active_snapshot_id = value

    @property
    def _is_indexed(self) -> bool:
        return self._default_state.is_indexed

    @_is_indexed.setter
    def _is_indexed(self, value: bool) -> None:
        self._default_state.is_indexed = value

    @property
    def _is_dense_indexed(self) -> bool:
        return self._default_state.is_dense_indexed

    @_is_dense_indexed.setter
    def _is_dense_indexed(self, value: bool) -> None:
        self._default_state.is_dense_indexed = value

    @property
    def is_lexical_only(self) -> bool:
        return self.retrieval_mode == "lexical_only"

    @property
    def dense_index(self) -> Any:
        if self._default_state.dense_index is None:
            if self.is_lexical_only:
                raise RuntimeError("DenseIndex is disabled in lexical_only retrieval mode.")
            from app.retrieval.dense import DenseIndex
            self._default_state.dense_index = DenseIndex(embedder=self._dense_embedder, index_dir=self.indexes_dir)
        return self._default_state.dense_index

    @dense_index.setter
    def dense_index(self, value: Any) -> None:
        self._default_state.dense_index = value

    def get_chunks(self, collection_id: str = "sebi_cscrf") -> List[ChunkRecord]:
        """Returns the loaded chunks for the specified collection."""
        cid = self._validate_collection_id(collection_id)
        state = self._get_or_create_collection_state(cid)
        if not state.is_indexed:
            self.load_and_index_corpus(collection_id=cid)
        return state.chunks

    def get_documents(self, collection_id: str = "sebi_cscrf") -> Dict[str, DocumentRecord]:
        """Returns the loaded document records for the specified collection."""
        cid = self._validate_collection_id(collection_id)
        state = self._get_or_create_collection_state(cid)
        if not state.is_indexed:
            self.load_and_index_corpus(collection_id=cid)
        return state.documents

    def get_active_snapshot_dir(self, collection_id: str = "sebi_cscrf") -> Optional[Path]:
        """
        Returns Path to active snapshot directory for collection_id if active_snapshot.json exists and is valid.
        For sebi_cscrf only, falls back to legacy root pointer data/indexes/active_snapshot.json if partition pointer is missing.
        """
        cid = self._validate_collection_id(collection_id)

        # 1. Check collection partition: data/indexes/{collection_id}/active_snapshot.json
        partition_dir = self.indexes_dir / cid
        try:
            self._validate_path_containment(partition_dir, self.indexes_dir)
        except ValueError:
            return None

        pointer_file = partition_dir / "active_snapshot.json"
        is_legacy = False

        if not pointer_file.exists():
            if cid == "sebi_cscrf":
                legacy_file = self.indexes_dir / "active_snapshot.json"
                if legacy_file.exists():
                    pointer_file = legacy_file
                    is_legacy = True
                else:
                    return None
            else:
                return None

        try:
            with open(pointer_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            snap_id = data.get("snapshot_id")
            ptr_coll = data.get("collection_id")
            manifest_chk = data.get("manifest_checksum")
            if not snap_id:
                return None

            # Verify collection ownership in pointer
            if ptr_coll and ptr_coll != cid:
                logger.error(f"Cross-collection pointer detected: expected '{cid}', found '{ptr_coll}'.")
                return None
            if not ptr_coll and (not is_legacy or cid != "sebi_cscrf"):
                logger.error(f"Pointer at '{pointer_file}' missing required collection_id for '{cid}'.")
                return None

            # Resolve snapshot directory
            if is_legacy:
                snap_dir = self.indexes_dir / "snapshots" / snap_id
            else:
                snap_dir = partition_dir / "snapshots" / snap_id

            # Containment check for snap_dir
            self._validate_path_containment(snap_dir, self.indexes_dir)

            manifest_path = snap_dir / "manifest.json"
            if snap_dir.exists() and manifest_path.exists():
                # Verify manifest integrity if checksum is recorded in pointer
                if manifest_chk:
                    raw_bytes = manifest_path.read_bytes()
                    raw_hash = hashlib.sha256(raw_bytes).hexdigest()
                    if raw_hash != manifest_chk:
                        lf_bytes = raw_bytes.replace(b"\r\n", b"\n")
                        crlf_bytes = lf_bytes.replace(b"\n", b"\r\n")
                        lf_hash = hashlib.sha256(lf_bytes).hexdigest()
                        crlf_hash = hashlib.sha256(crlf_bytes).hexdigest()
                        if manifest_chk not in (lf_hash, crlf_hash):
                            logger.error(f"Active snapshot {snap_id} manifest failed checksum verification.")
                            return None

                # Verify manifest collection ownership
                with open(manifest_path, "r", encoding="utf-8") as mf:
                    m_data = json.load(mf)
                m_coll = m_data.get("collection_id")
                if m_coll and m_coll != cid:
                    logger.error(f"Cross-collection manifest: expected '{cid}', found '{m_coll}'.")
                    return None
                if not m_coll and (not is_legacy or cid != "sebi_cscrf"):
                    logger.error(f"Manifest at '{manifest_path}' missing required collection_id for '{cid}'.")
                    return None

                return snap_dir
        except Exception as e:
            logger.warning(f"Failed to resolve active snapshot directory for collection '{cid}': {e}")
            pass
        return None

    def reload_if_stale(self, collection_id: str = "sebi_cscrf") -> bool:
        """
        Checks if active_snapshot.json pointer changed for the collection; if so, atomically reloads the snapshot.
        """
        cid = self._validate_collection_id(collection_id)
        state = self._get_or_create_collection_state(cid)

        # Check pointer
        partition_dir = self.indexes_dir / cid
        pointer_file = partition_dir / "active_snapshot.json"
        if not pointer_file.exists() and cid == "sebi_cscrf":
            pointer_file = self.indexes_dir / "active_snapshot.json"

        if not pointer_file.exists():
            return False

        try:
            with open(pointer_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            latest_id = data.get("snapshot_id")
            if latest_id and latest_id != state.active_snapshot_id:
                self.load_and_index_corpus(collection_id=cid)
                self.load_and_index_dense(collection_id=cid)
                return True
        except Exception:
            pass
        return False

    def load_and_index_corpus(self, collection_id: str = "sebi_cscrf") -> Tuple[int, int]:
        """
        Load document records and chunks for collection_id from its active published snapshot if available,
        otherwise fall back to processed_dir (pilot baseline for sebi_cscrf).
        Builds the BM25 index over the loaded chunks.

        Returns:
        - Tuple of (document_count, chunk_count)
        """
        cid = self._validate_collection_id(collection_id)
        state = self._get_or_create_collection_state(cid)

        snap_dir = self.get_active_snapshot_dir(collection_id=cid)
        if snap_dir is not None:
            chunks_path = snap_dir / "chunks.json"
            docs_path = snap_dir / "documents.json"
            if chunks_path.exists() and docs_path.exists():
                try:
                    with open(docs_path, "r", encoding="utf-8") as f:
                        docs_data = json.load(f)
                    with open(chunks_path, "r", encoding="utf-8") as f:
                        chunks_data = json.load(f)

                    # Verify ownership of loaded chunks and documents
                    filtered_docs = {}
                    for k, v in docs_data.items():
                        doc_rec = DocumentRecord(**v)
                        resolved_c = resolve_collection_identity(doc_rec.document_id, getattr(doc_rec, "collection_id", None))
                        if resolved_c == cid:
                            filtered_docs[k] = doc_rec

                    filtered_chunks = []
                    for c in chunks_data:
                        chunk_rec = ChunkRecord(**c)
                        resolved_c = resolve_collection_identity(chunk_rec.document_id, getattr(chunk_rec, "collection_id", None))
                        if resolved_c == cid or chunk_rec.document_id in filtered_docs:
                            filtered_chunks.append(chunk_rec)

                    state.documents = filtered_docs
                    state.chunks = filtered_chunks
                    state.active_snapshot_id = snap_dir.name
                    state.bm25_index.build_from_chunks(state.chunks, state.documents)
                    state.is_indexed = True
                    return len(state.documents), len(state.chunks)
                except Exception as e:
                    logger.warning(f"Failed loading snapshot data for collection '{cid}': {e}")
                    pass

        # Fallback to processed_dir (allowed only for sebi_cscrf baseline)
        if cid != "sebi_cscrf" or not self.processed_dir.exists():
            state.is_indexed = True
            state.documents = {}
            state.chunks = []
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

        if not doc_ids:
            doc_ids = [
                d.name for d in self.processed_dir.iterdir()
                if d.is_dir() and not d.name.startswith((".", "_"))
            ]

        docs_map: Dict[str, DocumentRecord] = {}
        chunks_list: List[ChunkRecord] = []

        for doc_id in sorted(doc_ids):
            # Resolve collection identity before including
            doc_folder = self.processed_dir / doc_id
            if not doc_folder.is_dir():
                continue

            doc_file = doc_folder / "document.json"
            chunks_file = doc_folder / "chunks.json"

            loaded_doc = None
            if doc_file.exists():
                try:
                    with open(doc_file, "r", encoding="utf-8") as f:
                        doc_data = json.load(f)
                    loaded_doc = DocumentRecord(**doc_data)
                except Exception:
                    pass

            resolved_c = resolve_collection_identity(doc_id, getattr(loaded_doc, "collection_id", None) if loaded_doc else None)
            if resolved_c != cid:
                continue

            if loaded_doc:
                docs_map[doc_id] = loaded_doc

            if chunks_file.exists():
                try:
                    with open(chunks_file, "r", encoding="utf-8") as f:
                        chunks_data = json.load(f)
                    for item in chunks_data:
                        chunk = ChunkRecord(**item)
                        chunks_list.append(chunk)
                except Exception:
                    pass

        state.documents = docs_map
        state.chunks = chunks_list
        state.bm25_index.build_from_chunks(state.chunks, state.documents)
        state.is_indexed = True
        return len(state.documents), len(state.chunks)

    def load_and_index_dense(self, auto_rebuild: bool = True, collection_id: str = "sebi_cscrf") -> bool:
        """
        Load persisted dense vector index for collection_id from active snapshot or indexes_dir.
        In lexical_only mode, dense indexing is safely skipped.
        """
        if self.is_lexical_only:
            state = self._get_or_create_collection_state(collection_id)
            state.is_dense_indexed = True
            return True

        cid = self._validate_collection_id(collection_id)
        state = self._get_or_create_collection_state(cid)

        if not state.is_indexed:
            self.load_and_index_corpus(collection_id=cid)

        if not state.chunks:
            state.is_dense_indexed = True
            return True

        from app.retrieval.dense import DenseIndex

        snap_dir = self.get_active_snapshot_dir(collection_id=cid)
        if snap_dir is not None:
            snap_dense = DenseIndex(embedder=self._dense_embedder, index_dir=snap_dir)
            if snap_dense.load(state.chunks, state.documents):
                state.dense_index = snap_dense
                state.is_dense_indexed = True
                return True

        # Check collection partition or indexes_dir
        if state.dense_index is None:
            index_dir = self.indexes_dir / cid if cid != "sebi_cscrf" else self.indexes_dir
            state.dense_index = DenseIndex(embedder=self._dense_embedder, index_dir=index_dir)

        loaded = state.dense_index.load(state.chunks, state.documents)
        if loaded:
            state.is_dense_indexed = True
            return True

        if auto_rebuild:
            state.dense_index.build_from_chunks(state.chunks, state.documents)
            state.dense_index.save()
            state.is_dense_indexed = True
            return True

        return False

    @staticmethod
    def preprocess_query(query: str) -> str:
        """
        Preprocesses a natural language query for optimal BM25 matching:
        1. Filters conversational question stop-words if substantive terms exist.
        2. Expands key domain acronyms (e.g. 'cscrf').
        """
        if not query:
            return ""
        tokens = [t.lower() for t in re.findall(r"[a-zA-Z0-9]+", query)]
        if not tokens:
            return query

        question_stop_words = {
            "what", "which", "where", "when", "whose", "whom", "who", "why", "how",
            "is", "are", "was", "were", "be", "been", "being", "the", "a", "an",
            "does", "did", "do", "has", "have", "had", "define", "meaning", "explain",
            "describe", "overview", "about",
        }

        substantive = [t for t in tokens if t not in question_stop_words]
        active_tokens = substantive if substantive else tokens

        expansions = {
            "cscrf": ["cscrf", "cybersecurity", "cyber", "resilience", "framework"],
        }

        expanded_tokens: List[str] = []
        for t in active_tokens:
            if t in expansions:
                for exp in expansions[t]:
                    if exp not in expanded_tokens:
                        expanded_tokens.append(exp)
            else:
                expanded_tokens.append(t)

        return " ".join(expanded_tokens)

    def search(
        self,
        query: str,
        top_k: int = 5,
        filters: Optional[RetrievalFilter] = None,
        collection_id: str = "sebi_cscrf",
    ) -> RetrievalResponse:
        """
        Execute lexical BM25 search over the specified collection's isolated index.
        """
        cid = self._validate_collection_id(collection_id)
        state = self._get_or_create_collection_state(cid)
        if not state.is_indexed:
            self.load_and_index_corpus(collection_id=cid)
        preprocessed = self.preprocess_query(query)
        search_q = preprocessed if preprocessed.strip() else query
        return state.bm25_index.search(query=search_q, top_k=top_k, filters=filters)

    def search_dense(
        self,
        query: str,
        top_k: int = 5,
        filters: Optional[RetrievalFilter] = None,
        collection_id: str = "sebi_cscrf",
    ) -> RetrievalResponse:
        """
        Execute dense vector cosine search over the specified collection's isolated index.
        """
        if self.is_lexical_only:
            raise RuntimeError("Dense search is disabled in lexical_only retrieval mode.")
        cid = self._validate_collection_id(collection_id)
        state = self._get_or_create_collection_state(cid)
        if not state.is_dense_indexed:
            self.load_and_index_dense(collection_id=cid)
        if state.dense_index is None:
            return RetrievalResponse(query=query, total_results=0, results=[])
        return state.dense_index.search(query=query, top_k=top_k, filters=filters)

    def search_hybrid(
        self,
        query: str,
        top_k: int = 5,
        candidate_k: int = 20,
        rrf_k: int = 60,
        filters: Optional[RetrievalFilter] = None,
        collection_id: str = "sebi_cscrf",
    ) -> RetrievalResponse:
        """
        Execute hybrid retrieval combining BM25 lexical and dense vector search via Reciprocal Rank Fusion (RRF)
        isolated strictly within the specified collection.
        In lexical_only mode, executes pure lexical BM25 search directly.
        """
        if not query or not query.strip():
            return RetrievalResponse(query=query, total_results=0, results=[])

        cid = self._validate_collection_id(collection_id)

        # Lexical-only mode: dispatch directly to lexical BM25 search
        if self.is_lexical_only:
            return self.search(query=query, top_k=top_k, filters=filters, collection_id=cid)

        # Retrieve candidate lists from component retrievers within collection
        bm25_resp = self.search(query=query, top_k=candidate_k, filters=filters, collection_id=cid)
        dense_resp = self.search_dense(query=query, top_k=candidate_k, filters=filters, collection_id=cid)

        # Fuse candidate lists using RRF
        return reciprocal_rank_fusion(
            ranked_lists=[bm25_resp.results, dense_resp.results],
            k=rrf_k,
            top_k=top_k,
            query=query,
        )
