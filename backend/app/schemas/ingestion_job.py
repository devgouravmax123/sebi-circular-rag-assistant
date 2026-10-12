"""
Data models and enumerations for the dynamic ingestion job state machine.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Dict, Any
import uuid


class JobStatus(str, Enum):
    """Primary lifecycle status for an ingestion job."""
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    DUPLICATE = "duplicate"
    FAILED = "failed"


class ProcessingStage(str, Enum):
    """Sub-stage representing the exact processing phase when status is 'processing'."""
    PENDING = "pending"
    DOWNLOADING = "downloading"
    VALIDATING = "validating"
    PARSING = "parsing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    PERSISTING = "persisting"
    INDEXING = "indexing"
    FINISHED = "finished"


class JobStateTransitionError(ValueError):
    """Raised when an invalid job state transition is attempted."""
    pass


class StaleLeaseError(RuntimeError):
    """Raised when a worker attempts an operation using an expired or superseded claim lease."""
    pass


@dataclass
class IngestionJobRecord:
    """Domain record representing an ingestion job."""
    id: uuid.UUID
    source_url: str
    idempotency_key: str
    status: JobStatus = JobStatus.QUEUED
    stage: ProcessingStage = ProcessingStage.PENDING
    document_id: Optional[str] = None
    sha256: Optional[str] = None
    attempt_count: int = 0
    max_attempts: int = 3
    claim_worker_id: Optional[str] = None
    claim_token: Optional[uuid.UUID] = None
    heartbeat_at: Optional[datetime] = None
    next_retry_at: Optional[datetime] = None
    error_details: Optional[Dict[str, Any]] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    @property
    def is_terminal(self) -> bool:
        """Returns True if the job has reached a terminal status."""
        return self.status in (JobStatus.COMPLETED, JobStatus.DUPLICATE, JobStatus.FAILED)


# Strict transition policy mapping: from_status -> allowed_to_statuses
LEGAL_STATUS_TRANSITIONS: Dict[JobStatus, set[JobStatus]] = {
    JobStatus.QUEUED: {JobStatus.PROCESSING},
    JobStatus.PROCESSING: {
        JobStatus.COMPLETED,
        JobStatus.DUPLICATE,
        JobStatus.FAILED,
        JobStatus.QUEUED,  # Retrying after transient error or stale recovery
    },
    # Terminal statuses allow no outward transitions
    JobStatus.COMPLETED: set(),
    JobStatus.DUPLICATE: set(),
    JobStatus.FAILED: set(),
}


def validate_status_transition(current_status: JobStatus, target_status: JobStatus) -> None:
    """Validate that transition from current_status to target_status is permitted."""
    if current_status == target_status:
        return
    allowed = LEGAL_STATUS_TRANSITIONS.get(current_status, set())
    if target_status not in allowed:
        raise JobStateTransitionError(
            f"Illegal job status transition from '{current_status.value}' to '{target_status.value}'. "
            f"Allowed transitions from '{current_status.value}': {[s.value for s in allowed]}."
        )


def calculate_exponential_backoff_seconds(
    attempt_count: int,
    base_seconds: float = 10.0,
    max_backoff_seconds: float = 300.0,
) -> float:
    """
    Calculate deterministic exponential backoff delay in seconds:
    delay = min(base_seconds * (2 ** (attempt_count - 1)), max_backoff_seconds).
    For attempt 1 -> 10s, attempt 2 -> 20s, attempt 3 -> 40s.
    """
    if attempt_count <= 0:
        return base_seconds
    backoff = base_seconds * (2 ** (attempt_count - 1))
    return min(backoff, max_backoff_seconds)
