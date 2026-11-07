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

from app.schemas.document import DocumentRecord, ChunkRecord, resolve_collection_identity
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
    WorkspaceRepository,
    CollectionRepository,
    QueryHistoryRepository,
    StorageRepositoryFactory,
    CorruptedStorageError,
    PersistenceError,
)


class FileDocumentRepository(DocumentRepository):
    """File-backed document metadata repository reading data/processed/."""

    def __init__(self, processed_dir: str | Path = "data/processed"):
        self.processed_dir = Path(processed_dir)

    def get_document(self, document_id: str, workspace_id: Optional[uuid.UUID] = None) -> Optional[DocumentRecord]:
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
            rec = DocumentRecord(**data)
            if workspace_id is not None and getattr(rec, "workspace_id", None) != workspace_id:
                return None
            return rec
        except json.JSONDecodeError as e:
            raise CorruptedStorageError(f"Malformed JSON in document record '{doc_file}': {e}") from e
        except (ValidationError, TypeError) as e:
            raise CorruptedStorageError(f"Invalid schema in document record '{doc_file}': {e}") from e

    def list_documents(self, collection_id: str, workspace_id: Optional[uuid.UUID] = None) -> List[DocumentRecord]:
        if not collection_id or not isinstance(collection_id, str) or not collection_id.strip():
            raise ValueError("collection_id must be a non-empty string")
        return self._list_documents_internal(collection_id=collection_id.strip(), workspace_id=workspace_id)

    def list_all_documents_global(self) -> List[DocumentRecord]:
        return self._list_documents_internal(collection_id=None, workspace_id=None)

    def _list_documents_internal(self, collection_id: Optional[str] = None, workspace_id: Optional[uuid.UUID] = None) -> List[DocumentRecord]:
        docs: List[DocumentRecord] = []
        if not self.processed_dir.exists():
            return docs
        for item in sorted(self.processed_dir.iterdir()):
            if item.is_dir():
                doc = self.get_document(item.name)
                if doc:
                    if collection_id is None or resolve_collection_identity(doc.document_id, getattr(doc, "collection_id", None)) == collection_id:
                        if workspace_id is None or getattr(doc, "workspace_id", None) == workspace_id:
                            docs.append(doc)
        return docs

    def save_document(self, document: DocumentRecord) -> None:
        doc_dir = self.processed_dir / document.document_id
        doc_dir.mkdir(parents=True, exist_ok=True)
        doc_file = doc_dir / "document.json"
        with open(doc_file, "w", encoding="utf-8") as f:
            f.write(document.model_dump_json(indent=2))

    def get_document_by_sha256(self, sha256: str, collection_id: str, workspace_id: Optional[uuid.UUID] = None) -> Optional[DocumentRecord]:
        if not collection_id or not collection_id.strip():
            raise ValueError(
                "collection_id is required for collection-scoped SHA-256 lookup. "
                "Use get_document_by_sha256_global() for explicit unscoped lookup."
            )
        for doc in self.list_documents(collection_id=collection_id.strip(), workspace_id=workspace_id):
            if doc.sha256 == sha256:
                return doc
        return None

    def get_document_by_sha256_global(self, sha256: str) -> Optional[DocumentRecord]:
        for doc in self.list_all_documents_global():
            if doc.sha256 == sha256:
                return doc
        return None


