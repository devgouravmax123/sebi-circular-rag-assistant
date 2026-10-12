"""
Focused tests for IngestionJobRepository, state machine transitions,
exponential backoff, lease management, and file-backed implementation.
Executes completely offline without external services.
"""
from datetime import datetime, timezone, timedelta
import tempfile
from pathlib import Path
import uuid
import pytest

from app.schemas.ingestion_job import (
    IngestionJobRecord,
    JobStatus,
    ProcessingStage,
    JobStateTransitionError,
    StaleLeaseError,
    validate_status_transition,
    calculate_exponential_backoff_seconds,
)
from app.persistence.file_repo import FileIngestionJobRepository


class TestJobStateMachineAndTransitions:
    """Tests for transition validity, terminal states, and backoff calculations."""

    def test_valid_transitions(self):
        # Queued -> Processing
        validate_status_transition(JobStatus.QUEUED, JobStatus.PROCESSING)

        # Processing -> Completed / Duplicate / Failed / Queued (retry)
        validate_status_transition(JobStatus.PROCESSING, JobStatus.COMPLETED)
        validate_status_transition(JobStatus.PROCESSING, JobStatus.DUPLICATE)
        validate_status_transition(JobStatus.PROCESSING, JobStatus.FAILED)
        validate_status_transition(JobStatus.PROCESSING, JobStatus.QUEUED)

    def test_invalid_transitions_rejected(self):
        # Queued directly to Completed without processing
        with pytest.raises(JobStateTransitionError, match="Illegal job status transition"):
            validate_status_transition(JobStatus.QUEUED, JobStatus.COMPLETED)

        # Terminal statuses cannot transition out
        with pytest.raises(JobStateTransitionError, match="Illegal job status transition"):
            validate_status_transition(JobStatus.COMPLETED, JobStatus.QUEUED)

        with pytest.raises(JobStateTransitionError, match="Illegal job status transition"):
            validate_status_transition(JobStatus.DUPLICATE, JobStatus.PROCESSING)

        with pytest.raises(JobStateTransitionError, match="Illegal job status transition"):
            validate_status_transition(JobStatus.FAILED, JobStatus.PROCESSING)

    def test_exponential_backoff_calculation(self):
        # Base 10s
        assert calculate_exponential_backoff_seconds(1) == 10.0
        assert calculate_exponential_backoff_seconds(2) == 20.0
        assert calculate_exponential_backoff_seconds(3) == 40.0
        assert calculate_exponential_backoff_seconds(4) == 80.0
        # Capped at max_backoff
        assert calculate_exponential_backoff_seconds(10, max_backoff_seconds=100.0) == 100.0


