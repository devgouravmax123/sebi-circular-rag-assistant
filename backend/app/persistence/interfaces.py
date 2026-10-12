"""
Abstract storage repository interfaces and domain exceptions for the SEBI Regulatory Intelligence Platform.
Enables pluggable persistence implementations (file-backed local mode vs. PostgreSQL).
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Tuple, Any
from datetime import datetime
import uuid
import numpy as np

from app.schemas.document import DocumentRecord, ChunkRecord
from app.schemas.graph import GraphEdge
from app.schemas.ingestion_job import (
    IngestionJobRecord,
    JobStatus,
    ProcessingStage,
    StaleLeaseError,
    JobStateTransitionError,
)


class PersistenceError(Exception):
    """Base exception for persistence failures."""
    pass


class CorruptedStorageError(PersistenceError):
    """Raised when an underlying persistent artifact or JSON record is malformed or invalid."""
    pass


class ConnectionStorageError(PersistenceError):
    """Raised when the database server is unreachable or connection fails."""
    pass


class DocumentRepository(ABC):
    """Abstract interface for managing regulatory documents and versions."""

    @abstractmethod
    def get_document(self, document_id: str) -> Optional[DocumentRecord]:
        """
        Retrieve document metadata by document_id.
        Raises CorruptedStorageError if record file is malformed or fails schema validation.
        """
        pass

    @abstractmethod
    def list_documents(self) -> List[DocumentRecord]:
        """
        List all registered regulatory documents.
        Raises CorruptedStorageError if any record file is corrupted.
        """
        pass

    @abstractmethod
    def save_document(self, document: DocumentRecord) -> None:
        """Persist or register a document record."""
        pass

    @abstractmethod
    def get_document_by_sha256(self, sha256: str) -> Optional[DocumentRecord]:
        """Retrieve document by source PDF checksum for duplicate detection."""
        pass


class ChunkRepository(ABC):
    """Abstract interface for retrieving text chunks and embeddings."""

    @abstractmethod
    def get_chunk(self, chunk_id: str) -> Optional[ChunkRecord]:
        """
        Retrieve chunk by chunk_id.
        Raises CorruptedStorageError if chunk data is unreadable or malformed.
        """
        pass

    @abstractmethod
    def list_chunks_for_document(self, document_id: str) -> List[ChunkRecord]:
        """
        Retrieve all ordered chunks for a document.
        Raises CorruptedStorageError if chunks.json exists but is malformed.
        """
        pass

    @abstractmethod
    def list_all_chunks(self) -> List[ChunkRecord]:
        """
        Retrieve all chunks across the indexed corpus.
        Raises CorruptedStorageError if any document's chunks are corrupted.
        """
        pass

    @abstractmethod
    def save_chunks(self, chunks: List[ChunkRecord]) -> None:
        """Persist chunks for a document."""
        pass

    @abstractmethod
    def save_embeddings(self, embeddings: Dict[str, np.ndarray], model_name: str = "BAAI/bge-small-en-v1.5") -> None:
        """
        Persist dense vector embeddings mapped by chunk_id.
        Enforces 384-dimensional vector constraint.
        """
        pass

    @abstractmethod
    def search_similar_chunks(
        self, query_vector: np.ndarray, top_k: int = 10, model_name: str = "BAAI/bge-small-en-v1.5"
    ) -> List[Tuple[str, float]]:
        """
        Exact cosine similarity search over chunk vectors.
        Returns ordered list of (chunk_id, cosine_similarity_score).
        """
        pass


class GraphEdgeRepository(ABC):
    """Abstract interface for storing and traversing regulatory relationship edges."""

    @abstractmethod
    def get_edge(self, edge_id: str) -> Optional[GraphEdge]:
        """
        Retrieve an edge by edge_id.
        Raises CorruptedStorageError if graph definition is malformed.
        """
        pass

    @abstractmethod
    def list_all_edges(self) -> List[GraphEdge]:
        """
        Retrieve all regulatory edges.
        Raises CorruptedStorageError if graph definition is malformed.
        """
        pass

    @abstractmethod
    def list_edges(
        self,
        status: Optional[str] = None,
        relationship: Optional[str] = None,
        source_document_id: Optional[str] = None,
        target_document_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[GraphEdge]:
        """
        Retrieve regulatory edges matching optional filters with bounded pagination.
        """
        pass

    @abstractmethod
    def get_outgoing_edges(self, document_id: str) -> List[GraphEdge]:
        """Retrieve edges where document_id is source (acting/newer)."""
        pass

    @abstractmethod
    def get_incoming_edges(self, document_id: str) -> List[GraphEdge]:
        """Retrieve edges where document_id is target (affected/older)."""
        pass

    @abstractmethod
    def save_edge(self, edge: GraphEdge) -> None:
        """Persist a regulatory relationship edge."""
        pass

    @abstractmethod
    def propose_edge(self, edge: GraphEdge) -> GraphEdge:
        """
        Propose a new candidate relationship. Status defaults to PROPOSED and provenance to USER_PROPOSED.
        Initial review history item is recorded.
        """
        pass

    @abstractmethod
    def review_edge(
        self,
        edge_id: str,
        decision: str,
        reviewer_id: str,
        rationale: str,
        reviewer_name: Optional[str] = None,
    ) -> GraphEdge:
        """
        Submit a human review decision (verify or reject).
        Records immutable audit history and updates review_status.
        """
        pass

    @abstractmethod
    def revoke_edge(
        self,
        edge_id: str,
        reviewer_id: str,
        rationale: str,
        reviewer_name: Optional[str] = None,
    ) -> GraphEdge:
        """
        Revoke an existing relationship.
        Records immutable audit history and marks status REVOKED.
        """
        pass

    @abstractmethod
    def reconcile_baseline_edges(
        self,
        baseline_edges: List[GraphEdge],
        allow_overwrite: bool = False,
    ) -> Dict[str, Any]:
        """
        Idempotently import and reconcile baseline regulatory edges without destructive replacement.
        Preserves existing VERIFIED edges.
        """
        pass


class IngestionJobRepository(ABC):
    """Abstract interface for managing ingestion jobs and worker queues."""

    @abstractmethod
    def create_job(
        self,
        source_url: str,
        idempotency_key: str,
        document_id: Optional[str] = None,
        max_attempts: int = 3,
    ) -> IngestionJobRecord:
        """
        Create a new ingestion job or return an existing job matching the idempotency key.
        Guarantees idempotent registration.
        """
        pass

    @abstractmethod
    def get_job(self, job_id: uuid.UUID) -> Optional[IngestionJobRecord]:
        """Retrieve job record by primary key."""
        pass

    @abstractmethod
    def get_job_by_idempotency_key(self, idempotency_key: str) -> Optional[IngestionJobRecord]:
        """Retrieve job record by unique idempotency key."""
        pass

    @abstractmethod
    def claim_next_job(
        self,
        worker_id: str,
        claim_token: Optional[uuid.UUID] = None,
        now: Optional[datetime] = None,
    ) -> Optional[IngestionJobRecord]:
        """
        Atomically claim the next eligible queued job using FOR UPDATE SKIP LOCKED.
        Transitions status from 'queued' to 'processing'.
        Returns None if no eligible job is available.
        """
        pass

    @abstractmethod
    def update_stage_and_heartbeat(
        self,
        job_id: uuid.UUID,
        claim_token: uuid.UUID,
        stage: ProcessingStage,
        now: Optional[datetime] = None,
    ) -> IngestionJobRecord:
        """
        Update the processing sub-stage and heartbeat timestamp.
        Raises StaleLeaseError if the claim lease is expired or owned by another claim_token.
        """
        pass

    @abstractmethod
    def schedule_retry(
        self,
        job_id: uuid.UUID,
        claim_token: uuid.UUID,
        error_details: Dict[str, Any],
        retry_delay_seconds: float,
        now: Optional[datetime] = None,
    ) -> IngestionJobRecord:
        """
        Record a transient failure and schedule next retry in the future.
        Transitions status from 'processing' back to 'queued'.
        Increments attempt_count. If attempts exceed max_attempts, marks as terminal 'failed'.
        """
        pass

    @abstractmethod
    def mark_completed(
        self,
        job_id: uuid.UUID,
        claim_token: uuid.UUID,
        document_id: str,
        sha256: str,
        now: Optional[datetime] = None,
    ) -> IngestionJobRecord:
        """
        Mark job as successfully completed.
        Transitions status from 'processing' to 'completed', stage to 'finished'.
        """
        pass

    @abstractmethod
    def mark_duplicate(
        self,
        job_id: uuid.UUID,
        claim_token: uuid.UUID,
        sha256: str,
        document_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> IngestionJobRecord:
        """
        Mark job as duplicate (identical content already ingested).
        Transitions status from 'processing' to 'duplicate', stage to 'finished'.
        """
        pass

    @abstractmethod
    def mark_failed(
        self,
        job_id: uuid.UUID,
        claim_token: uuid.UUID,
        error_details: Dict[str, Any],
        now: Optional[datetime] = None,
    ) -> IngestionJobRecord:
        """
        Record a terminal (non-retryable) failure.
        Transitions status from 'processing' to 'failed'.
        """
        pass

    @abstractmethod
    def recover_stale_jobs(
        self,
        lease_timeout_seconds: float = 60.0,
        now: Optional[datetime] = None,
    ) -> int:
        """
        Identify jobs stuck in 'processing' whose heartbeat is older than lease_timeout_seconds.
        Resets them to 'queued' if attempts remain, or marks as 'failed' if max attempts reached.
        Returns the number of recovered jobs.
        """
        pass

    @abstractmethod
    def list_jobs(
        self,
        status: Optional[JobStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[IngestionJobRecord]:
        """List ingestion jobs with bounded pagination and optional status filter."""
        pass


class StorageRepositoryFactory(ABC):
    """Abstract factory for acquiring repository instances."""

    @abstractmethod
    def get_document_repository(self) -> DocumentRepository:
        pass

    @abstractmethod
    def get_chunk_repository(self) -> ChunkRepository:
        pass

    @abstractmethod
    def get_graph_edge_repository(self) -> GraphEdgeRepository:
        pass

    @abstractmethod
    def get_ingestion_job_repository(self) -> IngestionJobRepository:
        pass