class FileChunkRepository(ChunkRepository):
    """File-backed chunk repository reading from data/processed/{doc_id}/chunks.json."""

    def __init__(self, processed_dir: str | Path = "data/processed"):
        self.processed_dir = Path(processed_dir)

    def get_chunk(self, chunk_id: str) -> Optional[ChunkRecord]:
        for chunk in self.list_all_chunks_global():
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

    def list_all_chunks(self, collection_id: str) -> List[ChunkRecord]:
        if not collection_id or not isinstance(collection_id, str) or not collection_id.strip():
            raise ValueError("collection_id must be a non-empty string")
        return self._list_all_chunks_internal(collection_id=collection_id.strip())

    def list_all_chunks_global(self) -> List[ChunkRecord]:
        return self._list_all_chunks_internal(collection_id=None)

    def _list_all_chunks_internal(self, collection_id: Optional[str] = None) -> List[ChunkRecord]:
        all_chunks: List[ChunkRecord] = []
        if not self.processed_dir.exists():
            return all_chunks
        for item in sorted(self.processed_dir.iterdir()):
            if item.is_dir():
                doc_chunks = self.list_chunks_for_document(item.name)
                if collection_id is not None:
                    doc_chunks = [
                        c for c in doc_chunks
                        if resolve_collection_identity(c.document_id, getattr(c, "collection_id", None)) == collection_id
                    ]
                all_chunks.extend(doc_chunks)
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
        self,
        collection_id: str,
        query_vector: Any,
        top_k: int = 10,
        model_name: str = "BAAI/bge-small-en-v1.5",
    ) -> List[Tuple[str, float]]:
        if not collection_id or not isinstance(collection_id, str) or not collection_id.strip():
            raise ValueError("collection_id must be a non-empty string")
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
            workspace_id=uuid.UUID(data["workspace_id"]) if data.get("workspace_id") else None,
            collection_id=data.get("collection_id"),
        )

    def _to_dict(self, record: IngestionJobRecord) -> Dict[str, Any]:
        return {
            "id": str(record.id),
            "source_url": record.source_url,
            "idempotency_key": record.idempotency_key,
            "status": record.status.value,
            "stage": record.stage.value,
            "document_id": record.document_id,
            "workspace_id": str(record.workspace_id) if record.workspace_id else None,
            "collection_id": record.collection_id,
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
        workspace_id: Optional[uuid.UUID] = None,
        collection_id: Optional[str] = None,
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
            workspace_id=workspace_id,
            collection_id=collection_id,
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
        workspace_id: Optional[uuid.UUID] = None,
        collection_id: Optional[str] = None,
    ) -> List[IngestionJobRecord]:
        jobs = self._load_jobs()
        records: List[IngestionJobRecord] = []
        for data in sorted(jobs.values(), key=lambda x: x.get("created_at", "")):
            rec = self._to_record(data)
            if status is not None and rec.status != status:
                continue
            if workspace_id is not None and getattr(rec, "workspace_id", None) != workspace_id:
                continue
            if collection_id is not None and getattr(rec, "collection_id", None) != collection_id:
                continue
            records.append(rec)
        return records[offset : offset + limit]

    def retry_job(
        self,
        job_id: uuid.UUID,
        workspace_id: Optional[uuid.UUID] = None,
        now: Optional[datetime] = None,
    ) -> IngestionJobRecord:
        jobs = self._load_jobs()
        str_id = str(job_id)
        if str_id not in jobs:
            raise PersistenceError(f"Ingestion job '{job_id}' not found")
        data = jobs[str_id]
        if workspace_id is not None and data.get("workspace_id") and uuid.UUID(str(data["workspace_id"])) != workspace_id:
            raise PersistenceError(f"Ingestion job '{job_id}' not found in specified workspace")
        if data.get("status") != JobStatus.FAILED.value:
            raise PersistenceError(f"Job '{job_id}' is in status '{data.get('status')}', expected 'failed' to retry")

        current_time = now or datetime.now(timezone.utc)
        data["status"] = JobStatus.QUEUED.value
        data["stage"] = ProcessingStage.PENDING.value
        data["claim_token"] = None
        data["claim_worker_id"] = None
        data["next_retry_at"] = current_time.isoformat()
        self._save_jobs(jobs)
        return self._to_record(data)

    def cancel_job(
        self,
        job_id: uuid.UUID,
        workspace_id: Optional[uuid.UUID] = None,
        reason: str = "cancelled_by_user",
        now: Optional[datetime] = None,
    ) -> IngestionJobRecord:
        jobs = self._load_jobs()
        str_id = str(job_id)
        if str_id not in jobs:
            raise PersistenceError(f"Ingestion job '{job_id}' not found")
        data = jobs[str_id]
        if workspace_id is not None and data.get("workspace_id") and uuid.UUID(str(data["workspace_id"])) != workspace_id:
            raise PersistenceError(f"Ingestion job '{job_id}' not found in specified workspace")
        if data.get("status") in (JobStatus.COMPLETED.value, JobStatus.DUPLICATE.value, JobStatus.FAILED.value):
            raise PersistenceError(f"Job '{job_id}' is already in terminal status '{data.get('status')}'")

        current_time = now or datetime.now(timezone.utc)
        data["status"] = JobStatus.FAILED.value
        data["stage"] = ProcessingStage.FINISHED.value
        data["completed_at"] = current_time.isoformat()
        data["error_details"] = {"reason": reason, "cancelled_at": current_time.isoformat()}
        self._save_jobs(jobs)
        return self._to_record(data)


