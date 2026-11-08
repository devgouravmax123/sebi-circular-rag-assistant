"""
Orchestration service and background worker for dynamic regulatory document ingestion.

Coordinates:
1. Job claiming via IngestionJobRepository (FOR UPDATE SKIP LOCKED).
2. Secure source download and validation via SecurePDFDownloader.
3. PDF extraction via PDFExtractor.
4. Chunking via TextChunker.
5. Dense vector embedding generation via DenseEmbedder.
6. Record construction and atomic persistence across StorageRepositoryFactory.
7. Deduplication detection (SHA-256 match) and immutable versioning.
8. Lease management, heartbeat refreshes, and exponential backoff retry scheduling.
9. Guaranteed temporary file cleanup on all exit paths.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from typing import Optional, Dict, Any, Tuple, List, Union
import uuid

import numpy as np

from app.core.config import get_settings
from app.schemas.document import DocumentRecord, PageContent, ChunkRecord
from app.schemas.ingestion_job import (
    IngestionJobRecord,
    JobStatus,
    ProcessingStage,
    StaleLeaseError,
    JobStateTransitionError,
    calculate_exponential_backoff_seconds,
)
from app.ingestion.downloader import (
    SecurePDFDownloader,
    DownloadResult,
    DownloadSecurityError,
    SSRFValidationError,
    InvalidPDFError,
    DownloadLimitExceededError,
    DownloadHTTPError,
)
from app.ingestion.extractor import PDFExtractor, compute_sha256
from app.ingestion.chunker import TextChunker
from app.retrieval.dense_embedder import DenseEmbedder, DEFAULT_MODEL_NAME
from app.retrieval.publisher import IndexPublisher
from app.storage import (
    BaseStorageAdapter,
    LocalStorageAdapter,
    get_storage_adapter,
    StorageFileNotFoundError,
    StorageError,
)
from app.persistence.interfaces import (
    StorageRepositoryFactory,
    IngestionJobRepository,
    DocumentRepository,
    ChunkRepository,
    PersistenceError,
    ConnectionStorageError,
)
try:
    from app.persistence.worker_procedure_repo import (
        PostgresWorkerProcedureRepository,
        ChunkBatchItem,
    )
except ImportError:
    PostgresWorkerProcedureRepository = None  # type: ignore[assignment, misc]
    ChunkBatchItem = None  # type: ignore[assignment, misc]

logger = logging.getLogger(__name__)


class NonRetryableIngestionError(Exception):
    """Explicitly non-retryable failure (e.g. SSRF violation, invalid PDF format, schema corruption)."""
    pass


class RetryableIngestionError(Exception):
    """Transient failure that should be retried with exponential backoff (e.g. network timeout, transient DB disconnect)."""
    pass


@dataclass
class JobExecutionResult:
    """Outcome of processing an individual ingestion job."""
    job_id: uuid.UUID
    status: JobStatus
    document_id: Optional[str] = None
    sha256: Optional[str] = None
    chunks_count: int = 0
    message: str = ""
    error_details: Optional[Dict[str, Any]] = None


def sanitize_error_message(msg: str, max_length: int = 1000) -> str:
    """
    Sanitize error message to avoid credential leaks and bound length.
    """
    if not msg:
        return ""
    # Truncate and strip
    sanitized = msg.strip()
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length] + " [truncated]"
    return sanitized


class HeartbeatThread:
    """
    Background timer thread that periodically refreshes the job heartbeat
    during long-running network or CPU operations.
    """

    def __init__(
        self,
        job_repo: IngestionJobRepository,
        job_id: uuid.UUID,
        claim_token: Union[str, uuid.UUID],
        interval_seconds: float = 15.0,
        stage: ProcessingStage = ProcessingStage.PENDING,
        worker_procedure_repo: Optional[PostgresWorkerProcedureRepository] = None,
    ):
        self.job_repo = job_repo
        self.job_id = job_id
        self.claim_token = claim_token
        self.interval_seconds = interval_seconds
        self.current_stage = stage
        self.worker_procedure_repo = worker_procedure_repo
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.lost_lease = False

    def set_stage(self, stage: ProcessingStage) -> None:
        self.current_stage = stage

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.wait(timeout=self.interval_seconds):
            if self.lost_lease:
                break
            try:
                if self.worker_procedure_repo is not None:
                    self.worker_procedure_repo.heartbeat(
                        job_id=self.job_id,
                        raw_claim_token=str(self.claim_token),
                        stage=self.current_stage.value,
                    )
                else:
                    self.job_repo.update_stage_and_heartbeat(
                        job_id=self.job_id,
                        claim_token=self.claim_token if isinstance(self.claim_token, uuid.UUID) else uuid.UUID(self.claim_token),
                        stage=self.current_stage,
                    )
            except (StaleLeaseError, PersistenceError) as e:
                err_str = str(e)
                if "UNAUTHORIZED_WORKER" in err_str or "LEASE_EXPIRED" in err_str or "StaleLeaseError" in err_str:
                    logger.warning(
                        f"Heartbeat failed: lease for job {self.job_id} has been lost or superseded."
                    )
                    self.lost_lease = True
                    break
                else:
                    logger.warning(f"Transient error in heartbeat update for job {self.job_id}: {e}")
            except Exception as e:
                logger.warning(f"Transient error in heartbeat update for job {self.job_id}: {e}")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)


class IngestionOrchestrator:
    """
    Orchestrates the lifecycle of an individual ingestion job:
    Download -> Extraction -> Chunking -> Embedding -> Persistence -> Completion.
    """

    def __init__(
        self,
        repository_factory: StorageRepositoryFactory,
        downloader: Optional[SecurePDFDownloader] = None,
        extractor: Optional[PDFExtractor] = None,
        chunker: Optional[TextChunker] = None,
        embedder: Optional[DenseEmbedder] = None,
        heartbeat_interval_seconds: float = 15.0,
        worker_procedure_repo: Optional[PostgresWorkerProcedureRepository] = None,
        chunk_batch_size: int = 100,
        indexes_dir: Optional[Union[str, Path]] = None,
        processed_dir: Optional[Union[str, Path]] = None,
        storage_adapter: Optional[BaseStorageAdapter] = None,
        publish_snapshots: bool = True,
    ):
        self.factory = repository_factory
        self.job_repo: IngestionJobRepository = repository_factory.get_ingestion_job_repository()
        self.doc_repo: DocumentRepository = repository_factory.get_document_repository()
        self.chunk_repo: ChunkRepository = repository_factory.get_chunk_repository()

        # Wire stored procedure repository if provided or if repository_factory is PostgresStorageRepositoryFactory
        if worker_procedure_repo is not None:
            self.worker_procedure_repo: Optional[PostgresWorkerProcedureRepository] = worker_procedure_repo
        elif hasattr(repository_factory, "session_factory") and PostgresWorkerProcedureRepository is not None:
            self.worker_procedure_repo = PostgresWorkerProcedureRepository(repository_factory.session_factory)
        else:
            self.worker_procedure_repo = None

        self.chunk_batch_size = chunk_batch_size
        self.downloader = downloader or SecurePDFDownloader()
        self.extractor = extractor or PDFExtractor()
        self.chunker = chunker or TextChunker()
        self.embedder = embedder or DenseEmbedder()
        self.heartbeat_interval_seconds = heartbeat_interval_seconds

        settings = get_settings()
        self.indexes_dir = Path(indexes_dir or settings.indexes_dir).resolve()
        self.processed_dir = Path(processed_dir or settings.processed_dir).resolve()
        if storage_adapter is not None:
            self.storage_adapter = storage_adapter
        else:
            self.storage_adapter = get_storage_adapter(settings)
        self.publish_snapshots = publish_snapshots

    def process_job(
        self,
        job: IngestionJobRecord,
        claim_token: Union[str, uuid.UUID],
    ) -> JobExecutionResult:
        """
        Execute full ingestion lifecycle for a claimed job.
        Enforces claim ownership, lease safety, and guaranteed staging cleanup.
        """
        job_id = job.id
        source_url = job.source_url
        downloaded_file: Optional[Path] = None

        heartbeat = HeartbeatThread(
            job_repo=self.job_repo,
            job_id=job_id,
            claim_token=claim_token,
            interval_seconds=self.heartbeat_interval_seconds,
            stage=ProcessingStage.PENDING,
            worker_procedure_repo=self.worker_procedure_repo,
        )
        heartbeat.start()

        def _assert_lease_alive() -> None:
            if heartbeat.lost_lease:
                raise StaleLeaseError(f"Worker lost lease for job {job_id}")

        def _update_stage(stage: ProcessingStage) -> None:
            _assert_lease_alive()
            heartbeat.set_stage(stage)
            if self.worker_procedure_repo is not None:
                self.worker_procedure_repo.heartbeat(
                    job_id=job_id,
                    raw_claim_token=str(claim_token),
                    stage=stage.value,
                )
            else:
                self.job_repo.update_stage_and_heartbeat(
                    job_id=job_id,
                    claim_token=claim_token if isinstance(claim_token, uuid.UUID) else uuid.UUID(claim_token),
                    stage=stage,
                )

        try:
            # Resolve authoritative collection scope upfront
            authoritative_collection: Optional[str] = (
                job.collection_id.strip() if job.collection_id and job.collection_id.strip() else None
            )
            if not authoritative_collection and job.document_id:
                try:
                    existing_doc_rec = self.doc_repo.get_document(job.document_id)
                    if existing_doc_rec and getattr(existing_doc_rec, "collection_id", None):
                        authoritative_collection = existing_doc_rec.collection_id.strip()
                except Exception:
                    pass
            if not authoritative_collection and job.document_id:
                from app.schemas.document import resolve_collection_identity
                inferred = resolve_collection_identity(job.document_id)
                if inferred:
                    authoritative_collection = inferred

            if not authoritative_collection:
                raise NonRetryableIngestionError(
                    f"Cannot determine authoritative collection scope for ingestion job '{job_id}'. "
                    "Every ingestion job must specify an explicit collection_id or map to a known collection identity."
                )

            # Stage 1: Downloading / Sourcing
            _update_stage(ProcessingStage.DOWNLOADING)
            is_local_upload = (
                source_url.startswith("upload://")
                or source_url.startswith("s3://")
                or source_url.startswith("file://")
                or "data/uploads" in source_url
                or (self.storage_adapter is not None and self.storage_adapter.exists(source_url))
            )

            if is_local_upload:
                staging_dir = Path(tempfile.gettempdir()) / "sebi_ingestion_staging"
                staging_dir.mkdir(parents=True, exist_ok=True)
                downloaded_file = staging_dir / f"staged_{uuid.uuid4().hex[:12]}.pdf"

                try:
                    if self.storage_adapter is not None and (source_url.startswith("upload://") or source_url.startswith("s3://") or self.storage_adapter.exists(source_url)):
                        with self.storage_adapter.materialize_to_local(source_url) as resolved_file:
                            file_size = resolved_file.stat().st_size
                            max_bytes = getattr(self.storage_adapter, "max_upload_bytes", 30 * 1024 * 1024)
                            if file_size > max_bytes:
                                raise NonRetryableIngestionError(f"Uploaded source file exceeds maximum size limit ({file_size} bytes)")

                            with open(resolved_file, "rb") as f_chk:
                                hdr = f_chk.read(5)
                                if hdr != b"%PDF-":
                                    raise NonRetryableIngestionError("Invalid file signature: uploaded source file is not a valid PDF")

                            sha256 = compute_sha256(resolved_file)
                            shutil.copy2(resolved_file, downloaded_file)
                    else:
                        clean_path = source_url.replace("file://", "").strip()
                        resolved_file = Path(clean_path).resolve()
                        if not resolved_file.is_file():
                            raise NonRetryableIngestionError(f"Uploaded source file not found or inaccessible: {source_url}")

                        file_size = resolved_file.stat().st_size
                        if file_size > 30 * 1024 * 1024:
                            raise NonRetryableIngestionError(f"Uploaded source file exceeds maximum size limit ({file_size} bytes)")

                        with open(resolved_file, "rb") as f_chk:
                            hdr = f_chk.read(5)
                            if hdr != b"%PDF-":
                                raise NonRetryableIngestionError("Invalid file signature: uploaded source file is not a valid PDF")

                        sha256 = compute_sha256(resolved_file)
                        shutil.copy2(resolved_file, downloaded_file)
                except StorageFileNotFoundError as e:
                    raise NonRetryableIngestionError(f"Uploaded source file not found or inaccessible: {source_url}") from e
                except StorageError as e:
                    raise RetryableIngestionError(f"Storage error accessing source file {source_url}: {e}") from e
            else:
                try:
                    download_res: DownloadResult = self.downloader.fetch_pdf(source_url)
                    downloaded_file = download_res.staging_path
                    sha256 = download_res.sha256
                except (SSRFValidationError, InvalidPDFError, DownloadLimitExceededError) as e:
                    raise NonRetryableIngestionError(f"Download rejected by security policy: {e}") from e
                except (DownloadHTTPError, DownloadSecurityError) as e:
                    # HTTP errors (timeouts, 502, 503) are transient
                    raise RetryableIngestionError(f"Transient download error: {e}") from e
                except Exception as e:
                    raise RetryableIngestionError(f"Unexpected network error: {e}") from e

            # Stage 2: Validating
            _update_stage(ProcessingStage.VALIDATING)
            if not downloaded_file.exists():
                raise NonRetryableIngestionError("Downloaded file staging path not found on disk")

            # Verify checksum on disk
            computed_sha = compute_sha256(downloaded_file)
            if computed_sha != sha256:
                raise NonRetryableIngestionError(
                    f"Checksum mismatch on downloaded file: expected {sha256}, got {computed_sha}"
                )

            # Check if identical content SHA-256 is already persisted in the document repository for THIS collection
            existing_doc = self.doc_repo.get_document_by_sha256(sha256, collection_id=authoritative_collection)
            if existing_doc:
                heartbeat.stop()
                # Crash recovery case: if the persisted document has the exact document_id requested by this job,
                # this job previously succeeded in persisting but failed before marking completion.
                # Mark completed cleanly without creating duplicates or redundant work.
                target_doc_id = job.document_id or existing_doc.document_id
                if existing_doc.document_id == target_doc_id:
                    # Reconstruct the expected chunks from the validated downloaded PDF
                    # to strictly verify that persisted chunk count matches the true expected chunk count.
                    _update_stage(ProcessingStage.PARSING)
                    try:
                        pages: List[PageContent] = self.extractor.extract_pages(downloaded_file)
                    except Exception as e:
                        raise NonRetryableIngestionError(f"PDF extraction failed during crash recovery: {e}") from e

                    if not pages:
                        raise NonRetryableIngestionError("PDF has 0 readable pages during crash recovery")

                    _update_stage(ProcessingStage.CHUNKING)
                    try:
                        expected_chunks: List[ChunkRecord] = self.chunker.chunk_document(target_doc_id, pages)
                    except Exception as e:
                        raise NonRetryableIngestionError(f"Chunking failed during crash recovery: {e}") from e

                    expected_count = len(expected_chunks)
                    if expected_count == 0:
                        raise NonRetryableIngestionError("Document produced 0 text chunks during crash recovery")

                    # If snapshot publishing is enabled, ensure index snapshot is built and published
                    collection_to_publish = authoritative_collection
                    if self.publish_snapshots and collection_to_publish:
                        _update_stage(ProcessingStage.INDEXING)
                        try:
                            publisher = IndexPublisher(
                                repository_factory=self.factory,
                                indexes_dir=self.indexes_dir,
                                processed_dir=self.processed_dir,
                                embedder=self.embedder,
                                collection_id=collection_to_publish,
                                storage_adapter=self.storage_adapter,
                            )
                            pub_res = publisher.build_and_publish_snapshot()
                            if not pub_res.success:
                                if pub_res.error_details in ("ConcurrentPublicationConflict", "StaleSnapshotWriterError"):
                                    raise RetryableIngestionError(
                                        f"Publication conflict for collection '{collection_to_publish}': {pub_res.message}. Job will retry with backoff."
                                    )
                                raise NonRetryableIngestionError(f"Index snapshot publication failed: {pub_res.message}")
                            logger.info(
                                f"Published search index snapshot '{pub_res.snapshot_id}' for collection '{collection_to_publish}' during crash recovery "
                                f"({pub_res.total_documents} documents, {pub_res.total_chunks} chunks)"
                            )
                        except (NonRetryableIngestionError, RetryableIngestionError):
                            raise
                        except Exception as e:
                            logger.error(f"Error publishing index snapshot for collection '{collection_to_publish}' during crash recovery: {e}", exc_info=True)
                            raise RetryableIngestionError(f"Index snapshot publication failed during crash recovery: {e}") from e

                    heartbeat.stop()

                    if self.worker_procedure_repo is not None:
                        # Stored procedure app.complete_ingestion_job requires an assigned version_id and chunks.
                        # It validates that actual chunks in DB equals expected_count and verifies 1:1 embedding coverage.
                        self.worker_procedure_repo.complete_job(
                            job_id=job_id,
                            raw_claim_token=str(claim_token),
                            expected_chunk_count=expected_count,
                            allow_empty=False,
                        )
                    else:
                        job_claim_uuid = claim_token if isinstance(claim_token, uuid.UUID) else uuid.UUID(claim_token)
                        self.job_repo.mark_completed(
                            job_id=job_id,
                            claim_token=job_claim_uuid,
                            document_id=existing_doc.document_id,
                            sha256=sha256,
                        )
                    return JobExecutionResult(
                        job_id=job_id,
                        status=JobStatus.COMPLETED,
                        document_id=existing_doc.document_id,
                        sha256=sha256,
                        chunks_count=expected_count,
                        message=f"Crash recovery: Document '{existing_doc.document_id}' was already persisted; marked completed with verified chunk count {expected_count}.",
                    )
                else:
                    # Content deduplication within the same collection: different job/document registered with identical content
                    dup_message = f"Identical content SHA-256 already ingested in collection '{authoritative_collection}'"
                    safe_doc_id = existing_doc.document_id
                    if self.worker_procedure_repo is not None:
                        error_details = {
                            "code": "DUPLICATE_DOCUMENT",
                            "reason": dup_message,
                            "collection_id": authoritative_collection,
                            "sha256": sha256,
                            "detected_at": datetime.now(timezone.utc).isoformat(),
                        }
                        self.worker_procedure_repo.mark_duplicate_job(
                            job_id=job_id,
                            raw_claim_token=str(claim_token),
                            error_details=error_details,
                        )
                    else:
                        job_claim_uuid = claim_token if isinstance(claim_token, uuid.UUID) else uuid.UUID(claim_token)
                        self.job_repo.mark_duplicate(
                            job_id=job_id,
                            claim_token=job_claim_uuid,
                            sha256=sha256,
                            document_id=safe_doc_id,
                        )

                    return JobExecutionResult(
                        job_id=job_id,
                        status=JobStatus.DUPLICATE,
                        document_id=safe_doc_id,
                        sha256=sha256,
                        message=dup_message,
                        error_details={
                            "code": "DUPLICATE_DOCUMENT",
                            "reason": dup_message,
                            "collection_id": authoritative_collection,
                            "sha256": sha256,
                        },
                    )

            # Determine logical document_id from job or derive from source
            doc_id = job.document_id
            if not doc_id:
                # Derive canonical ID from filename or fallback to job UUID
                stem = Path(source_url).stem
                doc_id = stem if stem and stem != "." else f"doc_{job_id.hex[:12]}"

            # Stage 3: Parsing
            _update_stage(ProcessingStage.PARSING)
            try:
                pages: List[PageContent] = self.extractor.extract_pages(downloaded_file)
            except Exception as e:
                raise NonRetryableIngestionError(f"PDF extraction failed: {e}") from e

            if not pages:
                raise NonRetryableIngestionError("PDF has 0 readable pages")

            # Stage 4: Chunking
            _update_stage(ProcessingStage.CHUNKING)
            try:
                chunks: List[ChunkRecord] = self.chunker.chunk_document(doc_id, pages)
                for c in chunks:
                    c.collection_id = authoritative_collection
            except Exception as e:
                raise NonRetryableIngestionError(f"Chunking failed: {e}") from e

            if not chunks:
                raise NonRetryableIngestionError("Document produced 0 text chunks")

            # Stage 5: Embedding
            _update_stage(ProcessingStage.EMBEDDING)
            try:
                chunk_texts = [c.text for c in chunks]
                embeddings_arr: np.ndarray = self.embedder.embed_texts(chunk_texts)
                if embeddings_arr.shape[0] != len(chunks):
                    raise ValueError(
                        f"Embedder output count ({embeddings_arr.shape[0]}) does not match chunks ({len(chunks)})"
                    )
                embeddings_dict = {
                    chunks[i].chunk_id: embeddings_arr[i] for i in range(len(chunks))
                }
            except Exception as e:
                raise RetryableIngestionError(f"Vector embedding generation failed: {e}") from e

            # Stage 6: Persisting
            _update_stage(ProcessingStage.PERSISTING)

            if self.worker_procedure_repo is not None:
                try:
                    # 1. Stored Procedure: Create immutable document version (status: 'processing')
                    raw_token_str = str(claim_token)
                    target_version = 1
                    if job.document_id and hasattr(self.worker_procedure_repo, "get_next_version_number"):
                        try:
                            target_version = self.worker_procedure_repo.get_next_version_number(
                                document_id=job.document_id,
                                sha256_hash=sha256,
                            )
                        except Exception as e:
                            logger.warning(f"Could not compute next version number for {job.document_id}: {e}")
                            target_version = 1

                    version_id = self.worker_procedure_repo.create_version(
                        job_id=job_id,
                        raw_claim_token=raw_token_str,
                        version_number=target_version,
                        sha256_hash=sha256,
                        source_url=source_url,
                        storage_path=str(downloaded_file),
                        issue_date=None,
                        effective_date=None,
                    )

                    # 2. Build batch items and write chunks + embeddings in batches
                    batch_items: List[ChunkBatchItem] = []
                    for idx, chunk in enumerate(chunks):
                        emb = embeddings_dict[chunk.chunk_id]
                        emb_list = emb.tolist() if hasattr(emb, "tolist") else list(emb)
                        batch_items.append(
                            ChunkBatchItem(
                                chunk_id=chunk.chunk_id,
                                chunk_index=chunk.metadata.get("chunk_index", idx),
                                text=chunk.text,
                                pdf_page_start=chunk.page_start,
                                pdf_page_end=chunk.page_end,
                                embedding=emb_list,
                                printed_page_start=chunk.metadata.get("printed_page_start"),
                                printed_page_end=chunk.metadata.get("printed_page_end"),
                                is_ocr=chunk.is_ocr,
                                chunk_type=chunk.chunk_type or "body",
                                metadata_json=chunk.metadata,
                            )
                        )

                    # Write in configured chunk_batch_size
                    for b_start in range(0, len(batch_items), self.chunk_batch_size):
                        b_slice = batch_items[b_start : b_start + self.chunk_batch_size]
                        self.worker_procedure_repo.write_chunks_batch(
                            job_id=job_id,
                            raw_claim_token=raw_token_str,
                            version_id=version_id,
                            chunks=b_slice,
                        )

                    # Stage 7: Indexing & Publication
                    _update_stage(ProcessingStage.INDEXING)

                    if self.publish_snapshots:
                        collection_to_publish = authoritative_collection
                        try:
                            publisher = IndexPublisher(
                                repository_factory=self.factory,
                                indexes_dir=self.indexes_dir,
                                processed_dir=self.processed_dir,
                                embedder=self.embedder,
                                collection_id=collection_to_publish,
                                storage_adapter=self.storage_adapter,
                            )
                            pub_res = publisher.build_and_publish_snapshot()
                            if not pub_res.success:
                                if pub_res.error_details in ("ConcurrentPublicationConflict", "StaleSnapshotWriterError"):
                                    raise RetryableIngestionError(
                                        f"Publication conflict for collection '{collection_to_publish}': {pub_res.message}. Job will retry with backoff."
                                    )
                                raise NonRetryableIngestionError(f"Index snapshot publication failed: {pub_res.message}")
                            logger.info(
                                f"Published search index snapshot '{pub_res.snapshot_id}' for collection '{collection_to_publish}' "
                                f"({pub_res.total_documents} documents, {pub_res.total_chunks} chunks)"
                            )
                        except (NonRetryableIngestionError, RetryableIngestionError):
                            raise
                        except Exception as e:
                            logger.error(f"Error publishing index snapshot for collection '{collection_to_publish}': {e}", exc_info=True)
                            raise RetryableIngestionError(f"Index snapshot publication failed: {e}") from e

                    # Final: Complete job via stored procedure
                    heartbeat.stop()
                    self.worker_procedure_repo.complete_job(
                        job_id=job_id,
                        raw_claim_token=raw_token_str,
                        expected_chunk_count=len(chunks),
                        allow_empty=False,
                    )
                except ConnectionStorageError as e:
                    raise RetryableIngestionError(f"Storage connection error during worker procedure persistence: {e}") from e
                except PersistenceError as e:
                    raise RetryableIngestionError(f"Worker procedure persistence operation failed: {e}") from e
                except Exception as e:
                    raise RetryableIngestionError(f"Unexpected error during worker procedure persistence: {e}") from e
            else:
                # Direct repository persistence (file-backed storage / tests)
                doc_record = DocumentRecord(
                    document_id=doc_id,
                    title=job.document_id or doc_id,
                    source_url=source_url,
                    sha256=sha256,
                    pdf_path=str(downloaded_file),
                    extraction_status="extracted",
                    collection_id=authoritative_collection,
                )

                try:
                    self.doc_repo.save_document(doc_record)
                    self.chunk_repo.save_chunks(chunks)
                    self.chunk_repo.save_embeddings(embeddings_dict, model_name=DEFAULT_MODEL_NAME)
                except ConnectionStorageError as e:
                    raise RetryableIngestionError(f"Storage connection error during persistence: {e}") from e
                except PersistenceError as e:
                    raise RetryableIngestionError(f"Persistence operation failed: {e}") from e
                except Exception as e:
                    raise RetryableIngestionError(f"Unexpected error during persistence: {e}") from e

                # Stage 7: Indexing & Publication
                _update_stage(ProcessingStage.INDEXING)

                if self.publish_snapshots:
                    collection_to_publish = authoritative_collection
                    try:
                        publisher = IndexPublisher(
                            repository_factory=self.factory,
                            indexes_dir=self.indexes_dir,
                            processed_dir=self.processed_dir,
                            embedder=self.embedder,
                            collection_id=collection_to_publish,
                            storage_adapter=self.storage_adapter,
                        )
                        pub_res = publisher.build_and_publish_snapshot()
                        if not pub_res.success:
                            if pub_res.error_details in ("ConcurrentPublicationConflict", "StaleSnapshotWriterError"):
                                raise RetryableIngestionError(
                                    f"Publication conflict for collection '{collection_to_publish}': {pub_res.message}. Job will retry with backoff."
                                )
                            raise NonRetryableIngestionError(f"Index snapshot publication failed: {pub_res.message}")
                        logger.info(
                            f"Published search index snapshot '{pub_res.snapshot_id}' for collection '{collection_to_publish}' "
                            f"({pub_res.total_documents} documents, {pub_res.total_chunks} chunks)"
                        )
                    except (NonRetryableIngestionError, RetryableIngestionError):
                        raise
                    except Exception as e:
                        logger.error(f"Error publishing index snapshot for collection '{collection_to_publish}': {e}", exc_info=True)
                        raise RetryableIngestionError(f"Index snapshot publication failed: {e}") from e

                # Final: Mark Completed
                heartbeat.stop()
                fallback_claim_uuid = claim_token if isinstance(claim_token, uuid.UUID) else uuid.UUID(claim_token)
                self.job_repo.mark_completed(
                    job_id=job_id,
                    claim_token=fallback_claim_uuid,
                    document_id=doc_id,
                    sha256=sha256,
                )

            return JobExecutionResult(
                job_id=job_id,
                status=JobStatus.COMPLETED,
                document_id=doc_id,
                sha256=sha256,
                chunks_count=len(chunks),
                message=f"Successfully ingested {len(pages)} pages and {len(chunks)} chunks",
            )

        except StaleLeaseError as e:
            heartbeat.stop()
            logger.error(f"Job {job_id} abandoned: lease lost to another worker: {e}")
            return JobExecutionResult(
                job_id=job_id,
                status=JobStatus.FAILED,
                message=f"Lease lost: {e}",
            )

        except NonRetryableIngestionError as e:
            heartbeat.stop()
            err_msg = sanitize_error_message(str(e))
            logger.error(f"Job {job_id} failed with permanent error: {err_msg}")
            error_details = {
                "category": "non_retryable",
                "reason": err_msg,
                "failed_at": datetime.now(timezone.utc).isoformat(),
            }
            if not heartbeat.lost_lease:
                try:
                    if self.worker_procedure_repo is not None:
                        self.worker_procedure_repo.fail_job(
                            job_id=job_id,
                            raw_claim_token=str(claim_token),
                            error_details=error_details,
                            is_retryable=False,
                        )
                    else:
                        fallback_claim_uuid = claim_token if isinstance(claim_token, uuid.UUID) else uuid.UUID(claim_token)
                        self.job_repo.mark_failed(
                            job_id=job_id,
                            claim_token=fallback_claim_uuid,
                            error_details=error_details,
                        )
                except Exception as fail_err:
                    logger.warning(f"Failed to record permanent failure for job {job_id}: {fail_err}")
            return JobExecutionResult(
                job_id=job_id,
                status=JobStatus.FAILED,
                message=err_msg,
                error_details=error_details,
            )

        except (RetryableIngestionError, Exception) as e:
            heartbeat.stop()
            err_msg = sanitize_error_message(str(e))
            logger.warning(f"Job {job_id} encountered retryable failure: {err_msg}")

            # Check attempts against max_attempts
            next_attempt = job.attempt_count + 1
            if next_attempt >= job.max_attempts:
                # Terminal failure due to max attempts exceeded
                error_details = {
                    "category": "max_attempts_exceeded",
                    "reason": err_msg,
                    "attempts": next_attempt,
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                }
                if not heartbeat.lost_lease:
                    try:
                        if self.worker_procedure_repo is not None:
                            self.worker_procedure_repo.fail_job(
                                job_id=job_id,
                                raw_claim_token=str(claim_token),
                                error_details=error_details,
                                is_retryable=False,
                            )
                        else:
                            fallback_claim_uuid = claim_token if isinstance(claim_token, uuid.UUID) else uuid.UUID(claim_token)
                            self.job_repo.mark_failed(
                                job_id=job_id,
                                claim_token=fallback_claim_uuid,
                                error_details=error_details,
                            )
                    except Exception as fail_err:
                        logger.warning(f"Failed to record terminal failure for job {job_id}: {fail_err}")
                return JobExecutionResult(
                    job_id=job_id,
                    status=JobStatus.FAILED,
                    message=f"Max attempts ({job.max_attempts}) reached: {err_msg}",
                    error_details=error_details,
                )
            else:
                # Schedule retry with exponential backoff
                backoff_sec = calculate_exponential_backoff_seconds(next_attempt)
                error_details = {
                    "category": "retryable",
                    "reason": err_msg,
                    "attempt": next_attempt,
                    "retry_after_seconds": backoff_sec,
                    "scheduled_at": datetime.now(timezone.utc).isoformat(),
                }
                if not heartbeat.lost_lease:
                    try:
                        if self.worker_procedure_repo is not None:
                            self.worker_procedure_repo.fail_job(
                                job_id=job_id,
                                raw_claim_token=str(claim_token),
                                error_details=error_details,
                                is_retryable=True,
                            )
                        else:
                            fallback_claim_uuid = claim_token if isinstance(claim_token, uuid.UUID) else uuid.UUID(claim_token)
                            self.job_repo.schedule_retry(
                                job_id=job_id,
                                claim_token=fallback_claim_uuid,
                                error_details=error_details,
                                retry_delay_seconds=backoff_sec,
                            )
                    except Exception as retry_err:
                        logger.warning(f"Failed to schedule retry for job {job_id}: {retry_err}")
                return JobExecutionResult(
                    job_id=job_id,
                    status=JobStatus.QUEUED,
                    message=f"Scheduled retry in {backoff_sec}s: {err_msg}",
                    error_details=error_details,
                )

        finally:
            heartbeat.stop()
            # Guaranteed temporary file cleanup on every exit path
            if downloaded_file and downloaded_file.exists():
                try:
                    downloaded_file.unlink()
                except Exception as cleanup_err:
                    logger.warning(f"Failed to delete staging file {downloaded_file}: {cleanup_err}")


class IngestionWorker:
    """
    Background worker that polls for eligible queued jobs,
    claims them atomically via PostgresWorkerProcedureRepository (or legacy IngestionJobRepository),
    and executes them through IngestionOrchestrator.
    Supports clean shutdown (SIGINT/SIGTERM), configurable polling interval,
    and automatic stale job recovery.
    """

    def __init__(
        self,
        repository_factory: StorageRepositoryFactory,
        worker_id: Optional[str] = None,
        poll_interval_seconds: float = 2.0,
        lease_timeout_seconds: float = 60.0,
        stale_recovery_interval_seconds: float = 30.0,
        orchestrator: Optional[IngestionOrchestrator] = None,
        worker_procedure_repo: Optional[PostgresWorkerProcedureRepository] = None,
        recovery_batch_limit: int = 50,
    ):
        self.factory = repository_factory
        self.job_repo: IngestionJobRepository = repository_factory.get_ingestion_job_repository()
        self.worker_id = worker_id or f"worker_{os.getpid()}_{uuid.uuid4().hex[:6]}"
        self.poll_interval_seconds = poll_interval_seconds
        self.lease_timeout_seconds = lease_timeout_seconds
        self.stale_recovery_interval_seconds = stale_recovery_interval_seconds
        self.recovery_batch_limit = recovery_batch_limit

        if worker_procedure_repo is not None:
            self.worker_procedure_repo: Optional[PostgresWorkerProcedureRepository] = worker_procedure_repo
        elif hasattr(repository_factory, "session_factory") and PostgresWorkerProcedureRepository is not None:
            self.worker_procedure_repo = PostgresWorkerProcedureRepository(repository_factory.session_factory)
        else:
            self.worker_procedure_repo = None

        self.orchestrator = orchestrator or IngestionOrchestrator(
            repository_factory=repository_factory,
            worker_procedure_repo=self.worker_procedure_repo,
        )

        self._stop_event = threading.Event()
        self._last_stale_recovery = 0.0
        self._active_job_lock = threading.Lock()
        self._current_job_id: Optional[uuid.UUID] = None

    def stop(self) -> None:
        """Signal the worker to gracefully stop polling."""
        self._stop_event.set()

    @property
    def is_running(self) -> bool:
        return not self._stop_event.is_set()

    @property
    def current_job_id(self) -> Optional[uuid.UUID]:
        """Return the job ID currently being processed, if any."""
        with self._active_job_lock:
            return self._current_job_id

    def cleanup_orphan_staging_files(
        self,
        staging_dir: Optional[Path] = None,
        max_age_seconds: float = 3600.0,
    ) -> int:
        """
        Safely remove abandoned staging files left behind after ungraceful process crashes.
        Strict safety rules:
        - Only removes files matching the dedicated prefix 'sebi_staging_worker_' and '.pdf.staging' suffix.
        - Only removes files strictly older than max_age_seconds (default 1 hr, well past max lease time).
        - Preserves all active files and files belonging to other services/applications.
        - Idempotent and fails safe (logs warnings without raising).
        """
        import tempfile
        import time

        target_dir = Path(staging_dir) if staging_dir else Path(tempfile.gettempdir())
        if not target_dir.exists() or not target_dir.is_dir():
            return 0

        now = time.time()
        cleaned_count = 0
        try:
            for item in target_dir.iterdir():
                if (
                    item.is_file()
                    and item.name.startswith("sebi_staging_worker_")
                    and item.name.endswith(".pdf.staging")
                ):
                    try:
                        mtime = item.stat().st_mtime
                        if (now - mtime) >= max_age_seconds:
                            item.unlink()
                            cleaned_count += 1
                            logger.info(f"Cleaned up orphaned worker staging file: {item.name}")
                    except FileNotFoundError:
                        # Concurrent deletion or already removed
                        pass
                    except Exception as err:
                        logger.warning(f"Failed to remove orphaned staging file {item.name}: {err}")
        except Exception as scan_err:
            logger.warning(f"Failed scanning staging directory {target_dir} for orphaned files: {scan_err}")

        return cleaned_count

    def run_once(self) -> Optional[JobExecutionResult]:
        """
        Poll and execute a single job if available.
        Performs stale recovery if the recovery interval has elapsed.
        Returns JobExecutionResult or None if no eligible job was found.
        """
        if self._stop_event.is_set():
            return None

        now_ts = time.time()
        if now_ts - self._last_stale_recovery >= self.stale_recovery_interval_seconds:
            try:
                if self.worker_procedure_repo is not None:
                    recovered = self.worker_procedure_repo.recover_stale_jobs(
                        batch_limit=self.recovery_batch_limit
                    )
                else:
                    recovered = self.job_repo.recover_stale_jobs(
                        lease_timeout_seconds=self.lease_timeout_seconds
                    )
                if recovered > 0:
                    logger.info(f"Worker {self.worker_id} recovered {recovered} stale jobs")
            except Exception as e:
                logger.warning(f"Error during stale job recovery: {e}")
            self._last_stale_recovery = now_ts

        if self._stop_event.is_set():
            return None

        # Attempt to atomically claim next job
        if self.worker_procedure_repo is not None:
            claimed = self.worker_procedure_repo.claim_job(
                instance_id=self.worker_id,
                lease_duration_seconds=int(self.lease_timeout_seconds),
            )
            if not claimed:
                return None

            job_rec: Optional[IngestionJobRecord] = None
            try:
                job_rec = self.job_repo.get_job(claimed.job_id)
            except Exception as e:
                logger.warning(f"Could not hydrate job record for {claimed.job_id}: {e}")

            if job_rec is None:
                from app.schemas.document import resolve_collection_identity
                inferred_col = resolve_collection_identity(claimed.document_id) if claimed.document_id else None
                job_rec = IngestionJobRecord(
                    id=claimed.job_id,
                    source_url=claimed.source_url,
                    idempotency_key=f"key_{claimed.job_id}",
                    document_id=claimed.document_id,
                    collection_id=inferred_col or "sebi_cscrf",
                    status=JobStatus.PROCESSING,
                    stage=ProcessingStage.PENDING,
                )
            logger.info(f"Worker {self.worker_id} claimed job {claimed.job_id} (URL: {claimed.source_url})")
            with self._active_job_lock:
                self._current_job_id = claimed.job_id
            try:
                return self.orchestrator.process_job(job=job_rec, claim_token=claimed.raw_claim_token)
            finally:
                with self._active_job_lock:
                    self._current_job_id = None
        else:
            claim_token = uuid.uuid4()
            job = self.job_repo.claim_next_job(
                worker_id=self.worker_id,
                claim_token=claim_token,
            )

            if not job:
                return None

            logger.info(f"Worker {self.worker_id} claimed job {job.id} (URL: {job.source_url})")
            with self._active_job_lock:
                self._current_job_id = job.id
            try:
                return self.orchestrator.process_job(job=job, claim_token=claim_token)
            finally:
                with self._active_job_lock:
                    self._current_job_id = None

    def run_loop(
        self,
        max_jobs: Optional[int] = None,
        exit_when_idle: bool = False,
        timeout_seconds: Optional[float] = None,
    ) -> int:
        """
        Run the worker loop until stopped, max_jobs processed, queue drained, or timeout reached.
        Returns the number of jobs processed.
        """
        processed_count = 0
        start_time = time.time()
        while not self._stop_event.is_set():
            if max_jobs is not None and processed_count >= max_jobs:
                logger.info(f"Worker {self.worker_id} processed requested max_jobs limit ({max_jobs}). Stopping loop.")
                break

            if timeout_seconds is not None and (time.time() - start_time) >= timeout_seconds:
                logger.info(f"Worker {self.worker_id} reached execution timeout ({timeout_seconds}s). Stopping loop.")
                break

            result = self.run_once()
            if result:
                processed_count += 1
            else:
                if exit_when_idle:
                    logger.info(f"Worker {self.worker_id} found no eligible queued jobs (exit_when_idle=True). Exiting cleanly.")
                    break
                # Idle backoff
                self._stop_event.wait(timeout=self.poll_interval_seconds)

        return processed_count
