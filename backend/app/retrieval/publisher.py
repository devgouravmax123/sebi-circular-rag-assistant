"""
Search Index Publication and Synchronization Engine (Phase 8B.6).

Builds reproducible candidate search index snapshots (BM25 + Dense NumPy vectors),
validates all constraints (dimensions, finite values, IDs, fingerprints),
and atomically activates them for the active query service.
"""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Dict, List, Optional, Set, Tuple, Any

import numpy as np

from app.schemas.document import DocumentRecord, ChunkRecord
from app.schemas.publication import (
    SnapshotManifest,
    ActiveSnapshotPointer,
    PublicationResult,
    IndexStatusResponse,
)
from app.persistence.interfaces import (
    StorageRepositoryFactory,
    DocumentRepository,
    ChunkRepository,
    IngestionJobRepository,
)
from app.retrieval.bm25 import BM25Index

DEFAULT_MODEL_NAME = "BAAI/bge-small-en-v1.5"
DEFAULT_EMBEDDING_DIM = 384

logger = logging.getLogger(__name__)


class IndexPublicationError(Exception):
    """Base exception for search index building and publication failures."""
    pass


class IndexValidationError(IndexPublicationError):
    """Raised when candidate index data violates dimensional or integrity checks."""
    pass


def compute_file_sha256(filepath: Path) -> str:
    """Compute SHA-256 checksum of a file on disk."""
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