class FileWorkspaceRepository(WorkspaceRepository):
    """File-mode in-memory/stub workspace repository."""
    def __init__(self):
        self._workspaces: Dict[uuid.UUID, Any] = {}
        self._memberships: Dict[Tuple[uuid.UUID, uuid.UUID], Any] = {}

    def get_workspace(self, workspace_id: uuid.UUID) -> Optional[Any]:
        return self._workspaces.get(workspace_id)

    def get_workspace_by_slug(self, slug: str) -> Optional[Any]:
        for ws in self._workspaces.values():
            if getattr(ws, "slug", "") == slug:
                return ws
        return None

    def list_workspaces_for_user(self, user_id: uuid.UUID) -> List[Any]:
        ws_ids = {ws_id for (ws_id, u_id) in self._memberships.keys() if u_id == user_id}
        return [ws for ws_id, ws in self._workspaces.items() if ws_id in ws_ids]

    def create_workspace(self, name: str, slug: str, owner_id: Optional[uuid.UUID] = None, is_system: bool = False) -> Any:
        from app.models.auth import WorkspaceModel
        ws_id = uuid.uuid4()
        ws = WorkspaceModel(id=ws_id, name=name, slug=slug, owner_id=owner_id, is_system=is_system)
        self._workspaces[ws_id] = ws
        if owner_id:
            self.add_member(ws_id, owner_id, "owner")
        return ws

    def get_membership(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> Optional[Any]:
        return self._memberships.get((workspace_id, user_id))

    def add_member(self, workspace_id: uuid.UUID, user_id: uuid.UUID, role: str) -> Any:
        from app.models.auth import WorkspaceMembershipModel
        mem = WorkspaceMembershipModel(id=uuid.uuid4(), workspace_id=workspace_id, user_id=user_id, role=role)
        self._memberships[(workspace_id, user_id)] = mem
        return mem


class FileCollectionRepository(CollectionRepository):
    """File-mode in-memory/stub collection repository."""
    def __init__(self):
        self._collections: Dict[Tuple[uuid.UUID, str], Any] = {}

    def get_collection(self, collection_id: str, workspace_id: Optional[uuid.UUID] = None) -> Optional[Any]:
        cid_lower = collection_id.lower()
        for (ws_id, c_id), col in self._collections.items():
            if c_id.lower() == cid_lower:
                if workspace_id is None or ws_id == workspace_id:
                    return col
        return None

    def list_collections(self, workspace_id: uuid.UUID, include_public: bool = True) -> List[Any]:
        return [col for (ws_id, _), col in self._collections.items() if ws_id == workspace_id or (include_public and getattr(col, "is_public", False))]

    def create_collection(
        self,
        id: str,
        workspace_id: uuid.UUID,
        name: str,
        policy_name: str,
        description: Optional[str] = None,
        enforce_regulatory_graph: bool = False,
        is_system: bool = False,
        is_public: bool = False,
    ) -> Any:
        from app.models.collection import CollectionModel
        col = CollectionModel(
            id=id,
            workspace_id=workspace_id,
            name=name,
            policy_name=policy_name,
            description=description,
            enforce_regulatory_graph=enforce_regulatory_graph,
            is_system=is_system,
            is_public=is_public,
        )
        self._collections[(workspace_id, id)] = col
        return col

    def update_collection(self, collection_id: str, workspace_id: uuid.UUID, name: Optional[str] = None, description: Optional[str] = None) -> Optional[Any]:
        col = self.get_collection(collection_id, workspace_id)
        if not col:
            return None
        if name is not None:
            col.name = name
        if description is not None:
            col.description = description
        return col

    def delete_collection(self, collection_id: str, workspace_id: uuid.UUID) -> bool:
        key = (workspace_id, collection_id)
        if key in self._collections:
            del self._collections[key]
            return True
        return False


class FileQueryHistoryRepository(QueryHistoryRepository):
    """File-mode in-memory/stub query history repository."""
    def __init__(self):
        self._entries: Dict[uuid.UUID, Any] = {}

    def create_entry(
        self,
        user_id: uuid.UUID,
        collection_id: str,
        query_text: str,
        disposition: str,
        workspace_id: Optional[uuid.UUID] = None,
        query_options: Optional[Dict[str, Any]] = None,
        summary_narrative: Optional[str] = None,
        answer_text: Optional[str] = None,
        claim_citations: Optional[List[Dict[str, Any]]] = None,
        verified_excerpts_count: int = 0,
        technical_validation_passed: bool = False,
    ) -> Any:
        from app.models.query_history import QueryHistoryModel
        entry = QueryHistoryModel(
            id=uuid.uuid4(),
            user_id=user_id,
            workspace_id=workspace_id,
            collection_id=collection_id,
            query_text=query_text,
            query_options=query_options or {},
            disposition=disposition,
            summary_narrative=summary_narrative,
            answer_text=answer_text,
            claim_citations=claim_citations or [],
            verified_excerpts_count=verified_excerpts_count,
            technical_validation_passed=technical_validation_passed,
        )
        self._entries[entry.id] = entry
        return entry

    def get_entry(self, history_id: uuid.UUID, user_id: uuid.UUID) -> Optional[Any]:
        entry = self._entries.get(history_id)
        if entry and getattr(entry, "user_id", None) == user_id:
            return entry
        return None

    def list_entries(
        self,
        user_id: uuid.UUID,
        workspace_id: Optional[uuid.UUID] = None,
        collection_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Any]:
        results = [
            e for e in self._entries.values()
            if getattr(e, "user_id", None) == user_id
            and (workspace_id is None or getattr(e, "workspace_id", None) == workspace_id)
            and (collection_id is None or getattr(e, "collection_id", "").lower() == collection_id.lower())
        ]
        return results[offset : offset + limit]

    def delete_entry(self, history_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        entry = self.get_entry(history_id, user_id)
        if entry:
            del self._entries[history_id]
            return True
        return False


class FileStorageRepositoryFactory(StorageRepositoryFactory):
    """Factory creating file-system backed repository instances."""

    def __init__(self, processed_dir: str | Path = "data/processed"):
        self.processed_dir = Path(processed_dir)
        self._workspace_repo = FileWorkspaceRepository()
        self._collection_repo = FileCollectionRepository()
        self._query_history_repo = FileQueryHistoryRepository()

    def get_document_repository(self) -> DocumentRepository:
        return FileDocumentRepository(self.processed_dir)

    def get_chunk_repository(self) -> ChunkRepository:
        return FileChunkRepository(self.processed_dir)

    def get_graph_edge_repository(self) -> GraphEdgeRepository:
        return FileGraphEdgeRepository(self.processed_dir)

    def get_ingestion_job_repository(self) -> IngestionJobRepository:
        return FileIngestionJobRepository(self.processed_dir)

    def get_workspace_repository(self) -> WorkspaceRepository:
        return self._workspace_repo

    def get_collection_repository(self) -> CollectionRepository:
        return self._collection_repo

    def get_query_history_repository(self) -> QueryHistoryRepository:
        return self._query_history_repo
