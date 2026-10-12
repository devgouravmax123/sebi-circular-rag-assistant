"""
Administrative REST API router for document registration, job inspection, and retries.
Path prefix: /api/v1/admin
"""
from datetime import datetime, timezone
import hmac
import logging
from typing import Optional, List, Dict, Any
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Security, status

from app.core.config import Settings, get_settings
from app.persistence.interfaces import (
    StorageRepositoryFactory,
    IngestionJobRepository,
    PersistenceError,
    ConnectionStorageError,
)
from app.persistence.postgres import PostgresStorageRepositoryFactory
from app.persistence.file_repo import FileStorageRepositoryFactory
from app.schemas.ingestion_job import (
    IngestionJobRecord,
    JobStatus,
    ProcessingStage,
)
from app.schemas.admin import (
    RegisterDocumentRequest,
    RegisterDocumentResponse,
    JobStatusResponse,
    JobListResponse,
    RetryJobResponse,
)
from app.schemas.graph import (
    GraphEdge,
    RelationshipType,
    ScopeType,
    ReviewStatus,
    ConfidenceLevel,
    EdgeProvenance,
    ProposeEdgeRequest,
    ReviewEdgeRequest,
    RevokeEdgeRequest,
    ReconcileBaselineEdgesRequest,
)
from app.schemas.cscrf_graph_data import VERIFIED_CSCRF_EDGES_DATA

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


def get_repository_factory(settings: Settings = Depends(get_settings)) -> StorageRepositoryFactory:
    """Dependency returning the active StorageRepositoryFactory (PostgreSQL or File)."""
    if settings.is_database_enabled and settings.database_url:
        return PostgresStorageRepositoryFactory(settings.database_url)
    return FileStorageRepositoryFactory(processed_dir=settings.processed_dir)


def verify_admin_key(
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    settings: Settings = Depends(get_settings),
) -> str:
    """
    Enforce administrative authentication via constant-time comparison against configured ADMIN_API_KEY.
    Fails safely with HTTP 500 if ADMIN_API_KEY is not configured in production.
    """
    configured_key = settings.admin_api_key
    if not configured_key or not configured_key.strip():
        # Fail safely: admin operations cannot be performed without configured secret
        logger.error("Admin authentication rejected: ADMIN_API_KEY is not configured on server")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Administrative authentication is not configured on the server.",
        )

    if not x_admin_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing required administrative header 'X-Admin-Key'.",
        )

    # Constant-time comparison to prevent timing side-channel attacks
    if not hmac.compare_digest(x_admin_key.strip(), configured_key.strip()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid administrative key.",
        )

    return x_admin_key


from app.retrieval.publisher import IndexPublisher
from app.schemas.publication import (
    IndexStatusResponse,
    PublicationResult,
)


def _to_status_response(
    job: IngestionJobRecord,
    publisher: Optional[IndexPublisher] = None,
) -> JobStatusResponse:
    # Determine honest searchability based on active index snapshot
    is_searchable = False
    pub_status = "not_published"
    pub_notice = (
        "Newly ingested documents are persisted in storage with pgvector embeddings, but not yet "
        "published to the active query index. Publication requires an administrative sync."
    )

    if publisher and job.document_id:
        if publisher.is_document_searchable(job.document_id, sha256=job.sha256):
            is_searchable = True
            pub_status = "published"
            pub_notice = "Document is published and searchable in the active query index."
        elif job.status == JobStatus.COMPLETED:
            pub_status = "pending_publication"
            pub_notice = "Document ingestion completed and records are stored, but search index publication is pending."
        elif job.status == JobStatus.FAILED:
            pub_status = "not_published"
            pub_notice = "Document ingestion failed; document is not published to search index."

    return JobStatusResponse(
        job_id=job.id,
        source_url=job.source_url,
        idempotency_key=job.idempotency_key,
        status=job.status,
        stage=job.stage,
        document_id=job.document_id,
        sha256=job.sha256,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        heartbeat_at=job.heartbeat_at,
        next_retry_at=job.next_retry_at,
        error_details=job.error_details,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        is_terminal=job.is_terminal,
        searchable=is_searchable,
        publication_status=pub_status,
        publication_notice=pub_notice,
    )


