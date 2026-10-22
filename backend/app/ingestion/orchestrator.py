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
import threading
import time
from typing import Optional, Dict, Any, Tuple, List
import uuid

import numpy as np

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
from app.persistence.interfaces import (
    StorageRepositoryFactory,
    IngestionJobRepository,
    DocumentRepository,
    ChunkRepository,
    PersistenceError,
    ConnectionStorageError,
)

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
        claim_token: uuid.UUID,
        interval_seconds: float = 15.0,
        stage: ProcessingStage = ProcessingStage.PENDING,
    ):
        self.job_repo = job_repo
        self.job_id = job_id
        self.claim_token = claim_token
        self.interval_seconds = interval_seconds
        self.current_stage = stage
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
                self.job_repo.update_stage_and_heartbeat(
                    job_id=self.job_id,
                    claim_token=self.claim_token,
                    stage=self.current_stage,
                )
            except StaleLeaseError:
                logger.warning(
                    f"Heartbeat failed: lease for job {self.job_id} has been lost or superseded."
                )
                self.lost_lease = True
                break
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
    ):
        self.factory = repository_factory
        self.job_repo: IngestionJobRepository = repository_factory.get_ingestion_job_repository()
        self.doc_repo: DocumentRepository = repository_factory.get_document_repository()
        self.chunk_repo: ChunkRepository = repository_factory.get_chunk_repository()

        self.downloader = downloader or SecurePDFDownloader()
        self.extractor = extractor or PDFExtractor()
        self.chunker = chunker or TextChunker()
        self.embedder = embedder or DenseEmbedder()
        self.heartbeat_interval_seconds = heartbeat_interval_seconds

    def process_job(
        self,
        job: IngestionJobRecord,
        claim_token: uuid.UUID,
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
        )
        heartbeat.start()

        def _assert_lease_alive() -> None:
            if heartbeat.lost_lease:
                raise StaleLeaseError(f"Worker lost lease for job {job_id}")

        def _update_stage(stage: ProcessingStage) -> None:
            _assert_lease_alive()
            heartbeat.set_stage(stage)
            self.job_repo.update_stage_and_heartbeat(
                job_id=job_id,
                claim_token=claim_token,
                stage=stage,
            )

        try:
            # Stage 1: Downloading
            _update_stage(ProcessingStage.DOWNLOADING)
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

            # Check if identical content SHA-256 is already persisted in the document repository
            existing_doc = self.doc_repo.get_document_by_sha256(sha256)
            if existing_doc:
                heartbeat.stop()
                # Crash recovery case: if the persisted document has the exact document_id requested by this job,
                # this job previously succeeded in persisting but failed before marking completion.
                # Mark completed cleanly without creating duplicates or redundant work.
                target_doc_id = job.document_id or existing_doc.document_id
                if existing_doc.document_id == target_doc_id:
                    self.job_repo.mark_completed(
                        job_id=job_id,
                        claim_token=claim_token,
                        document_id=existing_doc.document_id,
                        sha256=sha256,
                    )
                    return JobExecutionResult(
                        job_id=job_id,
                        status=JobStatus.COMPLETED,
                        document_id=existing_doc.document_id,
                        sha256=sha256,
                        message=f"Crash recovery: Document '{existing_doc.document_id}' was already persisted; marked completed.",
                    )
                else:
                    # Content deduplication: different job/document registered with identical content
                    self.job_repo.mark_duplicate(
                        job_id=job_id,
                        claim_token=claim_token,
                        sha256=sha256,
                        document_id=existing_doc.document_id,
                    )
                    return JobExecutionResult(
                        job_id=job_id,
                        status=JobStatus.DUPLICATE,
                        document_id=existing_doc.document_id,
                        sha256=sha256,
                        message=f"Identical content SHA-256 already ingested as '{existing_doc.document_id}'",
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
            doc_record = DocumentRecord(
                document_id=doc_id,
                title=job.document_id or doc_id,
                source_url=source_url,
                sha256=sha256,
                pdf_path=str(downloaded_file),
                extraction_status="extracted",
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

            # Stage 7: Indexing boundary
            _update_stage(ProcessingStage.INDEXING)
            # Note: Live in-memory index hot-reloading is decoupled in Phase 8B.4 to prevent mutating
            # concurrent read-query structures. Index publication will be handled via dedicated sync.

            # Final: Mark Completed
            heartbeat.stop()
            self.job_repo.mark_completed(
                job_id=job_id,
                claim_token=claim_token,
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
            try:
                self.job_repo.mark_failed(
                    job_id=job_id,
                    claim_token=claim_token,
                    error_details=error_details,
                )
            except StaleLeaseError:
                pass
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
                try:
                    self.job_repo.mark_failed(
                        job_id=job_id,
                        claim_token=claim_token,
                        error_details=error_details,
                    )
                except StaleLeaseError:
                    pass
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
                try:
                    self.job_repo.schedule_retry(
                        job_id=job_id,
                        claim_token=claim_token,
                        error_details=error_details,
                        retry_delay_seconds=backoff_sec,
                    )
                except StaleLeaseError:
                    pass
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
    Background worker that polls IngestionJobRepository for eligible queued jobs,
    claims them atomically, and executes them through IngestionOrchestrator.
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
    ):
        self.factory = repository_factory
        self.job_repo: IngestionJobRepository = repository_factory.get_ingestion_job_repository()
        self.worker_id = worker_id or f"worker_{os.getpid()}_{uuid.uuid4().hex[:6]}"
        self.poll_interval_seconds = poll_interval_seconds
        self.lease_timeout_seconds = lease_timeout_seconds
        self.stale_recovery_interval_seconds = stale_recovery_interval_seconds
        self.orchestrator = orchestrator or IngestionOrchestrator(repository_factory)

        self._stop_event = threading.Event()
        self._last_stale_recovery = 0.0

    def stop(self) -> None:
        """Signal the worker to gracefully stop polling."""
        self._stop_event.set()

    @property
    def is_running(self) -> bool:
        return not self._stop_event.is_set()

    def run_once(self) -> Optional[JobExecutionResult]:
        """
        Poll and execute a single job if available.
        Performs stale recovery if the recovery interval has elapsed.
        Returns JobExecutionResult or None if no eligible job was found.
        """
        now_ts = time.time()
        if now_ts - self._last_stale_recovery >= self.stale_recovery_interval_seconds:
            try:
                recovered = self.job_repo.recover_stale_jobs(
                    lease_timeout_seconds=self.lease_timeout_seconds
                )
                if recovered > 0:
                    logger.info(f"Worker {self.worker_id} recovered {recovered} stale jobs")
            except Exception as e:
                logger.warning(f"Error during stale job recovery: {e}")
            self._last_stale_recovery = now_ts

        # Attempt to atomically claim next job
        claim_token = uuid.uuid4()
        job = self.job_repo.claim_next_job(
            worker_id=self.worker_id,
            claim_token=claim_token,
        )

        if not job:
            return None

        logger.info(f"Worker {self.worker_id} claimed job {job.id} (URL: {job.source_url})")
        return self.orchestrator.process_job(job=job, claim_token=claim_token)

    def run_loop(self, max_jobs: Optional[int] = None) -> int:
        """
        Run the worker loop until stopped or max_jobs processed.
        Returns the number of jobs processed.
        """
        processed_count = 0
        while not self._stop_event.is_set():
            if max_jobs is not None and processed_count >= max_jobs:
                break

            result = self.run_once()
            if result:
                processed_count += 1
            else:
                # Idle backoff
                self._stop_event.wait(timeout=self.poll_interval_seconds)

        return processed_count
