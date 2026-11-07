"""
Corpus loader and retrieval service for the processed SEBI circulars.
"""
import hashlib
import json
import logging
import re
from collections import OrderedDict
from pathlib import Path
import threading
from typing import List, Dict, Tuple, Optional, Any, TYPE_CHECKING

logger = logging.getLogger(__name__)

from app.schemas.document import ChunkRecord, DocumentRecord, resolve_collection_identity
from app.schemas.retrieval import RetrievalResponse, RetrievalFilter
from app.retrieval.bm25 import BM25Index
from app.retrieval.rrf import reciprocal_rank_fusion
from app.core.config import get_settings
from app.storage import BaseStorageAdapter, LocalStorageAdapter, get_storage_adapter
from app.retrieval.snapshot_store import DurableSnapshotStore

if TYPE_CHECKING:
    from app.retrieval.dense import DenseIndex
    from app.retrieval.dense_embedder import DenseEmbedder


class SnapshotGeneration:
    """
    Immutable, consistent in-memory snapshot generation containing both BM25 and Dense
    indexes, chunk records, and document metadata for a single collection snapshot.
    Ensures per-query retrieval consistency without generation mixing.
    """

    def __init__(
        self,
        collection_id: str,
        snapshot_id: Optional[str],
        bm25_index: BM25Index,
        dense_index: Optional[Any],
        chunks: List[ChunkRecord],
        documents: Dict[str, DocumentRecord],
    ):
        self.collection_id = collection_id
        self.snapshot_id = snapshot_id
        self.bm25_index = bm25_index
        self.dense_index = dense_index
        self.chunks = list(chunks)
        self.documents = dict(documents)

    def search_bm25(
        self,
        query: str,
        top_k: int = 5,
        filters: Optional[RetrievalFilter] = None,
        preprocess_fn: Optional[Any] = None,
    ) -> RetrievalResponse:
        search_q = preprocess_fn(query) if preprocess_fn else query
        search_q = search_q if (search_q and search_q.strip()) else query
        return self.bm25_index.search(query=search_q, top_k=top_k, filters=filters)

    def search_dense(
        self,
        query: str,
        top_k: int = 5,
        filters: Optional[RetrievalFilter] = None,
    ) -> RetrievalResponse:
        if self.dense_index is None:
            return RetrievalResponse(query=query, total_results=0, results=[])
        return self.dense_index.search(query=query, top_k=top_k, filters=filters)


_USE_DEFAULT_FACTORY = object()