@router.post(
    "/documents/register",
    response_model=RegisterDocumentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def register_document_for_ingestion(
    payload: RegisterDocumentRequest,
    _admin_auth: str = Depends(verify_admin_key),
    factory: StorageRepositoryFactory = Depends(get_repository_factory),
) -> RegisterDocumentResponse:
    """
    Registers an official regulatory PDF URL for asynchronous ingestion.
    Enforces idempotency: repeated submissions with the same idempotency_key return the existing job.
    Does NOT execute downloading, parsing, or chunking synchronously inside the HTTP handler.
    """
    job_repo = factory.get_ingestion_job_repository()

    try:
        # Check if job already exists under idempotency key
        existing_job = job_repo.get_job_by_idempotency_key(payload.idempotency_key)
        if existing_job:
            return RegisterDocumentResponse(
                job_id=existing_job.id,
                idempotency_key=existing_job.idempotency_key,
                source_url=existing_job.source_url,
                document_id=existing_job.document_id,
                status=existing_job.status,
                stage=existing_job.stage,
                is_duplicate_registration=True,
                message=f"Job already registered under idempotency key '{payload.idempotency_key}'.",
                created_at=existing_job.created_at,
            )

        # Create new queued job
        job = job_repo.create_job(
            source_url=payload.source_url,
            idempotency_key=payload.idempotency_key,
            document_id=payload.document_id,
            max_attempts=payload.max_attempts,
        )

        return RegisterDocumentResponse(
            job_id=job.id,
            idempotency_key=job.idempotency_key,
            source_url=job.source_url,
            document_id=job.document_id,
            status=job.status,
            stage=job.stage,
            is_duplicate_registration=False,
            message="Document successfully queued for background ingestion.",
            created_at=job.created_at,
        )
    except ConnectionStorageError as e:
        logger.error(f"Storage connection error during document registration: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage backend is temporarily unavailable.",
        )
    except PersistenceError as e:
        logger.error(f"Persistence error during document registration: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal error registering document for ingestion.",
        )


def get_index_publisher(
    factory: StorageRepositoryFactory = Depends(get_repository_factory),
    settings: Settings = Depends(get_settings),
) -> IndexPublisher:
    """Dependency provider for IndexPublisher."""
    return IndexPublisher(
        repository_factory=factory,
        indexes_dir=settings.indexes_dir,
        processed_dir=settings.processed_dir,
    )


@router.get(
    "/jobs/{job_id}/status",
    response_model=JobStatusResponse,
)
def get_job_status(
    job_id: uuid.UUID,
    _admin_auth: str = Depends(verify_admin_key),
    factory: StorageRepositoryFactory = Depends(get_repository_factory),
    publisher: IndexPublisher = Depends(get_index_publisher),
) -> JobStatusResponse:
    """Retrieve detailed state, stage, and retry status for a specific ingestion job."""
    job_repo = factory.get_ingestion_job_repository()
    try:
        job = job_repo.get_job(job_id)
        if not job:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Ingestion job '{job_id}' not found.",
            )
        return _to_status_response(job, publisher=publisher)
    except HTTPException:
        raise
    except ConnectionStorageError as e:
        logger.error(f"Storage connection error in get_job_status: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage backend is temporarily unavailable.",
        )
    except Exception as e:
        logger.error(f"Unexpected error in get_job_status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal error retrieving job status.",
        )


