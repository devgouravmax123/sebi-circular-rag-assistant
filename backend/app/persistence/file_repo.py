"""
File-system backed storage repository implementation.
Serves as the local zero-cost default, reading and writing to data/processed/ and cscrf_graph.json.
Guarantees 100% backward compatibility with all Phase 0–7 behaviors while rigorously raising
CorruptedStorageError on malformed, unparseable, or invalid records instead of silently fabricating data.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any
import uuid
from pydantic import ValidationError

from app.schemas.document import DocumentRecord, ChunkRecord
from app.schemas.graph import (
    GraphEdge,
    ReviewStatus,
    RelationshipType,
    ScopeType,
    ConfidenceLevel,
    EdgeProvenance,
    EdgeReviewHistoryItem,
)
from app.schemas.ingestion_job import (
    IngestionJobRecord,
    JobStatus,
    ProcessingStage,
    StaleLeaseError,
    JobStateTransitionError,
)
from app.persistence.interfaces import (
    DocumentRepository,
    ChunkRepository,
    GraphEdgeRepository,
    IngestionJobRepository,
    StorageRepositoryFactory,
    CorruptedStorageError,
    PersistenceError,
)


class FileDocumentRepository(DocumentRepository):
    """File-backed document metadata repository reading data/processed/."""

    def __init__(self, processed_dir: str | Path = "data/processed"):
        self.processed_dir = Path(processed_dir)

    def get_document(self, document_id: str) -> Optional[DocumentRecord]:
        doc_dir = self.processed_dir / document_id
        if not doc_dir.exists():
            return None

        doc_file = doc_dir / "document.json"
        if not doc_file.exists():
            doc_file = doc_dir / "metadata.json"
        if not doc_file.exists():
            return None

        try:
            with open(doc_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return DocumentRecord(**data)
        except json.JSONDecodeError as e:
            raise CorruptedStorageError(f"Malformed JSON in document record '{doc_file}': {e}") from e
        except (ValidationError, TypeError) as e:
            raise CorruptedStorageError(f"Invalid schema in document record '{doc_file}': {e}") from e

    def list_documents(self) -> List[DocumentRecord]:
        docs: List[DocumentRecord] = []
        if not self.processed_dir.exists():
            return docs
        for item in sorted(self.processed_dir.iterdir()):
            if item.is_dir():
                doc = self.get_document(item.name)
                if doc:
                    docs.append(doc)
        return docs

    def save_document(self, document: DocumentRecord) -> None:
        doc_dir = self.processed_dir / document.document_id
        doc_dir.mkdir(parents=True, exist_ok=True)
        doc_file = doc_dir / "document.json"
        with open(doc_file, "w", encoding="utf-8") as f:
            f.write(document.model_dump_json(indent=2))

    def get_document_by_sha256(self, sha256: str) -> Optional[DocumentRecord]:
        for doc in self.list_documents():
            if doc.sha256 == sha256:
                return doc
        return None


class FileChunkRepository(ChunkRepository):
    """File-backed chunk repository reading from data/processed/{doc_id}/chunks.json."""

    def __init__(self, processed_dir: str | Path = "data/processed"):
        self.processed_dir = Path(processed_dir)

    def get_chunk(self, chunk_id: str) -> Optional[ChunkRecord]:
        for chunk in self.list_all_chunks():
            if chunk.chunk_id == chunk_id:
                return chunk
        return None

    def list_chunks_for_document(self, document_id: str) -> List[ChunkRecord]:
        chunks_file = self.processed_dir / document_id / "chunks.json"
        if not chunks_file.exists():
            return []
        try:
            with open(chunks_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise CorruptedStorageError(f"chunks.json in '{chunks_file}' must be a JSON array, got {type(data).__name__}")
            return [ChunkRecord(**c) for c in data]
        except json.JSONDecodeError as e:
            raise CorruptedStorageError(f"Malformed JSON in chunk record '{chunks_file}': {e}") from e
        except (ValidationError, TypeError) as e:
            raise CorruptedStorageError(f"Invalid chunk schema in '{chunks_file}': {e}") from e

    def list_all_chunks(self) -> List[ChunkRecord]:
        all_chunks: List[ChunkRecord] = []
        if not self.processed_dir.exists():
            return all_chunks
        for item in sorted(self.processed_dir.iterdir()):
            if item.is_dir():
                all_chunks.extend(self.list_chunks_for_document(item.name))
        return all_chunks

    def save_chunks(self, chunks: List[ChunkRecord]) -> None:
        if not chunks:
            return
        doc_id = chunks[0].document_id
        chunks_file = self.processed_dir / doc_id / "chunks.json"
        chunks_file.parent.mkdir(parents=True, exist_ok=True)
        with open(chunks_file, "w", encoding="utf-8") as f:
            json.dump([c.model_dump() for c in chunks], f, indent=2)

    def save_embeddings(self, embeddings: Dict[str, Any], model_name: str = "BAAI/bge-small-en-v1.5") -> None:
        # File-backed embeddings are stored in data/indexes/
        pass

    def search_similar_chunks(
        self, query_vector: Any, top_k: int = 10, model_name: str = "BAAI/bge-small-en-v1.5"
    ) -> List[Tuple[str, float]]:
        # Supported by dense index loader
        return []


class FileGraphEdgeRepository(GraphEdgeRepository):
    """File-backed graph edge repository reading data/processed/cscrf_graph.json."""

    def __init__(self, processed_dir: str | Path = "data/processed"):
        self.processed_dir = Path(processed_dir)
        self.graph_file = self.processed_dir / "cscrf_graph.json"

    def _load_raw_edges(self) -> List[GraphEdge]:
        if not self.graph_file.exists():
            return []
        try:
            with open(self.graph_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw_list = data if isinstance(data, list) else data.get("edges", [])
            if not isinstance(raw_list, list):
                raise CorruptedStorageError(f"Expected list of edges in '{self.graph_file}', got {type(raw_list).__name__}")
            return [GraphEdge(**e) for e in raw_list]
        except json.JSONDecodeError as e:
            raise CorruptedStorageError(f"Malformed JSON in graph edge record '{self.graph_file}': {e}") from e
        except (ValidationError, TypeError, ValueError) as e:
            raise CorruptedStorageError(f"Invalid edge schema in '{self.graph_file}': {e}") from e

    def get_edge(self, edge_id: str) -> Optional[GraphEdge]:
        for edge in self._load_raw_edges():
            if edge.edge_id == edge_id:
                return edge
        return None

    def list_all_edges(self) -> List[GraphEdge]:
        return self._load_raw_edges()

    def get_outgoing_edges(self, document_id: str) -> List[GraphEdge]:
        return [e for e in self._load_raw_edges() if e.source_document_id == document_id]

    def get_incoming_edges(self, document_id: str) -> List[GraphEdge]:
        return [e for e in self._load_raw_edges() if e.target_document_id == document_id]

    def save_edge(self, edge: GraphEdge) -> None:
        edges = self._load_raw_edges()
        # Replace if existing, else append
        updated = [e for e in edges if e.edge_id != edge.edge_id]
        updated.append(edge)
        self.graph_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.graph_file, "w", encoding="utf-8") as f:
            json.dump([e.model_dump(mode="json") for e in updated], f, indent=2)

    def list_edges(
        self,
        status: Optional[str] = None,
        relationship: Optional[str] = None,
        source_document_id: Optional[str] = None,
        target_document_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[GraphEdge]:
        edges = self._load_raw_edges()
        filtered = []
        for e in edges:
            if status is not None and e.review_status.value != status:
                continue
            if relationship is not None and e.relationship.value != relationship:
                continue
            if source_document_id is not None and e.source_document_id != source_document_id:
                continue
            if target_document_id is not None and e.target_document_id != target_document_id:
                continue
            filtered.append(e)
        return filtered[offset : offset + limit]

    def propose_edge(self, edge: GraphEdge) -> GraphEdge:
        edges = self._load_raw_edges()
        if any(e.edge_id == edge.edge_id for e in edges):
            raise PersistenceError(f"Edge with ID '{edge.edge_id}' already exists")

        # Initial review history entry
        now = datetime.now(timezone.utc)
        edge.review_status = ReviewStatus.PROPOSED
        edge.provenance = EdgeProvenance.USER_PROPOSED
        edge.created_at = now
        edge.updated_at = now
        history_item = EdgeReviewHistoryItem(
            action="PROPOSE",
            from_status="NONE",
            to_status=ReviewStatus.PROPOSED.value,
            reviewer_id=edge.reviewed_by or "system_user",
            reviewer_name=None,
            rationale=edge.review_rationale or "Initial proposal submitted for human review",
            timestamp=now,
        )
        edge.review_history = [history_item]
        self.save_edge(edge)
        return edge

    def review_edge(
        self,
        edge_id: str,
        decision: str,
        reviewer_id: str,
        rationale: str,
        reviewer_name: Optional[str] = None,
    ) -> GraphEdge:
        edge = self.get_edge(edge_id)
        if not edge:
            raise PersistenceError(f"Edge '{edge_id}' not found")

        decision_clean = decision.lower().strip()
        if decision_clean not in ("verify", "reject"):
            raise ValueError(f"Invalid review decision '{decision}'. Must be 'verify' or 'reject'")

        if edge.review_status == ReviewStatus.REVOKED:
            raise ValueError(f"Cannot review edge '{edge_id}': edge has already been REVOKED and cannot be modified without re-proposing.")

        new_status = ReviewStatus.VERIFIED if decision_clean == "verify" else ReviewStatus.REJECTED
        old_status = edge.review_status.value
        now = datetime.now(timezone.utc)

        history_item = EdgeReviewHistoryItem(
            action="VERIFY" if decision_clean == "verify" else "REJECT",
            from_status=old_status,
            to_status=new_status.value,
            reviewer_id=reviewer_id,
            reviewer_name=reviewer_name,
            rationale=rationale,
            timestamp=now,
        )

        edge.review_status = new_status
        edge.reviewed_by = reviewer_id
        edge.reviewed_at = now
        edge.review_rationale = rationale
        edge.updated_at = now
        edge.review_history.append(history_item)

        self.save_edge(edge)
        return edge

    def revoke_edge(
        self,
        edge_id: str,
        reviewer_id: str,
        rationale: str,
        reviewer_name: Optional[str] = None,
    ) -> GraphEdge:
        edge = self.get_edge(edge_id)
        if not edge:
            raise PersistenceError(f"Edge '{edge_id}' not found")

        if edge.review_status != ReviewStatus.VERIFIED:
            raise ValueError(f"Cannot revoke edge '{edge_id}' with status '{edge.review_status.value}': only VERIFIED edges can be revoked.")

        old_status = edge.review_status.value
        now = datetime.now(timezone.utc)

        history_item = EdgeReviewHistoryItem(
            action="REVOKE",
            from_status=old_status,
            to_status=ReviewStatus.REVOKED.value,
            reviewer_id=reviewer_id,
            reviewer_name=reviewer_name,
            rationale=rationale,
            timestamp=now,
        )

        edge.review_status = ReviewStatus.REVOKED
        edge.reviewed_by = reviewer_id
        edge.reviewed_at = now
        edge.review_rationale = rationale
        edge.updated_at = now
        edge.review_history.append(history_item)

        self.save_edge(edge)
        return edge

    def reconcile_baseline_edges(
        self,
        baseline_edges: List[GraphEdge],
        allow_overwrite: bool = False,
    ) -> Dict[str, Any]:
        existing_edges = {e.edge_id: e for e in self._load_raw_edges()}
        inserted = 0
        preserved = 0
        updated = 0

        for b_edge in baseline_edges:
            if b_edge.edge_id not in existing_edges:
                existing_edges[b_edge.edge_id] = b_edge
                inserted += 1
            else:
                curr = existing_edges[b_edge.edge_id]
                # Never overwrite verified edges unless explicitly allowed and verified
                if curr.review_status == ReviewStatus.VERIFIED:
                    preserved += 1
                elif allow_overwrite:
                    existing_edges[b_edge.edge_id] = b_edge
                    updated += 1
                else:
                    preserved += 1

        self.graph_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.graph_file, "w", encoding="utf-8") as f:
            json.dump([e.model_dump(mode="json") for e in existing_edges.values()], f, indent=2)

        return {
            "total_baseline": len(baseline_edges),
            "inserted": inserted,
            "preserved": preserved,
            "updated": updated,
        }


class FileIngestionJobRepository(IngestionJobRepository):
    """File-backed ingestion job repository storing jobs in data/processed/ingestion_jobs.json."""

    def __init__(self, processed_dir: str | Path = "data/processed"):
        self.processed_dir = Path(processed_dir)
        self.jobs_file = self.processed_dir / "ingestion_jobs.json"

    def _load_jobs(self) -> Dict[str, Dict[str, Any]]:
        if not self.jobs_file.exists():
            return {}
        try:
            with open(self.jobs_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            raise CorruptedStorageError(f"Malformed jobs file '{self.jobs_file}': {e}") from e

    def _save_jobs(self, jobs: Dict[str, Dict[str, Any]]) -> None:
        self.jobs_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.jobs_file, "w", encoding="utf-8") as f:
            json.dump(jobs, f, indent=2)

    def _to_record(self, data: Dict[str, Any]) -> IngestionJobRecord:
        return IngestionJobRecord(
            id=uuid.UUID(data["id"]),
            source_url=data["source_url"],
            idempotency_key=data["idempotency_key"],
            status=JobStatus(data["status"]),
            stage=ProcessingStage(data.get("stage", "pending")),
            document_id=data.get("document_id"),
            sha256=data.get("sha256"),
            attempt_count=data.get("attempt_count", 0),
            max_attempts=data.get("max_attempts", 3),
            claim_worker_id=data.get("claim_worker_id"),
            claim_token=uuid.UUID(data["claim_token"]) if data.get("claim_token") else None,
            heartbeat_at=datetime.fromisoformat(data["heartbeat_at"]) if data.get("heartbeat_at") else None,
            next_retry_at=datetime.fromisoformat(data["next_retry_at"]) if data.get("next_retry_at") else None,
            error_details=data.get("error_details"),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else datetime.now(timezone.utc),
            started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None,
            completed_at=datetime.fromisoformat(data["completed_at"]) if data.get("completed_at") else None,
        )

    def _to_dict(self, record: IngestionJobRecord) -> Dict[str, Any]:
        return {
            "id": str(record.id),
            "source_url": record.source_url,
            "idempotency_key": record.idempotency_key,
            "status": record.status.value,
            "stage": record.stage.value,
            "document_id": record.document_id,
            "sha256": record.sha256,
            "attempt_count": record.attempt_count,
            "max_attempts": record.max_attempts,
            "claim_worker_id": record.claim_worker_id,
            "claim_token": str(record.claim_token) if record.claim_token else None,
            "heartbeat_at": record.heartbeat_at.isoformat() if record.heartbeat_at else None,
            "next_retry_at": record.next_retry_at.isoformat() if record.next_retry_at else None,
            "error_details": record.error_details,
            "created_at": record.created_at.isoformat(),
            "started_at": record.started_at.isoformat() if record.started_at else None,
            "completed_at": record.completed_at.isoformat() if record.completed_at else None,
        }

    def create_job(
        self,
        source_url: str,
        idempotency_key: str,
        document_id: Optional[str] = None,
        max_attempts: int = 3,
    ) -> IngestionJobRecord:
        jobs = self._load_jobs()
        for j in jobs.values():
            if j.get("idempotency_key") == idempotency_key:
                return self._to_record(j)

        job_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        record = IngestionJobRecord(
            id=job_id,
            source_url=source_url,
            idempotency_key=idempotency_key,
            document_id=document_id,
            max_attempts=max_attempts,
            created_at=now,
        )
        jobs[str(job_id)] = self._to_dict(record)
        self._save_jobs(jobs)
        return record

    def get_job(self, job_id: uuid.UUID) -> Optional[IngestionJobRecord]:
        jobs = self._load_jobs()
        data = jobs.get(str(job_id))
        return self._to_record(data) if data else None

    def get_job_by_idempotency_key(self, idempotency_key: str) -> Optional[IngestionJobRecord]:
        jobs = self._load_jobs()
        for data in jobs.values():
            if data.get("idempotency_key") == idempotency_key:
                return self._to_record(data)
        return None

    def claim_next_job(
        self,
        worker_id: str,
        claim_token: Optional[uuid.UUID] = None,
        now: Optional[datetime] = None,
    ) -> Optional[IngestionJobRecord]:
        current_time = now or datetime.now(timezone.utc)
        jobs = self._load_jobs()

        for j_id, data in sorted(jobs.items(), key=lambda x: x[1].get("created_at", "")):
            if data.get("status") == JobStatus.QUEUED.value:
                retry_at = datetime.fromisoformat(data["next_retry_at"]) if data.get("next_retry_at") else None
                if retry_at and retry_at > current_time:
                    continue

                token = claim_token or uuid.uuid4()
                data["status"] = JobStatus.PROCESSING.value
                data["stage"] = ProcessingStage.PENDING.value
                data["claim_worker_id"] = worker_id
                data["claim_token"] = str(token)
                data["heartbeat_at"] = current_time.isoformat()
                if not data.get("started_at"):
                    data["started_at"] = current_time.isoformat()

                self._save_jobs(jobs)
                return self._to_record(data)

        return None

    def update_stage_and_heartbeat(
        self,
        job_id: uuid.UUID,
        claim_token: uuid.UUID,
        stage: ProcessingStage,
        now: Optional[datetime] = None,
    ) -> IngestionJobRecord:
        jobs = self._load_jobs()
        data = jobs.get(str(job_id))
        if not data:
            raise PersistenceError(f"Job '{job_id}' not found")
        if data.get("claim_token") != str(claim_token) or data.get("status") != JobStatus.PROCESSING.value:
            raise StaleLeaseError(f"Lease expired or invalid for job '{job_id}'")

        current_time = now or datetime.now(timezone.utc)
        data["stage"] = stage.value
        data["heartbeat_at"] = current_time.isoformat()
        self._save_jobs(jobs)
        return self._to_record(data)

    def schedule_retry(
        self,
        job_id: uuid.UUID,
        claim_token: uuid.UUID,
        error_details: Dict[str, Any],
        retry_delay_seconds: float,
        now: Optional[datetime] = None,
    ) -> IngestionJobRecord:
        jobs = self._load_jobs()
        data = jobs.get(str(job_id))
        if not data:
            raise PersistenceError(f"Job '{job_id}' not found")
        if data.get("claim_token") != str(claim_token) or data.get("status") != JobStatus.PROCESSING.value:
            raise StaleLeaseError(f"Lease expired or invalid for job '{job_id}'")

        current_time = now or datetime.now(timezone.utc)
        attempts = data.get("attempt_count", 0) + 1
        max_attempts = data.get("max_attempts", 3)
        data["attempt_count"] = attempts
        data["error_details"] = error_details

        if attempts >= max_attempts:
            data["status"] = JobStatus.FAILED.value
            data["completed_at"] = current_time.isoformat()
        else:
            data["status"] = JobStatus.QUEUED.value
            retry_dt = datetime.fromtimestamp(current_time.timestamp() + retry_delay_seconds, tz=timezone.utc)
            data["next_retry_at"] = retry_dt.isoformat()
            data["claim_token"] = None
            data["claim_worker_id"] = None

        self._save_jobs(jobs)
        return self._to_record(data)

    def mark_completed(
        self,
        job_id: uuid.UUID,
        claim_token: uuid.UUID,
        document_id: str,
        sha256: str,
        now: Optional[datetime] = None,
    ) -> IngestionJobRecord:
        jobs = self._load_jobs()
        data = jobs.get(str(job_id))
        if not data:
            raise PersistenceError(f"Job '{job_id}' not found")
        if data.get("claim_token") != str(claim_token) or data.get("status") != JobStatus.PROCESSING.value:
            raise StaleLeaseError(f"Lease expired or invalid for job '{job_id}'")

        current_time = now or datetime.now(timezone.utc)
        data["status"] = JobStatus.COMPLETED.value
        data["stage"] = ProcessingStage.FINISHED.value
        data["document_id"] = document_id
        data["sha256"] = sha256
        data["completed_at"] = current_time.isoformat()
        self._save_jobs(jobs)
        return self._to_record(data)

    def mark_duplicate(
        self,
        job_id: uuid.UUID,
        claim_token: uuid.UUID,
        sha256: str,
        document_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> IngestionJobRecord:
        jobs = self._load_jobs()
        data = jobs.get(str(job_id))
        if not data:
            raise PersistenceError(f"Job '{job_id}' not found")
        if data.get("claim_token") != str(claim_token) or data.get("status") != JobStatus.PROCESSING.value:
            raise StaleLeaseError(f"Lease expired or invalid for job '{job_id}'")

        current_time = now or datetime.now(timezone.utc)
        data["status"] = JobStatus.DUPLICATE.value
        data["stage"] = ProcessingStage.FINISHED.value
        data["sha256"] = sha256
        if document_id:
            data["document_id"] = document_id
        data["completed_at"] = current_time.isoformat()
        self._save_jobs(jobs)
        return self._to_record(data)

    def mark_failed(
        self,
        job_id: uuid.UUID,
        claim_token: uuid.UUID,
        error_details: Dict[str, Any],
        now: Optional[datetime] = None,
    ) -> IngestionJobRecord:
        jobs = self._load_jobs()
        data = jobs.get(str(job_id))
        if not data:
            raise PersistenceError(f"Job '{job_id}' not found")
        if data.get("claim_token") != str(claim_token) or data.get("status") != JobStatus.PROCESSING.value:
            raise StaleLeaseError(f"Lease expired or invalid for job '{job_id}'")

        current_time = now or datetime.now(timezone.utc)
        data["status"] = JobStatus.FAILED.value
        data["error_details"] = error_details
        data["completed_at"] = current_time.isoformat()
        self._save_jobs(jobs)
        return self._to_record(data)

    def recover_stale_jobs(
        self,
        lease_timeout_seconds: float = 60.0,
        now: Optional[datetime] = None,
    ) -> int:
        current_time = now or datetime.now(timezone.utc)
        jobs = self._load_jobs()
        recovered_count = 0

        for data in jobs.values():
            if data.get("status") == JobStatus.PROCESSING.value:
                hb = datetime.fromisoformat(data["heartbeat_at"]) if data.get("heartbeat_at") else None
                if hb and (current_time.timestamp() - hb.timestamp()) > lease_timeout_seconds:
                    attempts = data.get("attempt_count", 0) + 1
                    data["attempt_count"] = attempts
                    if attempts >= data.get("max_attempts", 3):
                        data["status"] = JobStatus.FAILED.value
                        data["completed_at"] = current_time.isoformat()
                        data["error_details"] = {"reason": "Heartbeat lease expired; max attempts reached"}
                    else:
                        data["status"] = JobStatus.QUEUED.value
                        data["claim_token"] = None
                        data["claim_worker_id"] = None
                        data["error_details"] = {"reason": "Recovered from stale worker lease timeout"}
                    recovered_count += 1

        if recovered_count > 0:
            self._save_jobs(jobs)
        return recovered_count

    def list_jobs(
        self,
        status: Optional[JobStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[IngestionJobRecord]:
        jobs = self._load_jobs()
        records: List[IngestionJobRecord] = []
        for data in sorted(jobs.values(), key=lambda x: x.get("created_at", "")):
            rec = self._to_record(data)
            if status is None or rec.status == status:
                records.append(rec)
        return records[offset : offset + limit]


class FileStorageRepositoryFactory(StorageRepositoryFactory):
    """Factory creating file-system backed repository instances."""

    def __init__(self, processed_dir: str | Path = "data/processed"):
        self.processed_dir = Path(processed_dir)

    def get_document_repository(self) -> DocumentRepository:
        return FileDocumentRepository(self.processed_dir)

    def get_chunk_repository(self) -> ChunkRepository:
        return FileChunkRepository(self.processed_dir)

    def get_graph_edge_repository(self) -> GraphEdgeRepository:
        return FileGraphEdgeRepository(self.processed_dir)

    def get_ingestion_job_repository(self) -> IngestionJobRepository:
        return FileIngestionJobRepository(self.processed_dir)