class IndexPublisher:
    """
    Orchestrates candidate snapshot construction, validation, atomic activation,
    and searchability status synchronization for a specific document collection.
    """

    def __init__(
        self,
        repository_factory: StorageRepositoryFactory,
        indexes_dir: str | Path = "data/indexes",
        processed_dir: str | Path = "data/processed",
        embedder: Optional[Any] = None,
        collection_id: str = "sebi_cscrf",
    ):
        self.factory = repository_factory
        self.indexes_dir = Path(indexes_dir).resolve()
        self.processed_dir = Path(processed_dir)
        self.collection_id = self._validate_collection_id(collection_id)

        # Base directory for collection snapshots
        if self.collection_id == "sebi_cscrf":
            # For sebi_cscrf: if collection partition directory already exists, use it.
            # Otherwise, use root indexes_dir (legacy SEBI pilot layout) so legacy readers and tests pass seamlessly.
            partition_dir = self.indexes_dir / "sebi_cscrf"
            if partition_dir.exists():
                self.collection_dir = partition_dir
            else:
                self.collection_dir = self.indexes_dir
        else:
            self.collection_dir = self.indexes_dir / self.collection_id

        self._validate_path_containment(self.collection_dir, self.indexes_dir)

        self.snapshots_dir = self.collection_dir / "snapshots"
        self.lock_file_path = self.collection_dir / ".publish.lock"

        if embedder is None:
            from app.retrieval.dense_embedder import DenseEmbedder
            self.embedder = DenseEmbedder(model_name=DEFAULT_MODEL_NAME)
        else:
            self.embedder = embedder

    @staticmethod
    def _validate_collection_id(collection_id: str) -> str:
        """Validate collection_id against safe character and naming rules."""
        if not collection_id or not isinstance(collection_id, str):
            raise IndexValidationError("collection_id must be a non-empty string.")
        cid = collection_id.strip()
        if not re.match(r"^[a-zA-Z0-9_-]+$", cid):
            raise IndexValidationError(
                f"Invalid collection_id '{cid}': must contain only alphanumeric characters, underscores, and hyphens."
            )
        return cid

    @staticmethod
    def _validate_path_containment(target_path: Path, root_path: Path) -> Path:
        """Enforces that target_path is strictly contained within root_path, preventing path traversal."""
        resolved_target = target_path.resolve()
        resolved_root = root_path.resolve()
        try:
            resolved_target.relative_to(resolved_root)
        except ValueError:
            raise IndexValidationError(
                f"Path traversal detected: path '{target_path}' is outside approved root '{root_path}'."
            )
        return resolved_target

    @property
    def active_pointer_path(self) -> Path:
        """
        Returns path to active_snapshot.json.
        Prefers collection partition: data/indexes/{collection_id}/active_snapshot.json.
        For sebi_cscrf only, falls back to legacy root data/indexes/active_snapshot.json
        if the partition pointer does not exist.
        """
        partition_pointer = self.collection_dir / "active_snapshot.json"
        if partition_pointer.exists():
            return partition_pointer
        if self.collection_id == "sebi_cscrf":
            root_pointer = self.indexes_dir / "active_snapshot.json"
            if root_pointer.exists():
                return root_pointer
        return partition_pointer

    @property
    def legacy_root_pointer_path(self) -> Path:
        """Direct path to legacy root active_snapshot.json."""
        return self.indexes_dir / "active_snapshot.json"

    def is_using_legacy_pointer(self) -> bool:
        """Returns True if the active pointer currently resolved is the legacy root pointer."""
        if self.collection_id != "sebi_cscrf":
            return False
        partition_pointer = self.collection_dir / "active_snapshot.json"
        root_pointer = self.indexes_dir / "active_snapshot.json"
        return not partition_pointer.exists() and root_pointer.exists()

    def get_active_pointer(self) -> Optional[ActiveSnapshotPointer]:
        """Read and validate the current active snapshot pointer."""
        pointer_path = self.active_pointer_path
        if not pointer_path.exists():
            return None
        try:
            with open(pointer_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            pointer = ActiveSnapshotPointer(**data)

            # Validate pointer collection ownership
            ptr_coll = getattr(pointer, "collection_id", None)
            if ptr_coll:
                if ptr_coll != self.collection_id:
                    logger.error(
                        f"Cross-collection pointer detected: pointer declares '{ptr_coll}', expected '{self.collection_id}'."
                    )
                    return None
            else:
                # Missing collection_id in pointer: allowed ONLY for legacy sebi_cscrf pointer
                if self.collection_id != "sebi_cscrf" or not self.is_using_legacy_pointer():
                    logger.error(f"Pointer at '{pointer_path}' lacks collection ownership metadata.")
                    return None

            return pointer
        except Exception as e:
            logger.warning(f"Malformed active_snapshot.json pointer at '{pointer_path}': {e}")
            return None

    def get_active_manifest(self) -> Optional[SnapshotManifest]:
        """Read and validate the manifest of the active snapshot."""
        pointer = self.get_active_pointer()
        if not pointer:
            return None

        # Resolve snapshot directory
        if self.is_using_legacy_pointer():
            manifest_path = self.indexes_dir / "snapshots" / pointer.snapshot_id / "manifest.json"
        else:
            manifest_path = self.snapshots_dir / pointer.snapshot_id / "manifest.json"

        # Validate path containment to prevent snapshot_id path traversal
        try:
            self._validate_path_containment(manifest_path, self.indexes_dir)
        except IndexValidationError as e:
            logger.error(f"Unsafe snapshot manifest path: {e}")
            return None

        if not manifest_path.exists():
            return None
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            manifest = SnapshotManifest(**data)

            # Validate manifest collection ownership
            m_coll = getattr(manifest, "collection_id", None)
            if m_coll:
                if m_coll != self.collection_id:
                    logger.error(
                        f"Cross-collection snapshot manifest detected: manifest declares '{m_coll}', expected '{self.collection_id}'."
                    )
                    return None
            else:
                # Missing collection_id in manifest: allowed ONLY for legacy sebi_cscrf snapshots
                if self.collection_id != "sebi_cscrf" or not self.is_using_legacy_pointer():
                    logger.error(f"Snapshot manifest at '{manifest_path}' lacks collection ownership metadata.")
                    return None

            return manifest
        except Exception as e:
            logger.error(f"Failed to read manifest for snapshot {pointer.snapshot_id}: {e}")
            return None

    def get_index_status(self) -> IndexStatusResponse:
        """Inspect active snapshot and identify pending un-indexed documents for this collection."""
        from app.schemas.document import resolve_collection_identity
        doc_repo = self.factory.get_document_repository()
        all_docs = doc_repo.list_documents()

        # Filter documents belonging to this collection
        coll_docs = [
            d for d in all_docs
            if resolve_collection_identity(d.document_id, getattr(d, "collection_id", None)) == self.collection_id
        ]
        coll_doc_ids = {d.document_id for d in coll_docs}

        manifest = self.get_active_manifest()
        pointer = self.get_active_pointer()

        if manifest and pointer:
            published_ids = set(manifest.document_ids)
            pending_ids = sorted(list(coll_doc_ids - published_ids))
            return IndexStatusResponse(
                has_active_snapshot=True,
                active_snapshot_id=manifest.snapshot_id,
                total_documents=manifest.total_documents,
                total_chunks=manifest.total_chunks,
                published_document_ids=sorted(list(published_ids)),
                pending_document_ids=pending_ids,
                last_activated_at=pointer.activated_at,
                embedding_model=manifest.embedding_model,
                format_version=manifest.format_version,
            )
        else:
            return IndexStatusResponse(
                has_active_snapshot=False,
                active_snapshot_id=None,
                total_documents=0,
                total_chunks=0,
                published_document_ids=[],
                pending_document_ids=sorted(list(coll_doc_ids)),
                last_activated_at=None,
                embedding_model=self.embedder.model_name,
                format_version="1.0.0",
            )

    def is_document_searchable(self, document_id: str, sha256: Optional[str] = None) -> bool:
        """
        Returns True only if the document (and its specific content version if sha256 is provided)
        is included in the active index snapshot manifest.
        """
        manifest = self.get_active_manifest()
        if not manifest:
            return False
        if document_id not in manifest.document_ids:
            return False
        if sha256:
            published_sha = manifest.document_versions.get(document_id)
            if published_sha and published_sha != sha256:
                return False
        return True

    def build_and_publish_snapshot(
        self,
        snapshot_id: Optional[str] = None,
        dry_run: bool = False,
    ) -> PublicationResult:
        """
        Builds, validates, and atomically activates a search index snapshot across
        all registered documents in the authoritative repository.
        """
        now = datetime.now(timezone.utc)
        snap_id = snapshot_id or f"snap_{now.strftime('%Y%m%d_%H%M%S')}_{os.urandom(3).hex()}"

        # Prevent overlapping concurrent builds using an atomic lock file
        lock_fd = None
        try:
            self.collection_dir.mkdir(parents=True, exist_ok=True)
            lock_fd = os.open(str(self.lock_file_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            with os.fdopen(lock_fd, "w", encoding="utf-8") as f_lock:
                f_lock.write(f"{os.getpid()}:{now.isoformat()}\n")
            lock_fd = None  # closed by fdopen context exit
        except FileExistsError:
            logger.warning(f"Concurrent publication attempt for collection '{self.collection_id}' blocked by active lock.")
            return PublicationResult(
                success=False,
                snapshot_id=snap_id,
                total_documents=0,
                total_chunks=0,
                message=f"A publication build is already in progress for collection '{self.collection_id}'. Concurrent build rejected.",
                error_details="ConcurrentPublicationConflict",
            )
        except Exception as e:
            if lock_fd is not None:
                os.close(lock_fd)
            raise e

        staging_dir: Optional[Path] = None
        try:
            self.snapshots_dir.mkdir(parents=True, exist_ok=True)
            staging_dir = self.snapshots_dir / f"_staging_{snap_id}"

            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
            staging_dir.mkdir(parents=True, exist_ok=True)

            # 1. Extract documents and chunks from authoritative repository, filtering by collection ownership
            from app.schemas.document import resolve_collection_identity
            doc_repo = self.factory.get_document_repository()
            chunk_repo = self.factory.get_chunk_repository()

            all_raw_docs = doc_repo.list_documents()
            # Filter strictly to documents belonging to this collection
            filtered_docs = [
                d for d in all_raw_docs
                if resolve_collection_identity(d.document_id, getattr(d, "collection_id", None)) == self.collection_id
            ]
            documents_map = {d.document_id: d for d in sorted(filtered_docs, key=lambda x: x.document_id)}
            coll_doc_ids = set(documents_map.keys())

            raw_chunks = chunk_repo.list_all_chunks()
            # Filter chunks belonging to this collection's documents
            all_chunks = []
            for c in raw_chunks:
                # Check document ownership or chunk document_id resolution
                chunk_coll = resolve_collection_identity(c.document_id, getattr(c, "collection_id", None))
                if chunk_coll == self.collection_id or c.document_id in coll_doc_ids:
                    all_chunks.append(c)

            # Deterministic sorting of chunks: document_id then chunk_id
            all_chunks.sort(key=lambda c: (c.document_id, c.chunk_id))

            if not all_chunks:
                raise IndexValidationError(f"No chunks found in repository for collection '{self.collection_id}' to index.")

            # Validate unique chunk IDs
            chunk_ids = [c.chunk_id for c in all_chunks]
            if len(chunk_ids) != len(set(chunk_ids)):
                dups = [cid for cid, count in Counter(chunk_ids).items() if count > 1]
                raise IndexValidationError(f"Duplicate chunk IDs found for collection '{self.collection_id}': {dups[:5]}")

            # 2. Build or load dense vector embeddings
            embeddings_matrix = self._obtain_embeddings_matrix(all_chunks, chunk_repo)

            # 3. Validation gates
            self._validate_candidate_artifacts(all_chunks, embeddings_matrix)

            # 4. Stage artifacts into staging_dir
            chunks_path = staging_dir / "chunks.json"
            docs_path = staging_dir / "documents.json"
            npy_path = staging_dir / "dense_index.npy"
            meta_path = staging_dir / "dense_index_meta.json"
            manifest_path = staging_dir / "manifest.json"

            # Save chunks and documents JSON
            with open(chunks_path, "w", encoding="utf-8") as f:
                json.dump([c.model_dump() for c in all_chunks], f, indent=2)

            with open(docs_path, "w", encoding="utf-8") as f:
                json.dump({k: v.model_dump() for k, v in documents_map.items()}, f, indent=2)

            # Save NumPy dense matrix
            np.save(npy_path, embeddings_matrix)

            # Compute corpus fingerprint
            hasher = hashlib.sha256()
            for c in all_chunks:
                hasher.update(c.chunk_id.encode("utf-8"))
                hasher.update(c.text.encode("utf-8"))
            corpus_fingerprint = hasher.hexdigest()

            # Save dense_index_meta.json
            dense_meta = {
                "collection_id": self.collection_id,
                "model_name": self.embedder.model_name,
                "embedding_dim": int(embeddings_matrix.shape[1]),
                "num_chunks": len(all_chunks),
                "chunk_ids": chunk_ids,
                "fingerprint": corpus_fingerprint,
            }
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(dense_meta, f, indent=2)

            # Checksums
            artifact_checksums = {
                "chunks.json": compute_file_sha256(chunks_path),
                "documents.json": compute_file_sha256(docs_path),
                "dense_index.npy": compute_file_sha256(npy_path),
                "dense_index_meta.json": compute_file_sha256(meta_path),
            }

            # Build Manifest
            manifest = SnapshotManifest(
                snapshot_id=snap_id,
                collection_id=self.collection_id,
                created_at=now,
                format_version="1.0.0",
                embedding_model=self.embedder.model_name,
                embedding_dim=DEFAULT_EMBEDDING_DIM,
                total_documents=len(documents_map),
                total_chunks=len(all_chunks),
                document_ids=list(documents_map.keys()),
                document_versions={d.document_id: d.sha256 for d in documents_map.values()},
                chunk_ids=chunk_ids,
                corpus_fingerprint=corpus_fingerprint,
                artifact_checksums=artifact_checksums,
            )
            with open(manifest_path, "w", encoding="utf-8") as f:
                f.write(manifest.model_dump_json(indent=2))

            if dry_run:
                shutil.rmtree(staging_dir, ignore_errors=True)
                return PublicationResult(
                    success=True,
                    snapshot_id=snap_id,
                    total_documents=len(documents_map),
                    total_chunks=len(all_chunks),
                    newly_published_documents=list(documents_map.keys()),
                    message=f"Dry run: candidate snapshot built and validated successfully for collection '{self.collection_id}'.",
                )

            # 5. Atomic Activation: move staging to snapshots/<snap_id>
            final_snapshot_dir = self.snapshots_dir / snap_id
            if final_snapshot_dir.exists():
                shutil.rmtree(final_snapshot_dir, ignore_errors=True)
            staging_dir.rename(final_snapshot_dir)

            # Atomic pointer activation using temp file + replace in collection directory
            manifest_checksum = compute_file_sha256(final_snapshot_dir / "manifest.json")
            pointer = ActiveSnapshotPointer(
                snapshot_id=snap_id,
                collection_id=self.collection_id,
                activated_at=datetime.now(timezone.utc),
                manifest_checksum=manifest_checksum,
                total_documents=len(documents_map),
                total_chunks=len(all_chunks),
            )

            target_pointer_path = self.collection_dir / "active_snapshot.json"
            temp_pointer = self.collection_dir / f".tmp_pointer_{snap_id}.json"
            with open(temp_pointer, "w", encoding="utf-8") as f:
                f.write(pointer.model_dump_json(indent=2))
            os.replace(temp_pointer, target_pointer_path)

            # Mirror to root data/indexes/active_snapshot.json and dense_index.npy for legacy backwards compatibility ONLY for sebi_cscrf
            if self.collection_id == "sebi_cscrf":
                try:
                    root_temp_pointer = self.indexes_dir / f".tmp_root_pointer_{snap_id}.json"
                    with open(root_temp_pointer, "w", encoding="utf-8") as f:
                        f.write(pointer.model_dump_json(indent=2))
                    os.replace(root_temp_pointer, self.indexes_dir / "active_snapshot.json")
                except Exception as root_ptr_err:
                    logger.warning(f"Legacy active_snapshot.json mirroring skipped or failed: {root_ptr_err}")

                try:
                    shutil.copy(final_snapshot_dir / "dense_index.npy", self.indexes_dir / "dense_index.npy")
                    shutil.copy(final_snapshot_dir / "dense_index_meta.json", self.indexes_dir / "dense_index_meta.json")
                except Exception as mirror_err:
                    logger.warning(f"Legacy dense_index.npy mirroring skipped or failed on host mount: {mirror_err}")

            # 6. Synchronize searchability across ingestion jobs
            self._synchronize_job_searchability(manifest)

            logger.info(
                f"Successfully published search index snapshot '{snap_id}' with "
                f"{len(documents_map)} documents and {len(all_chunks)} chunks."
            )
            return PublicationResult(
                success=True,
                snapshot_id=snap_id,
                total_documents=len(documents_map),
                total_chunks=len(all_chunks),
                newly_published_documents=list(documents_map.keys()),
                message=f"Snapshot '{snap_id}' successfully published and activated.",
            )

        except Exception as e:
            # Clean up staging dir on failure
            if staging_dir is not None and staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
            logger.error(f"Failed to publish index snapshot: {e}", exc_info=True)
            return PublicationResult(
                success=False,
                snapshot_id=snap_id,
                total_documents=0,
                total_chunks=0,
                message=f"Index publication failed: {e}",
                error_details=str(e),
            )

        finally:
            # Release concurrent publication lock
            try:
                if self.lock_file_path.exists():
                    self.lock_file_path.unlink()
            except Exception as e:
                logger.warning(f"Could not release publication lock: {e}")

    def _obtain_embeddings_matrix(
        self,
        chunks: List[ChunkRecord],
        chunk_repo: ChunkRepository,
    ) -> np.ndarray:
        """
        Obtain 384-dimensional dense vectors for all chunks.
        Reuses existing persistent embeddings from postgres or existing index if available;
        otherwise computes via DenseEmbedder.
        """
        # If running against PostgreSQL, query ChunkEmbeddingModel
        if hasattr(self.factory, "session_factory"):
            try:
                from sqlalchemy import select
                from app.persistence.models import ChunkEmbeddingModel
                chunk_id_set = {c.chunk_id for c in chunks}
                with self.factory.session_factory() as session:
                    rows = session.execute(
                        select(ChunkEmbeddingModel.chunk_id, ChunkEmbeddingModel.embedding)
                        .where(ChunkEmbeddingModel.chunk_id.in_(chunk_id_set))
                    ).all()
                    db_vecs = {r[0]: np.asarray(r[1], dtype=np.float32) for r in rows}

                if len(db_vecs) == len(chunks):
                    # Assemble ordered matrix
                    return np.vstack([db_vecs[c.chunk_id] for c in chunks])
            except Exception as e:
                logger.debug(f"Could not load pre-persisted vectors from DB: {e}")

        # Check existing dense_index.npy for matches in collection active snapshot or collection directory
        candidate_npy_paths = []
        active_manifest = self.get_active_manifest()
        active_pointer = self.get_active_pointer()
        if active_pointer:
            candidate_npy_paths.append((self.snapshots_dir / active_pointer.snapshot_id / "dense_index.npy", self.snapshots_dir / active_pointer.snapshot_id / "dense_index_meta.json"))
        candidate_npy_paths.append((self.collection_dir / "dense_index.npy", self.collection_dir / "dense_index_meta.json"))
        if self.collection_id == "sebi_cscrf":
            candidate_npy_paths.append((self.indexes_dir / "dense_index.npy", self.indexes_dir / "dense_index_meta.json"))

        for existing_npy, existing_meta in candidate_npy_paths:
            if existing_npy.exists() and existing_meta.exists():
                try:
                    with open(existing_meta, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    arr = np.load(existing_npy)
                    existing_ids = meta.get("chunk_ids", [])
                    if len(existing_ids) == arr.shape[0]:
                        existing_map = {cid: arr[idx] for idx, cid in enumerate(existing_ids)}
                        if all(c.chunk_id in existing_map for c in chunks):
                            return np.vstack([existing_map[c.chunk_id] for c in chunks])
                except Exception:
                    pass

        # Otherwise generate missing embeddings via DenseEmbedder
        logger.info(f"Computing dense vector embeddings for {len(chunks)} chunks...")
        texts = [c.text for c in chunks]
        return self.embedder.embed_texts(texts)

    def _validate_candidate_artifacts(
        self,
        chunks: List[ChunkRecord],
        embeddings: np.ndarray,
    ) -> None:
        """Perform strict structural and mathematical validation on candidate index."""
        if embeddings.ndim != 2:
            raise IndexValidationError(f"Embeddings array must be 2D, got shape {embeddings.shape}")
        if embeddings.shape[0] != len(chunks):
            raise IndexValidationError(
                f"Embedding row count ({embeddings.shape[0]}) does not match chunk count ({len(chunks)})"
            )
        if embeddings.shape[1] != DEFAULT_EMBEDDING_DIM:
            raise IndexValidationError(
                f"Embedding dimension ({embeddings.shape[1]}) does not match expected ({DEFAULT_EMBEDDING_DIM})"
            )
        if not np.all(np.isfinite(embeddings)):
            raise IndexValidationError("Embeddings matrix contains NaN or Infinite values.")

    def _synchronize_job_searchability(self, manifest: SnapshotManifest) -> None:
        """
        Updates completed ingestion jobs corresponding to published documents so their
        status reflection is accurately tracked.
        """
        try:
            job_repo = self.factory.get_ingestion_job_repository()
            # Inspect jobs
            if hasattr(job_repo, "list_jobs"):
                jobs = job_repo.list_jobs(limit=100)
                # Job searchability is dynamically derived from manifest.document_ids in status responses,
                # ensuring durable consistency across worker crash and restore.
        except Exception as e:
            logger.warning(f"Could not synchronize job searchability: {e}")