@router.get(
    "/jobs",
    response_model=JobListResponse,
)
def list_jobs(
    status_filter: Optional[JobStatus] = Query(None, alias="status", description="Filter by status"),
    limit: int = Query(50, ge=1, le=100, description="Max jobs to return (bounded to 100)"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    _admin_auth: str = Depends(verify_admin_key),
    factory: StorageRepositoryFactory = Depends(get_repository_factory),
    publisher: IndexPublisher = Depends(get_index_publisher),
) -> JobListResponse:
    """List recent ingestion jobs with bounded pagination and optional status filter."""
    job_repo = factory.get_ingestion_job_repository()
    try:
        jobs = job_repo.list_jobs(status=status_filter, limit=limit, offset=offset)
        return JobListResponse(
            total=len(jobs),
            limit=limit,
            offset=offset,
            jobs=[_to_status_response(j, publisher=publisher) for j in jobs],
        )

    except ConnectionStorageError as e:
        logger.error(f"Storage connection error in list_jobs: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage backend is temporarily unavailable.",
        )
    except Exception as e:
        logger.error(f"Unexpected error in list_jobs: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal error listing jobs.",
        )


@router.post(
    "/jobs/{job_id}/retry",
    response_model=RetryJobResponse,
)
def retry_failed_job(
    job_id: uuid.UUID,
    _admin_auth: str = Depends(verify_admin_key),
    factory: StorageRepositoryFactory = Depends(get_repository_factory),
) -> RetryJobResponse:
    """
    Manually schedule a deliberate retry for an eligible failed job.
    Validates that the job is currently in 'failed' status and resets it to 'queued'.
    Rejects jobs that are completed, duplicate, or currently processing.
    """
    job_repo = factory.get_ingestion_job_repository()
    try:
        job = job_repo.get_job(job_id)
        if not job:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Ingestion job '{job_id}' not found.",
            )

        if job.status != JobStatus.FAILED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Job '{job_id}' is in status '{job.status.value}'. "
                    "Only jobs in 'failed' status may be retried."
                ),
            )

        # Reset job to queued with claim lease cleared and retry time set to immediate
        now = datetime.now(timezone.utc)
        if hasattr(job_repo, "session_factory"):
            # PostgreSQL implementation
            from sqlalchemy import select
            from app.persistence.models import IngestionJobModel
            with job_repo.session_factory() as session:
                with session.begin():
                    m = session.execute(
                        select(IngestionJobModel).where(IngestionJobModel.id == job_id)
                    ).scalar_one()
                    m.status = JobStatus.QUEUED.value
                    m.stage = ProcessingStage.PENDING.value
                    m.claim_token = None
                    m.claim_worker_id = None
                    m.next_retry_at = now
                    session.flush()
        else:
            # File repo implementation
            from app.persistence.file_repo import FileIngestionJobRepository
            if isinstance(job_repo, FileIngestionJobRepository):
                jobs = job_repo._load_jobs()
                data = jobs.get(str(job_id))
                if data:
                    data["status"] = JobStatus.QUEUED.value
                    data["stage"] = ProcessingStage.PENDING.value
                    data["claim_token"] = None
                    data["claim_worker_id"] = None
                    data["next_retry_at"] = now.isoformat()
                    job_repo._save_jobs(jobs)

        updated = job_repo.get_job(job_id)
        if not updated:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Job '{job_id}' not found after retry update.",
            )
        return RetryJobResponse(
            job_id=job_id,
            status=updated.status,
            message="Job successfully returned to queue for processing.",
            attempt_count=updated.attempt_count,
            next_retry_at=updated.next_retry_at,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in retry_failed_job: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal error scheduling job retry.",
        )


@router.get(
    "/indexes/status",
    response_model=IndexStatusResponse,
)
def get_index_publication_status(
    _admin_auth: str = Depends(verify_admin_key),
    publisher: IndexPublisher = Depends(get_index_publisher),
) -> IndexStatusResponse:
    """Inspect active published search index snapshot and pending un-indexed documents."""
    return publisher.get_index_status()


@router.post(
    "/indexes/publish",
    response_model=PublicationResult,
)
def publish_search_index(
    dry_run: bool = Query(False, description="If true, builds and validates candidate index without activating it"),
    _admin_auth: str = Depends(verify_admin_key),
    publisher: IndexPublisher = Depends(get_index_publisher),
) -> PublicationResult:
    """
    Builds, validates, and atomically activates a search index snapshot across
    all registered documents in the authoritative repository.
    """
    result = publisher.build_and_publish_snapshot(dry_run=dry_run)
    if not result.success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result.message,
        )
    return result


# ---------------------------------------------------------------------------
# Regulatory Relationship Management Endpoints (Phase 8D)
# ---------------------------------------------------------------------------