class TestFileIngestionJobRepository:
    """Verifies repository interface methods on FileIngestionJobRepository."""

    @pytest.fixture
    def repo(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            yield FileIngestionJobRepository(processed_dir=Path(tmp_dir))

    def test_job_creation_and_idempotency(self, repo):
        # Initial creation
        job1 = repo.create_job(
            source_url="https://sebi.gov.in/test.pdf",
            idempotency_key="key-001",
            document_id="doc_test_1",
        )
        assert job1.status == JobStatus.QUEUED
        assert job1.stage == ProcessingStage.PENDING
        assert job1.idempotency_key == "key-001"

        # Re-registering identical idempotency key returns same job
        job2 = repo.create_job(
            source_url="https://sebi.gov.in/test.pdf",
            idempotency_key="key-001",
        )
        assert job2.id == job1.id
        assert job2.status == JobStatus.QUEUED

    def test_claim_next_job(self, repo):
        job = repo.create_job(source_url="https://sebi.gov.in/test.pdf", idempotency_key="key-002")

        # Worker claims job
        claim_token = uuid.uuid4()
        claimed = repo.claim_next_job(worker_id="worker-1", claim_token=claim_token)
        assert claimed is not None
        assert claimed.id == job.id
        assert claimed.status == JobStatus.PROCESSING
        assert claimed.claim_worker_id == "worker-1"
        assert claimed.claim_token == claim_token
        assert claimed.heartbeat_at is not None

        # Subsequent claim returns None (no more queued jobs)
        claimed_again = repo.claim_next_job(worker_id="worker-2")
        assert claimed_again is None

    def test_no_claim_before_next_retry_at(self, repo):
        job = repo.create_job(source_url="https://sebi.gov.in/test.pdf", idempotency_key="key-003")
        token = uuid.uuid4()
        claimed = repo.claim_next_job(worker_id="worker-1", claim_token=token)

        # Schedule retry 60 seconds into future
        now = datetime.now(timezone.utc)
        repo.schedule_retry(
            job_id=claimed.id,
            claim_token=token,
            error_details={"error": "timeout"},
            retry_delay_seconds=60.0,
            now=now,
        )

        # Immediate attempt to claim should return None (retry time not reached)
        assert repo.claim_next_job(worker_id="worker-1", now=now) is None

        # Claiming after retry time arrives succeeds
        future_time = now + timedelta(seconds=65)
        claimed_after = repo.claim_next_job(worker_id="worker-1", now=future_time)
        assert claimed_after is not None
        assert claimed_after.id == job.id
        assert claimed_after.attempt_count == 1

    def test_heartbeat_and_stage_update(self, repo):
        job = repo.create_job(source_url="https://sebi.gov.in/test.pdf", idempotency_key="key-004")
        token = uuid.uuid4()
        repo.claim_next_job(worker_id="worker-1", claim_token=token)

        updated = repo.update_stage_and_heartbeat(
            job_id=job.id,
            claim_token=token,
            stage=ProcessingStage.DOWNLOADING,
        )
        assert updated.stage == ProcessingStage.DOWNLOADING

        # Mismatched claim token raises StaleLeaseError
        with pytest.raises(StaleLeaseError):
            repo.update_stage_and_heartbeat(
                job_id=job.id,
                claim_token=uuid.uuid4(),  # Wrong token
                stage=ProcessingStage.PARSING,
            )

    def test_mark_completed_and_duplicate(self, repo):
        # Completed
        job_a = repo.create_job(source_url="https://sebi.gov.in/a.pdf", idempotency_key="key-a")
        token_a = uuid.uuid4()
        repo.claim_next_job(worker_id="worker-1", claim_token=token_a)
        completed = repo.mark_completed(
            job_id=job_a.id,
            claim_token=token_a,
            document_id="doc_a",
            sha256="abc123sha",
        )
        assert completed.status == JobStatus.COMPLETED
        assert completed.stage == ProcessingStage.FINISHED
        assert completed.completed_at is not None

        # Duplicate
        job_b = repo.create_job(source_url="https://sebi.gov.in/b.pdf", idempotency_key="key-b")
        token_b = uuid.uuid4()
        repo.claim_next_job(worker_id="worker-1", claim_token=token_b)
        duplicate = repo.mark_duplicate(
            job_id=job_b.id,
            claim_token=token_b,
            sha256="abc123sha",
        )
        assert duplicate.status == JobStatus.DUPLICATE
        assert duplicate.stage == ProcessingStage.FINISHED

    def test_stale_job_recovery(self, repo):
        job = repo.create_job(source_url="https://sebi.gov.in/stale.pdf", idempotency_key="key-stale")
        token = uuid.uuid4()
        t0 = datetime.now(timezone.utc)
        repo.claim_next_job(worker_id="worker-dead", claim_token=token, now=t0)

        # At t0 + 10s (lease timeout 60s), should NOT recover
        recovered = repo.recover_stale_jobs(lease_timeout_seconds=60.0, now=t0 + timedelta(seconds=10))
        assert recovered == 0

        # At t0 + 70s, should recover and re-queue
        t_recovered = t0 + timedelta(seconds=70)
        recovered = repo.recover_stale_jobs(lease_timeout_seconds=60.0, now=t_recovered)
        assert recovered == 1

        rec = repo.get_job(job.id)
        assert rec.status == JobStatus.QUEUED
        assert rec.attempt_count == 1
        assert rec.claim_token is None

        # Old worker trying to update with expired lease gets StaleLeaseError
        with pytest.raises(StaleLeaseError):
            repo.update_stage_and_heartbeat(job.id, token, ProcessingStage.PARSING)