class CollectionRetrievalState:
    """Holds isolated retrieval state (BM25, Dense, Chunks, Documents, Generation) for a single collection."""

    def __init__(self, collection_id: str):
        self.collection_id = collection_id
        self.bm25_index = BM25Index()
        self.dense_index: Optional[Any] = None
        self.documents: Dict[str, DocumentRecord] = {}
        self.chunks: List[ChunkRecord] = []
        self.active_snapshot_id: Optional[str] = None
        self.is_indexed: bool = False
        self.is_dense_indexed: bool = False
        self.is_postgres_backed: bool = False
        self.active_generation: Optional[SnapshotGeneration] = None


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
        storage_adapter: Optional[BaseStorageAdapter] = None,
        max_cached_collections: Optional[int] = None,
        repository_factory: Any = _USE_DEFAULT_FACTORY,
    ):
        settings = get_settings()
        self.processed_dir = Path(processed_dir)
        self.indexes_dir = Path(indexes_dir).resolve()
        self.retrieval_mode = (retrieval_mode or settings.retrieval_mode).strip().lower()
        self._dense_embedder = dense_embedder
        self.max_cached_collections: int = (
            max_cached_collections
            if max_cached_collections is not None
            else getattr(settings, "max_cached_collections", 5)
        )
        if self.max_cached_collections < 1:
            self.max_cached_collections = 1

        if storage_adapter is not None:
            self.storage_adapter = storage_adapter
        else:
            self.storage_adapter = get_storage_adapter(settings)

        if repository_factory is _USE_DEFAULT_FACTORY:
            if settings.is_database_enabled and settings.database_url:
                try:
                    from app.persistence.postgres import PostgresStorageRepositoryFactory
                    self.repository_factory: Optional[Any] = PostgresStorageRepositoryFactory(
                        database_url=settings.database_url
                    )
                except Exception as ex:
                    logger.warning(f"Could not initialize PostgresStorageRepositoryFactory in RetrievalService: {ex}")
                    self.repository_factory = None
            else:
                self.repository_factory = None
        else:
            self.repository_factory = repository_factory

        self.durable_store = DurableSnapshotStore(
            storage_adapter=self.storage_adapter,
            indexes_dir=self.indexes_dir,
            session_factory=getattr(self.repository_factory, "session_factory", None),
        )

        # Thread synchronization lock for generation state swaps and cache evictions
        self._state_lock = threading.RLock()

        # Per-collection isolated retrieval state LRU cache
        self._collection_states: OrderedDict[str, CollectionRetrievalState] = OrderedDict()

        # Eagerly initialize default legacy SEBI state in cache
        self._get_or_create_collection_state("sebi_cscrf")

    @property
    def cached_collection_ids(self) -> List[str]:
        """Returns the list of currently cached collection IDs in LRU order (oldest to newest)."""
        with self._state_lock:
            return list(self._collection_states.keys())

    @property
    def cached_collection_count(self) -> int:
        """Returns the number of currently cached collections."""
        with self._state_lock:
            return len(self._collection_states)

    @property
    def _default_state(self) -> CollectionRetrievalState:
        """Legacy default SEBI state accessor ensuring state exists and LRU recency is maintained."""
        return self._get_or_create_collection_state("sebi_cscrf")

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
        with self._state_lock:
            if cid in self._collection_states:
                self._collection_states.move_to_end(cid)
                return self._collection_states[cid]

            # Evict LRU collection states until under max capacity
            while len(self._collection_states) >= self.max_cached_collections:
                evicted_cid, evicted_state = self._collection_states.popitem(last=False)
                logger.info(
                    f"Collection retrieval LRU cache capacity ({self.max_cached_collections}) reached. "
                    f"Evicted in-memory collection state for '{evicted_cid}'."
                )

            state = CollectionRetrievalState(cid)
            self._collection_states[cid] = state
            return state

    def _sync_state_generation(self, state: CollectionRetrievalState) -> None:
        """Synchronizes or constructs the SnapshotGeneration for a collection state under lock."""
        with self._state_lock:
            state.active_generation = SnapshotGeneration(
                collection_id=state.collection_id,
                snapshot_id=state.active_snapshot_id,
                bm25_index=state.bm25_index,
                dense_index=state.dense_index,
                chunks=state.chunks,
                documents=state.documents,
            )

    # --- Backward compatibility properties mapping to default 'sebi_cscrf' state ---
    @property
    def bm25_index(self) -> BM25Index:
        if self._default_state.active_generation is not None:
            return self._default_state.active_generation.bm25_index
        return self._default_state.bm25_index

    @bm25_index.setter
    def bm25_index(self, value: BM25Index) -> None:
        self._default_state.bm25_index = value
        self._sync_state_generation(self._default_state)

    @property
    def documents(self) -> Dict[str, DocumentRecord]:
        if self._default_state.active_generation is not None:
            return self._default_state.active_generation.documents
        return self._default_state.documents

    @documents.setter
    def documents(self, value: Dict[str, DocumentRecord]) -> None:
        self._default_state.documents = value
        self._sync_state_generation(self._default_state)

    @property
    def chunks(self) -> List[ChunkRecord]:
        if self._default_state.active_generation is not None:
            return self._default_state.active_generation.chunks
        return self._default_state.chunks

    @chunks.setter
    def chunks(self, value: List[ChunkRecord]) -> None:
        self._default_state.chunks = value
        self._sync_state_generation(self._default_state)

    @property
    def active_snapshot_id(self) -> Optional[str]:
        if self._default_state.active_generation is not None:
            return self._default_state.active_generation.snapshot_id
        return self._default_state.active_snapshot_id

    @active_snapshot_id.setter
    def active_snapshot_id(self, value: Optional[str]) -> None:
        self._default_state.active_snapshot_id = value
        self._sync_state_generation(self._default_state)

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
        if self._default_state.active_generation is not None and self._default_state.active_generation.dense_index is not None:
            return self._default_state.active_generation.dense_index
        if self._default_state.dense_index is None:
            if self.is_lexical_only:
                raise RuntimeError("DenseIndex is disabled in lexical_only retrieval mode.")
            from app.retrieval.dense import DenseIndex
            self._default_state.dense_index = DenseIndex(embedder=self._dense_embedder, index_dir=self.indexes_dir)
            self._sync_state_generation(self._default_state)
        return self._default_state.dense_index

    @dense_index.setter
    def dense_index(self, value: Any) -> None:
        self._default_state.dense_index = value
        self._sync_state_generation(self._default_state)

    def get_chunks(self, collection_id: str = "sebi_cscrf") -> List[ChunkRecord]:
        """Returns the loaded chunks for the specified collection."""
        cid = self._validate_collection_id(collection_id)
        gen = self.get_active_generation(collection_id=cid)
        if gen is not None:
            return gen.chunks
        state = self._get_or_create_collection_state(cid)
        return state.chunks

    def get_documents(self, collection_id: str = "sebi_cscrf") -> Dict[str, DocumentRecord]:
        """Returns the loaded document records for the specified collection."""
        cid = self._validate_collection_id(collection_id)
        gen = self.get_active_generation(collection_id=cid)
        if gen is not None:
            return gen.documents
        state = self._get_or_create_collection_state(cid)
        return state.documents

    def get_active_snapshot_dir(self, collection_id: str = "sebi_cscrf") -> Optional[Path]:
        """
        Returns Path to active snapshot directory for collection_id if active_snapshot.json exists and is valid.
        When remote storage is configured, resolves and hydrates from remote storage into local cache.
        For sebi_cscrf only, falls back to legacy root pointer data/indexes/active_snapshot.json if partition pointer is missing.
        """
        cid = self._validate_collection_id(collection_id)

        # Remote provider hydration branch
        if not isinstance(self.storage_adapter, LocalStorageAdapter):
            hydrated = self.durable_store.hydrate_snapshot(cid)
            if hydrated is not None:
                return hydrated
            if cid != "sebi_cscrf":
                return None

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

    def _get_doc_repo(self) -> Any:
        if self.repository_factory is None:
            return None
        getter = getattr(self.repository_factory, "get_document_repository", None) or getattr(
            self.repository_factory, "create_document_repository", None
        )
        return getter() if getter else None

    def _get_chunk_repo(self) -> Any:
        if self.repository_factory is None:
            return None
        getter = getattr(self.repository_factory, "get_chunk_repository", None) or getattr(
            self.repository_factory, "create_chunk_repository", None
        )
        return getter() if getter else None

    def has_collection_data(self, collection_id: str) -> bool:
        """
        Returns True if the collection has data loaded in memory, in PostgreSQL,
        or in an active snapshot on disk.
        """
        cid = self._validate_collection_id(collection_id)
        with self._state_lock:
            state = self._collection_states.get(cid)
            if state and (state.chunks or state.documents):
                return True
        if self.repository_factory is not None:
            try:
                doc_repo = self._get_doc_repo()
                docs = doc_repo.list_documents(cid) if doc_repo else []
                if docs:
                    return True
                chunk_repo = self._get_chunk_repo()
                chunks = chunk_repo.list_all_chunks(cid) if chunk_repo else []
                if chunks:
                    return True
            except Exception:
                pass
        snap_dir = self.get_active_snapshot_dir(cid)
        return snap_dir is not None and snap_dir.exists()

    def _load_isolated_generation(
        self,
        collection_id: str = "sebi_cscrf",
        target_snapshot_dir: Optional[Path] = None,
    ) -> Optional[SnapshotGeneration]:
        """
        Loads document records, chunks, BM25 index, and dense index into a new isolated
        SnapshotGeneration instance without modifying the current service or collection state.
        """
        cid = self._validate_collection_id(collection_id)
        snap_dir = target_snapshot_dir if target_snapshot_dir is not None else self.get_active_snapshot_dir(collection_id=cid)

        if snap_dir is not None:
            lock_path = snap_dir.parent / f".{snap_dir.name}.lock"
            with self.durable_store._acquire_process_lock(lock_path):
                chunks_path = snap_dir / "chunks.json"
                docs_path = snap_dir / "documents.json"
                if chunks_path.exists() and docs_path.exists():
                    with open(docs_path, "r", encoding="utf-8") as f:
                        docs_data = json.load(f)
                    with open(chunks_path, "r", encoding="utf-8") as f:
                        chunks_data = json.load(f)

                    filtered_docs: Dict[str, DocumentRecord] = {}
                    for k, v in docs_data.items():
                        doc_rec = DocumentRecord(**v)
                        resolved_c = resolve_collection_identity(doc_rec.document_id, getattr(doc_rec, "collection_id", None))
                        if resolved_c == cid:
                            filtered_docs[k] = doc_rec

                    filtered_chunks: List[ChunkRecord] = []
                    for c in chunks_data:
                        chunk_rec = ChunkRecord(**c)
                        resolved_c = resolve_collection_identity(chunk_rec.document_id, getattr(chunk_rec, "collection_id", None))
                        if resolved_c == cid or chunk_rec.document_id in filtered_docs:
                            filtered_chunks.append(chunk_rec)

                    bm25 = BM25Index()
                    bm25.build_from_chunks(filtered_chunks, filtered_docs)

                    dense = None
                    if not self.is_lexical_only:
                        from app.retrieval.dense import DenseIndex
                        snap_dense = DenseIndex(embedder=self._dense_embedder, index_dir=snap_dir)
                        if snap_dense.load(filtered_chunks, filtered_docs):
                            dense = snap_dense
                        else:
                            snap_dense.build_from_chunks(filtered_chunks, filtered_docs)
                            dense = snap_dense

                    return SnapshotGeneration(
                        collection_id=cid,
                        snapshot_id=snap_dir.name,
                        bm25_index=bm25,
                        dense_index=dense,
                        chunks=filtered_chunks,
                        documents=filtered_docs,
                    )

        # Fallback to processed_dir (allowed only for sebi_cscrf baseline)
        if cid != "sebi_cscrf" or not self.processed_dir.exists():
            return SnapshotGeneration(
                collection_id=cid,
                snapshot_id=None,
                bm25_index=BM25Index(),
                dense_index=None,
                chunks=[],
                documents={},
            )

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

        bm25 = BM25Index()
        bm25.build_from_chunks(chunks_list, docs_map)

        dense = None
        if not self.is_lexical_only:
            from app.retrieval.dense import DenseIndex
            dense_idx = DenseIndex(embedder=self._dense_embedder, index_dir=self.indexes_dir)
            if dense_idx.load(chunks_list, docs_map):
                dense = dense_idx
            else:
                dense_idx.build_from_chunks(chunks_list, docs_map)
                dense = dense_idx

        return SnapshotGeneration(
            collection_id=cid,
            snapshot_id=None,
            bm25_index=bm25,
            dense_index=dense,
            chunks=chunks_list,
            documents=docs_map,
        )

    def get_active_generation(self, collection_id: str = "sebi_cscrf") -> SnapshotGeneration:
        """
        Retrieves the currently pinned active SnapshotGeneration for the specified collection.
        If no generation is active, initializes it under thread lock and returns it.
        Ensures dense indexing is loaded before returning a generation for dense-capable retrieval.
        """
        cid = self._validate_collection_id(collection_id)
        state = self._get_or_create_collection_state(cid)

        # Fast path without lock
        gen = state.active_generation
        if gen is not None and (self.is_lexical_only or state.is_dense_indexed):
            return gen

        with self._state_lock:
            state = self._get_or_create_collection_state(cid)
            if state.active_generation is not None and (self.is_lexical_only or state.is_dense_indexed):
                return state.active_generation

            if not state.is_indexed:
                self.load_and_index_corpus(collection_id=cid)
            if not self.is_lexical_only and not state.is_dense_indexed:
                self.load_and_index_dense(collection_id=cid)

            if state.active_generation is None:
                self._sync_state_generation(state)

            assert state.active_generation is not None
            return state.active_generation

    def reload_if_stale(self, collection_id: str = "sebi_cscrf") -> bool:
        """
        Checks if active_snapshot.json pointer changed for the collection; if so, atomically reloads the snapshot.
        Loads the new generation in isolation and performs an atomic pointer swap under self._state_lock.
        If reloading fails, the existing active generation is preserved intact without downtime or degradation.
        """
        cid = self._validate_collection_id(collection_id)
        state = self._get_or_create_collection_state(cid)
        current_active_id = state.active_generation.snapshot_id if state.active_generation else state.active_snapshot_id

        # 1. PostgreSQL repository check if collection is postgres-backed
        if self.repository_factory is not None and getattr(state, "is_postgres_backed", False):
            try:
                doc_repo = self._get_doc_repo()
                chunk_repo = self._get_chunk_repo()
                docs = doc_repo.list_documents(cid) if doc_repo else []
                chunks = chunk_repo.list_all_chunks(cid) if chunk_repo else []
                if len(docs) != len(state.documents) or len(chunks) != len(state.chunks):
                    logger.info(
                        f"Detected updated collection '{cid}' in PostgreSQL ({len(docs)} docs, {len(chunks)} chunks). Reloading..."
                    )
                    with self._state_lock:
                        docs_map = {d.document_id: d for d in docs}
                        state.documents = docs_map
                        state.chunks = chunks
                        bm25 = BM25Index()
                        if chunks:
                            bm25.build_from_chunks(chunks, docs_map)
                        state.bm25_index = bm25
                        if not self.is_lexical_only:
                            from app.retrieval.dense import PostgresDenseIndex
                            state.dense_index = PostgresDenseIndex(
                                chunk_repo=chunk_repo,
                                collection_id=cid,
                                embedder=self._dense_embedder,
                                chunks=chunks,
                                documents=docs_map,
                            )
                            state.is_dense_indexed = True
                        state.is_indexed = True
                        self._sync_state_generation(state)
                    return True
            except Exception as e:
                logger.warning(f"Error checking PostgreSQL collection update for '{cid}': {e}")

        latest_id = None
        # Remote provider check
        if not isinstance(self.storage_adapter, LocalStorageAdapter):
            try:
                remote_ptr = self.durable_store.get_remote_active_pointer(cid)
                if remote_ptr:
                    latest_id = remote_ptr.snapshot_id
            except Exception as e:
                logger.warning(f"Error checking remote active pointer for {cid}: {e}")

        # Check local partition pointer
        if latest_id is None:
            partition_dir = self.indexes_dir / cid
            pointer_file = partition_dir / "active_snapshot.json"
            if not pointer_file.exists() and cid == "sebi_cscrf":
                pointer_file = self.indexes_dir / "active_snapshot.json"

            if pointer_file.exists():
                try:
                    with open(pointer_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    latest_id = data.get("snapshot_id")
                except Exception as e:
                    logger.warning(f"Error reading local active pointer for {cid}: {e}")

        if current_active_id and latest_id and latest_id != current_active_id:
            try:
                new_gen = self._load_isolated_generation(collection_id=cid)
                if new_gen is not None and new_gen.snapshot_id == latest_id:
                    with self._state_lock:
                        state = self._get_or_create_collection_state(cid)
                        state.active_generation = new_gen
                        state.active_snapshot_id = new_gen.snapshot_id
                        state.documents = new_gen.documents
                        state.chunks = new_gen.chunks
                        state.bm25_index = new_gen.bm25_index
                        state.dense_index = new_gen.dense_index
                        state.is_indexed = True
                        state.is_dense_indexed = True
                    logger.info(f"Successfully reloaded generation for {cid} to snapshot {latest_id}")
                    return True
                else:
                    logger.warning(f"Isolated reload for {cid} yielded mismatch or None for snapshot {latest_id}")
            except Exception as e:
                logger.error(f"Failed to reload generation for {cid} to snapshot {latest_id}: {e}", exc_info=True)
                return False

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

        # Persistent PostgreSQL repository path
        if self.repository_factory is not None:
            doc_repo = self._get_doc_repo()
            chunk_repo = self._get_chunk_repo()
            try:
                docs = doc_repo.list_documents(cid) if doc_repo else []
                chunks = chunk_repo.list_all_chunks(cid) if chunk_repo else []
            except Exception as e:
                logger.error(f"PostgreSQL storage error loading collection '{cid}': {e}")
                # Fail closed: do not silently fall back to stale or missing disk files
                raise

            # If documents exist in PostgreSQL or collection has no snapshot on disk and is not sebi_cscrf
            snap_dir = self.get_active_snapshot_dir(collection_id=cid)
            if docs or (snap_dir is None and cid != "sebi_cscrf"):
                filtered_docs = {d.document_id: d for d in docs}
                filtered_chunks = list(chunks)

                state.documents = filtered_docs
                state.chunks = filtered_chunks
                state.active_snapshot_id = f"pg_{cid}"
                state.is_postgres_backed = True
                state.bm25_index = BM25Index()
                if state.chunks:
                    state.bm25_index.build_from_chunks(state.chunks, state.documents)
                state.is_indexed = True
                self._sync_state_generation(state)
                return len(state.documents), len(state.chunks)

        snap_dir = self.get_active_snapshot_dir(collection_id=cid)
        if snap_dir is not None:
            lock_path = snap_dir.parent / f".{snap_dir.name}.lock"
            with self.durable_store._acquire_process_lock(lock_path):
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
                        self._sync_state_generation(state)
                        return len(state.documents), len(state.chunks)
                    except Exception as e:
                        logger.warning(f"Failed loading snapshot data for collection '{cid}': {e}")
                        pass

        # Fallback to processed_dir (allowed only for sebi_cscrf baseline)
        if cid != "sebi_cscrf" or not self.processed_dir.exists():
            state.is_indexed = True
            state.documents = {}
            state.chunks = []
            self._sync_state_generation(state)
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
        self._sync_state_generation(state)
        return len(state.documents), len(state.chunks)

    def load_and_index_dense(self, auto_rebuild: bool = True, collection_id: str = "sebi_cscrf") -> bool:
        """
        Load persisted dense vector index for collection_id from active snapshot or indexes_dir.
        In lexical_only mode, dense indexing is safely skipped.
        Thread-safe under self._state_lock.
        """
        with self._state_lock:
            if self.is_lexical_only:
                state = self._get_or_create_collection_state(collection_id)
                state.is_dense_indexed = True
                self._sync_state_generation(state)
                return True

            cid = self._validate_collection_id(collection_id)
            state = self._get_or_create_collection_state(cid)

            if state.is_dense_indexed and state.dense_index is not None:
                return True

            if not state.is_indexed:
                self.load_and_index_corpus(collection_id=cid)

            # When PostgreSQL repository factory is active for this collection
            if self.repository_factory is not None and getattr(state, "is_postgres_backed", False):
                from app.retrieval.dense import PostgresDenseIndex
                chunk_repo = self._get_chunk_repo()
                state.dense_index = PostgresDenseIndex(
                    chunk_repo=chunk_repo,
                    collection_id=cid,
                    embedder=self._dense_embedder,
                    chunks=state.chunks,
                    documents=state.documents,
                )
                state.is_dense_indexed = True
                self._sync_state_generation(state)
                return True

            if not state.chunks:
                state.is_dense_indexed = True
                self._sync_state_generation(state)
                return True

            from app.retrieval.dense import DenseIndex

            snap_dir = self.get_active_snapshot_dir(collection_id=cid)
            if snap_dir is not None:
                lock_path = snap_dir.parent / f".{snap_dir.name}.lock"
                with self.durable_store._acquire_process_lock(lock_path):
                    if snap_dir.exists():
                        snap_dense = DenseIndex(embedder=self._dense_embedder, index_dir=snap_dir)
                        if snap_dense.load(state.chunks, state.documents):
                            state.dense_index = snap_dense
                            state.is_dense_indexed = True
                            self._sync_state_generation(state)
                            return True

            # Check collection partition or indexes_dir
            if state.dense_index is None:
                index_dir = self.indexes_dir / cid if cid != "sebi_cscrf" else self.indexes_dir
                state.dense_index = DenseIndex(embedder=self._dense_embedder, index_dir=index_dir)

            loaded = state.dense_index.load(state.chunks, state.documents)
            if loaded:
                state.is_dense_indexed = True
                self._sync_state_generation(state)
                return True

            if auto_rebuild:
                state.dense_index.build_from_chunks(state.chunks, state.documents)
                state.dense_index.save()
                state.is_dense_indexed = True
                self._sync_state_generation(state)
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
        gen = self.get_active_generation(collection_id=cid)
        return gen.search_bm25(
            query=query,
            top_k=top_k,
            filters=filters,
            preprocess_fn=self.preprocess_query,
        )

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
        gen = self.get_active_generation(collection_id=cid)
        return gen.search_dense(query=query, top_k=top_k, filters=filters)

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
        Pins a single immutable SnapshotGeneration reference for the entire hybrid retrieval operation.
        In lexical_only mode, executes pure lexical BM25 search directly.
        """
        if not query or not query.strip():
            return RetrievalResponse(query=query, total_results=0, results=[])

        cid = self._validate_collection_id(collection_id)
        self.reload_if_stale(collection_id=cid)

        # Pin the generation snapshot reference for the duration of this query
        generation = self.get_active_generation(collection_id=cid)

        # Lexical-only mode: dispatch directly to lexical BM25 search on pinned generation
        if self.is_lexical_only:
            return generation.search_bm25(
                query=query,
                top_k=top_k,
                filters=filters,
                preprocess_fn=self.preprocess_query,
            )

        # Retrieve candidate lists from component retrievers within collection using the pinned generation
        bm25_resp = generation.search_bm25(
            query=query,
            top_k=candidate_k,
            filters=filters,
            preprocess_fn=self.preprocess_query,
        )
        dense_resp = generation.search_dense(
            query=query,
            top_k=candidate_k,
            filters=filters,
        )

        # Fuse candidate lists using RRF
        return reciprocal_rank_fusion(
            ranked_lists=[bm25_resp.results, dense_resp.results],
            k=rrf_k,
            top_k=top_k,
            query=query,
        )