@router.get(
    "/edges",
    response_model=List[GraphEdge],
)
def list_regulatory_edges(
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by review status"),
    relationship_filter: Optional[str] = Query(None, alias="relationship", description="Filter by relationship type"),
    source_document_id: Optional[str] = Query(None, description="Filter by source document ID"),
    target_document_id: Optional[str] = Query(None, description="Filter by target document ID"),
    limit: int = Query(50, ge=1, le=200, description="Page limit"),
    offset: int = Query(0, ge=0, description="Page offset"),
    _admin_auth: str = Depends(verify_admin_key),
    factory: StorageRepositoryFactory = Depends(get_repository_factory),
) -> List[GraphEdge]:
    """
    List regulatory relationship edges with bounded pagination and optional filtering.
    """
    edge_repo = factory.get_graph_edge_repository()
    return edge_repo.list_edges(
        status=status_filter,
        relationship=relationship_filter,
        source_document_id=source_document_id,
        target_document_id=target_document_id,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/edges/propose",
    response_model=GraphEdge,
    status_code=status.HTTP_201_CREATED,
)
def propose_regulatory_edge(
    req: ProposeEdgeRequest,
    _admin_auth: str = Depends(verify_admin_key),
    factory: StorageRepositoryFactory = Depends(get_repository_factory),
) -> GraphEdge:
    """
    Propose a new candidate regulatory relationship.
    Initial review status is strictly PROPOSED (untrusted) until verified.
    """
    edge_repo = factory.get_graph_edge_repository()

    edge_id = req.edge_id or f"edge_{req.source_document_id}_{req.relationship.value.lower()}_{req.target_document_id}_{uuid.uuid4().hex[:8]}"

    edge = GraphEdge(
        edge_id=edge_id,
        source_document_id=req.source_document_id,
        target_document_id=req.target_document_id,
        relationship=req.relationship,
        scope=req.scope,
        affected_provisions=req.affected_provisions,
        evidence_text=req.evidence_text,
        evidence_page=req.evidence_page,
        effective_date=req.effective_date,
        confidence=req.confidence,
        review_status=ReviewStatus.PROPOSED,
        provenance=EdgeProvenance.USER_PROPOSED,
        reviewed_by=req.proposer_id,
        review_rationale=req.rationale,
    )

    try:
        return edge_repo.propose_edge(edge)
    except PersistenceError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )


@router.get(
    "/edges/{edge_id}",
    response_model=GraphEdge,
)
def get_regulatory_edge(
    edge_id: str,
    _admin_auth: str = Depends(verify_admin_key),
    factory: StorageRepositoryFactory = Depends(get_repository_factory),
) -> GraphEdge:
    """
    Inspect a single regulatory edge, its textual evidence, page provenance, and full review audit history.
    """
    edge_repo = factory.get_graph_edge_repository()
    edge = edge_repo.get_edge(edge_id)
    if not edge:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Regulatory edge '{edge_id}' not found",
        )
    return edge


@router.post(
    "/edges/{edge_id}/review",
    response_model=GraphEdge,
)
def review_regulatory_edge(
    edge_id: str,
    req: ReviewEdgeRequest,
    _admin_auth: str = Depends(verify_admin_key),
    factory: StorageRepositoryFactory = Depends(get_repository_factory),
) -> GraphEdge:
    """
    Submit a human review decision (verify or reject) for a relationship edge.
    Records reviewer identity, decision timestamp, and regulatory rationale into the immutable audit trail.
    """
    edge_repo = factory.get_graph_edge_repository()
    try:
        return edge_repo.review_edge(
            edge_id=edge_id,
            decision=req.decision,
            reviewer_id=req.reviewer_id,
            reviewer_name=req.reviewer_name,
            rationale=req.rationale,
        )
    except PersistenceError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.post(
    "/edges/{edge_id}/revoke",
    response_model=GraphEdge,
)
def revoke_regulatory_edge(
    edge_id: str,
    req: RevokeEdgeRequest,
    _admin_auth: str = Depends(verify_admin_key),
    factory: StorageRepositoryFactory = Depends(get_repository_factory),
) -> GraphEdge:
    """
    Revoke a previously verified relationship.
    State changes to REVOKED and immutable audit rationale is captured.
    """
    edge_repo = factory.get_graph_edge_repository()
    try:
        return edge_repo.revoke_edge(
            edge_id=edge_id,
            reviewer_id=req.reviewer_id,
            reviewer_name=req.reviewer_name,
            rationale=req.rationale,
        )
    except PersistenceError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.post(
    "/edges/reconcile-baseline",
)
def reconcile_baseline_edges(
    req: ReconcileBaselineEdgesRequest,
    _admin_auth: str = Depends(verify_admin_key),
    factory: StorageRepositoryFactory = Depends(get_repository_factory),
) -> Dict[str, Any]:
    """
    Idempotently reconcile baseline regulatory edges (e.g. 9 CSCRF verified edges)
    into storage without destructive overwriting.
    """
    edge_repo = factory.get_graph_edge_repository()
    baseline_edges = [GraphEdge(**e) for e in VERIFIED_CSCRF_EDGES_DATA]
    return edge_repo.reconcile_baseline_edges(
        baseline_edges=baseline_edges,
        allow_overwrite=req.allow_overwrite,
    )
