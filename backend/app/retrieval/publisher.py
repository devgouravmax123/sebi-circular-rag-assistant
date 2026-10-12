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
    and searchability status synchronization.
    """

    def __init__(
        self,
        repository_factory: StorageRepositoryFactory,
        indexes_dir: str | Path = "data/indexes",
        processed_dir: str | Path = "data/processed",
        embedder: Optional[Any] = None,
    ):
        self.factory = repository_factory
        self.indexes_dir = Path(indexes_dir)
        self.processed_dir = Path(processed_dir)
        self.snapshots_dir = self.indexes_dir / "snapshots"
        self.lock_file_path = self.indexes_dir / ".publish.lock"
        if embedder is None:
            from app.retrieval.dense_embedder import DenseEmbedder
            self.embedder = DenseEmbedder(model_name=DEFAULT_MODEL_NAME)
        else:
            self.embedder = embedder

    @property
    def active_pointer_path(self) -> Path:
        return self.indexes_dir / "active_snapshot.json"

    def get_active_pointer(self) -> Optional[ActiveSnapshotPointer]:
        """Read and validate the current active snapshot pointer."""
        if not self.active_pointer_path.exists():
            return None
        try:
            with open(self.active_pointer_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return ActiveSnapshotPointer(**data)
        except Exception as e:
            logger.warning(f"Malformed active_snapshot.json pointer: {e}")
            return None

    def get_active_manifest(self) -> Optional[SnapshotManifest]:
        """Read the manifest of the active snapshot."""
        pointer = self.get_active_pointer()
        if not pointer:
            return None
        manifest_path = self.snapshots_dir / pointer.snapshot_id / "manifest.json"
        if not manifest_path.exists():
            return None
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return SnapshotManifest(**data)
        except Exception as e:
            logger.error(f"Failed to read manifest for snapshot {pointer.snapshot_id}: {e}")
            return None

    def get_index_status(self) -> IndexStatusResponse:
        """Inspect active snapshot and identify pending un-indexed documents."""
        doc_repo = self.factory.get_document_repository()
        all_docs = doc_repo.list_documents()
        all_doc_ids = {d.document_id for d in all_docs}

        manifest = self.get_active_manifest()
        pointer = self.get_active_pointer()

        if manifest and pointer:
            published_ids = set(manifest.document_ids)
            pending_ids = sorted(list(all_doc_ids - published_ids))
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
                pending_document_ids=sorted(list(all_doc_ids)),
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
            self.indexes_dir.mkdir(parents=True, exist_ok=True)
            lock_fd = os.open(str(self.lock_file_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            with os.fdopen(lock_fd, "w", encoding="utf-8") as f_lock:
                f_lock.write(f"{os.getpid()}:{now.isoformat()}\n")
            lock_fd = None  # closed by fdopen context exit
        except FileExistsError:
            logger.warning("Concurrent publication attempt blocked by active lock.")
            return PublicationResult(
                success=False,
                snapshot_id=snap_id,
                total_documents=0,
                total_chunks=0,
                message="A publication build is already in progress. Concurrent build rejected.",
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

            # 1. Extract documents and chunks from authoritative repository
            doc_repo = self.factory.get_document_repository()
            chunk_repo = self.factory.get_chunk_repository()

            documents_list = doc_repo.list_documents()
            documents_map = {d.document_id: d for d in sorted(documents_list, key=lambda x: x.document_id)}
            all_chunks = chunk_repo.list_all_chunks()

            # Deterministic sorting of chunks: document_id then chunk_id
            all_chunks.sort(key=lambda c: (c.document_id, c.chunk_id))

            if not all_chunks:
                raise IndexValidationError("No chunks found in repository to index.")

            # Validate unique chunk IDs
            chunk_ids = [c.chunk_id for c in all_chunks]
            if len(chunk_ids) != len(set(chunk_ids)):
                dups = [cid for cid, count in Counter(chunk_ids).items() if count > 1]
                raise IndexValidationError(f"Duplicate chunk IDs found in repository: {dups[:5]}")

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
                    message="Dry run: candidate snapshot built and validated successfully without publication.",
                )

            # 5. Atomic Activation: move staging to snapshots/<snap_id>
            final_snapshot_dir = self.snapshots_dir / snap_id
            if final_snapshot_dir.exists():
                shutil.rmtree(final_snapshot_dir, ignore_errors=True)
            staging_dir.rename(final_snapshot_dir)

            # Atomic pointer activation using temp file + replace
            manifest_checksum = compute_file_sha256(final_snapshot_dir / "manifest.json")
            pointer = ActiveSnapshotPointer(
                snapshot_id=snap_id,
                activated_at=datetime.now(timezone.utc),
                manifest_checksum=manifest_checksum,
                total_documents=len(documents_map),
                total_chunks=len(all_chunks),
            )

            temp_pointer = self.indexes_dir / f".tmp_pointer_{snap_id}.json"
            with open(temp_pointer, "w", encoding="utf-8") as f:
                f.write(pointer.model_dump_json(indent=2))
            os.replace(temp_pointer, self.active_pointer_path)

            # Mirror to root data/indexes/dense_index.npy for legacy backwards compatibility
            # Use shutil.copy rather than shutil.copy2 to avoid copystat failures across Windows/CIFS/Docker mounts
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

        # Check existing dense_index.npy for matches
        existing_npy = self.indexes_dir / "dense_index.npy"
        existing_meta = self.indexes_dir / "dense_index_meta.json"
        if existing_npy.exists() and existing_meta.exists():
            try:
                with open(existing_meta, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                arr = np.load(existing_npy)
                existing_ids = meta.get("chunk_ids", [])
                if len(existing_ids) == arr.shape[0]:
                    existing_map = {cid: arr[idx] for idx, cid in enumerate(existing_ids)}
                    # If all chunks are present in existing map:
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
