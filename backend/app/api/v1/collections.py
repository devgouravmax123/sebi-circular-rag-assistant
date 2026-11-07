"""
REST API routes for Collection management (Milestone 8 - Task 8.3).
Path prefixes:
  - /api/v1/workspaces/{workspace_id}/collections (Workspace-scoped operations)
  - /api/v1/collections (Direct collection lookup and mutation)
"""
import logging
from typing import List, Optional, Dict, Any, Set
import uuid

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func
from pathlib import Path

from app.core.config import get_settings
from app.api.deps import get_db_session, get_current_user
from app.api.workspace_deps import (
    WorkspaceAuthContext,
    require_workspace_reader,
    require_workspace_contributor,
    require_workspace_admin,
    SYSTEM_WORKSPACE_ID,
)
from app.models.auth import UserModel, WorkspaceModel, WorkspaceMembershipModel
from app.models.collection import CollectionModel
from app.persistence.models import RegulatoryDocumentModel, IngestionJobModel, DocumentVersionModel
from app.schemas.workspace import (
    CollectionCreateRequest,
    CollectionUpdateRequest,
    CollectionDetailResponse,
)
from app.schemas.collection_ingestion import (
    CollectionIngestionJobCreateRequest,
    CollectionIngestionJobResponse,
    CollectionIngestionJobStatusResponse,
    CollectionIngestionJobListResponse,
    CollectionDocumentSummaryResponse,
    CollectionDocumentListResponse,
    sanitize_error_message,
    sanitize_error_details,
)
from app.storage import (
    BaseStorageAdapter,
    LocalStorageAdapter,
    get_storage_adapter,
    StorageLimitExceededError,
    InvalidFileFormatError,
    PathTraversalError,
)
from app.retrieval.publisher import IndexPublisher
from app.persistence.file_repo import FileStorageRepositoryFactory
from app.persistence.postgres import PostgresStorageRepositoryFactory

logger = logging.getLogger(__name__)

workspace_collections_router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}/collections", tags=["collections"])
collections_router = APIRouter(prefix="/api/v1/collections", tags=["collections"])


# -------------------------------------------------------------------------
# Workspace-Scoped Collection Endpoints
# -------------------------------------------------------------------------

@workspace_collections_router.post(
    "",
    response_model=CollectionDetailResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new collection in workspace",
)
def create_collection(
    payload: CollectionCreateRequest,
    auth_ctx: WorkspaceAuthContext = Depends(require_workspace_admin),
    db: Session = Depends(get_db_session),
) -> CollectionDetailResponse:
    """
    Creates a new user collection inside the authorized workspace.
    Requires workspace owner or admin role.
    """
    # Check if collection id already exists in this workspace
    existing = (
        db.query(CollectionModel)
        .filter(
            CollectionModel.workspace_id == auth_ctx.workspace.id,
            func.lower(CollectionModel.id) == payload.id,
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A collection with this ID already exists in this workspace.",
        )

    # System collection ID collision check
    sys_existing = (
        db.query(CollectionModel)
        .filter(
            CollectionModel.workspace_id == SYSTEM_WORKSPACE_ID,
            func.lower(CollectionModel.id) == payload.id,
        )
        .first()
    )
    if sys_existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Collection ID conflicts with a system collection.",
        )

    col = CollectionModel(
        id=payload.id,
        workspace_id=auth_ctx.workspace.id,
        name=payload.name,
        description=payload.description,
        policy_name=payload.policy_name,
        enforce_regulatory_graph=payload.enforce_regulatory_graph,
        is_system=False,
        is_public=False,
    )

    db.add(col)
    try:
        db.commit()
        db.refresh(col)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A collection with this ID already exists.",
        )

    logger.info(f"Collection created: {col.id} in ws={col.workspace_id} by user={auth_ctx.user.id}")
    return CollectionDetailResponse.model_validate(col)


@workspace_collections_router.get(
    "",
    response_model=List[CollectionDetailResponse],
    status_code=status.HTTP_200_OK,
    summary="List collections in workspace",
)
def list_workspace_collections(
    auth_ctx: WorkspaceAuthContext = Depends(require_workspace_reader),
    db: Session = Depends(get_db_session),
) -> List[CollectionDetailResponse]:
    """Returns all private collections belonging strictly to the authorized workspace."""
    cols = (
        db.query(CollectionModel)
        .filter(CollectionModel.workspace_id == auth_ctx.workspace.id)
        .order_by(CollectionModel.created_at.asc())
        .all()
    )
    return [CollectionDetailResponse.model_validate(c) for c in cols]


@workspace_collections_router.post(
    "/{collection_id}/ingestion-jobs",
    response_model=CollectionIngestionJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a document for ingestion into an authorized private collection",
)
def create_collection_ingestion_job(
    collection_id: str,
    payload: CollectionIngestionJobCreateRequest,
    auth_ctx: WorkspaceAuthContext = Depends(require_workspace_admin),
    db: Session = Depends(get_db_session),
) -> CollectionIngestionJobResponse:
    """
    Submits an official document URL for asynchronous background ingestion into an authorized private collection.
    Enforces:
    - Caller must be workspace owner or admin.
    - Collection must belong to the authorized workspace.
    - System/demo collections are strictly rejected from private user ingestion.
    - Job and document are atomically assigned to the collection's workspace and collection_id.
    - Idempotency: repeated submissions with the same idempotency key return the existing job.
    """
    col = (
        db.query(CollectionModel)
        .filter(
            CollectionModel.workspace_id == auth_ctx.workspace.id,
            func.lower(CollectionModel.id) == collection_id.lower(),
        )
        .first()
    )
    if not col:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found in workspace.",
        )

    if col.is_system or col.is_public:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="System collections cannot be ingested via user endpoints.",
        )

    # Check for existing job by idempotency key
    existing_job = (
        db.query(IngestionJobModel)
        .filter(IngestionJobModel.idempotency_key == payload.idempotency_key)
        .first()
    )
    if existing_job:
        # Cross-tenant idempotency conflict check
        if existing_job.workspace_id != col.workspace_id or existing_job.collection_id != col.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency key conflict.",
            )
        return CollectionIngestionJobResponse(
            job_id=existing_job.id,
            workspace_id=existing_job.workspace_id,
            collection_id=existing_job.collection_id,
            document_id=existing_job.document_id,
            source_url=existing_job.source_url,
            status=existing_job.status,
            stage=existing_job.stage,
            idempotency_key=existing_job.idempotency_key,
            is_duplicate_registration=True,
            message=f"Job already registered under idempotency key '{payload.idempotency_key}'.",
            created_at=existing_job.created_at,
        )

    # Determine or generate document ID
    doc_id = payload.document_id
    if not doc_id:
        doc_id = f"doc_{uuid.uuid4().hex[:12]}"

    # Ensure document record exists with matching workspace and collection ownership
    doc = db.query(RegulatoryDocumentModel).filter(RegulatoryDocumentModel.id == doc_id).first()
    if not doc:
        doc = RegulatoryDocumentModel(
            id=doc_id,
            domain_id="SEBI_CSCRF",  # default taxonomy domain
            title=payload.title or f"Ingested Document {doc_id}",
            circular_number=payload.circular_number,
            workspace_id=col.workspace_id,
            collection_id=col.id,
        )
        db.add(doc)
        db.flush()
    else:
        # Document already exists: assert strict ownership match
        if doc.workspace_id != col.workspace_id or doc.collection_id != col.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Document ID already belongs to another collection or workspace.",
            )

    job_id = uuid.uuid4()
    job = IngestionJobModel(
        id=job_id,
        source_url=payload.source_url,
        idempotency_key=payload.idempotency_key,
        document_id=doc.id,
        workspace_id=col.workspace_id,
        collection_id=col.id,
        status="queued",
        stage="pending",
        attempt_count=0,
        max_attempts=payload.max_attempts,
    )

    db.add(job)
    try:
        db.commit()
        db.refresh(job)
    except IntegrityError as e:
        db.rollback()
        logger.error(f"IntegrityError creating collection ingestion job: {e}")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Job registration conflict.",
        )

    logger.info(f"Queued ingestion job {job.id} for doc={doc.id} col={col.id} ws={col.workspace_id}")
    return CollectionIngestionJobResponse(
        job_id=job.id,
        workspace_id=job.workspace_id,
        collection_id=job.collection_id,
        document_id=job.document_id,
        source_url=job.source_url,
        status=job.status,
        stage=job.stage,
        idempotency_key=job.idempotency_key,
        is_duplicate_registration=False,
        message="Document successfully queued for background ingestion.",
        created_at=job.created_at,
    )


def _get_index_publisher(collection_id: str) -> IndexPublisher:
    settings = get_settings()
    if settings.is_database_enabled and settings.database_url:
        factory = PostgresStorageRepositoryFactory(settings.database_url)
    else:
        factory = FileStorageRepositoryFactory(processed_dir=settings.processed_dir)
    return IndexPublisher(
        repository_factory=factory,
        indexes_dir=settings.indexes_dir,
        processed_dir=settings.processed_dir,
        collection_id=collection_id,
        storage_adapter=get_storage_adapter(settings),
    )


def _to_job_status_response(job: IngestionJobModel, published_doc_ids: set) -> CollectionIngestionJobStatusResponse:
    err_msg = None
    if isinstance(job.error_details, dict):
        raw_msg = job.error_details.get("reason") or job.error_details.get("message")
        err_msg = sanitize_error_message(raw_msg, job.collection_id)
    elif job.error_details is not None:
        err_msg = sanitize_error_message(str(job.error_details), job.collection_id)

    is_published = bool(job.document_id and job.document_id in published_doc_ids)

    return CollectionIngestionJobStatusResponse(
        job_id=job.id,
        workspace_id=job.workspace_id,
        collection_id=job.collection_id,
        document_id=job.document_id,
        status=job.status,
        stage=job.stage or "pending",
        idempotency_key=job.idempotency_key,
        is_terminal=job.status in ("completed", "failed", "duplicate"),
        is_published=is_published,
        error_message=err_msg,
        attempt_count=job.attempt_count or 0,
        created_at=job.created_at,
        updated_at=job.heartbeat_at or job.started_at,
        completed_at=job.completed_at,
    )


@workspace_collections_router.post(
    "/{collection_id}/upload",
    response_model=CollectionIngestionJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a private document PDF for background ingestion into an authorized collection",
)
async def upload_collection_document(
    workspace_id: uuid.UUID,
    collection_id: str,
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    circular_number: Optional[str] = Form(None),
    auth_ctx: WorkspaceAuthContext = Depends(require_workspace_contributor),
    db: Session = Depends(get_db_session),
) -> CollectionIngestionJobResponse:
    """
    Accepts an uploaded PDF document, stores it in durable tenant-scoped storage,
    and enqueues an ingestion job for background worker processing.
    """
    col = (
        db.query(CollectionModel)
        .filter(
            CollectionModel.workspace_id == auth_ctx.workspace.id,
            func.lower(CollectionModel.id) == collection_id.lower(),
        )
        .first()
    )
    if not col:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found in workspace.",
        )

    if col.is_system or col.is_public:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="System collections cannot be modified via upload.",
        )

    # Validate file extension
    filename = file.filename or ""
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only PDF documents (.pdf) are supported.",
        )

    # Persist uploaded file via storage adapter
    settings = get_settings()
    storage_adapter = get_storage_adapter(settings)
    doc_id = f"doc_{uuid.uuid4().hex[:12]}"

    try:
        save_res = storage_adapter.save_upload_stream(
            workspace_id=col.workspace_id,
            collection_id=col.id,
            document_id=doc_id,
            stream=file.file,
            original_filename=filename,
        )
    except InvalidFileFormatError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except StorageLimitExceededError as e:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Error persisting uploaded PDF: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to store uploaded document.",
        )

    # Deterministic duplicate check by SHA-256 within this collection
    existing_job = (
        db.query(IngestionJobModel)
        .filter(
            IngestionJobModel.workspace_id == col.workspace_id,
            IngestionJobModel.collection_id == col.id,
            IngestionJobModel.sha256 == save_res.sha256,
            IngestionJobModel.status.in_(["queued", "processing", "completed"]),
        )
        .first()
    )
    if existing_job:
        # Fix 3 (Milestone 10.2): Clean up redundant upload artifact on duplicate detection
        storage_adapter.delete_file(save_res.source_uri)
        return CollectionIngestionJobResponse(
            job_id=existing_job.id,
            workspace_id=existing_job.workspace_id,
            collection_id=existing_job.collection_id,
            document_id=existing_job.document_id,
            source_url=existing_job.source_url,
            status=existing_job.status,
            stage=existing_job.stage,
            idempotency_key=existing_job.idempotency_key or "",
            is_duplicate_registration=True,
            message=f"An identical document (SHA-256 {save_res.sha256[:12]}...) is already registered in this collection.",
            created_at=existing_job.created_at,
        )

    doc_title = title.strip() if title and title.strip() else (filename or f"Uploaded Document {doc_id}")
    doc = RegulatoryDocumentModel(
        id=doc_id,
        domain_id="SEBI_CSCRF",
        title=doc_title,
        circular_number=circular_number.strip() if circular_number and circular_number.strip() else None,
        workspace_id=col.workspace_id,
        collection_id=col.id,
    )
    db.add(doc)
    db.flush()

    job_id = uuid.uuid4()
    job = IngestionJobModel(
        id=job_id,
        source_url=save_res.source_uri,
        idempotency_key=f"upload_{save_res.sha256[:16]}_{job_id.hex[:8]}",
        document_id=doc.id,
        workspace_id=col.workspace_id,
        collection_id=col.id,
        status="queued",
        stage="pending",
        sha256=save_res.sha256,
        attempt_count=0,
        max_attempts=3,
    )
    db.add(job)
    try:
        db.commit()
        db.refresh(job)
    except IntegrityError as e:
        db.rollback()
        storage_adapter.delete_file(save_res.source_uri)
        logger.error(f"IntegrityError creating upload ingestion job: {e}")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document upload registration conflict.",
        )

    logger.info(f"Uploaded and queued job {job.id} for doc={doc.id} col={col.id} ws={col.workspace_id}")
    return CollectionIngestionJobResponse(
        job_id=job.id,
        workspace_id=job.workspace_id,
        collection_id=job.collection_id,
        document_id=job.document_id,
        source_url=job.source_url,
        status=job.status,
        stage=job.stage,
        idempotency_key=job.idempotency_key,
        is_duplicate_registration=False,
        message="Document successfully uploaded and queued for background ingestion.",
        created_at=job.created_at,
    )


@workspace_collections_router.get(
    "/{collection_id}/ingestion-jobs",
    response_model=CollectionIngestionJobListResponse,
    status_code=status.HTTP_200_OK,
    summary="List ingestion jobs in an authorized collection",
)
def list_collection_ingestion_jobs(
    workspace_id: uuid.UUID,
    collection_id: str,
    auth_ctx: WorkspaceAuthContext = Depends(require_workspace_reader),
    db: Session = Depends(get_db_session),
) -> CollectionIngestionJobListResponse:
    col = (
        db.query(CollectionModel)
        .filter(
            CollectionModel.workspace_id == auth_ctx.workspace.id,
            func.lower(CollectionModel.id) == collection_id.lower(),
        )
        .first()
    )
    if not col:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found in workspace.",
        )

    jobs = (
        db.query(IngestionJobModel)
        .filter(
            IngestionJobModel.workspace_id == auth_ctx.workspace.id,
            func.lower(IngestionJobModel.collection_id) == collection_id.lower(),
        )
        .order_by(IngestionJobModel.created_at.desc())
        .all()
    )

    try:
        pub = _get_index_publisher(col.id)
        manifest = pub.get_active_manifest()
        published_ids = set(manifest.document_ids) if manifest else set()
    except Exception:
        published_ids = set()

    status_items = [_to_job_status_response(j, published_ids) for j in jobs]
    return CollectionIngestionJobListResponse(jobs=status_items, total=len(status_items))


@workspace_collections_router.get(
    "/{collection_id}/ingestion-jobs/{job_id}",
    response_model=CollectionIngestionJobStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Get single ingestion job status in an authorized collection",
)
def get_collection_ingestion_job(
    workspace_id: uuid.UUID,
    collection_id: str,
    job_id: uuid.UUID,
    auth_ctx: WorkspaceAuthContext = Depends(require_workspace_reader),
    db: Session = Depends(get_db_session),
) -> CollectionIngestionJobStatusResponse:
    col = (
        db.query(CollectionModel)
        .filter(
            CollectionModel.workspace_id == auth_ctx.workspace.id,
            func.lower(CollectionModel.id) == collection_id.lower(),
        )
        .first()
    )
    if not col:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found in workspace.",
        )

    job = (
        db.query(IngestionJobModel)
        .filter(
            IngestionJobModel.id == job_id,
            IngestionJobModel.workspace_id == auth_ctx.workspace.id,
            func.lower(IngestionJobModel.collection_id) == collection_id.lower(),
        )
        .first()
    )
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ingestion job not found.",
        )

    try:
        pub = _get_index_publisher(col.id)
        manifest = pub.get_active_manifest()
        published_ids = set(manifest.document_ids) if manifest else set()
    except Exception:
        published_ids = set()

    return _to_job_status_response(job, published_ids)


@workspace_collections_router.get(
    "/{collection_id}/documents",
    response_model=CollectionDocumentListResponse,
    status_code=status.HTTP_200_OK,
    summary="List documents belonging to an authorized collection",
)
def list_collection_documents(
    workspace_id: uuid.UUID,
    collection_id: str,
    auth_ctx: WorkspaceAuthContext = Depends(require_workspace_reader),
    db: Session = Depends(get_db_session),
) -> CollectionDocumentListResponse:
    col = (
        db.query(CollectionModel)
        .filter(
            CollectionModel.workspace_id == auth_ctx.workspace.id,
            func.lower(CollectionModel.id) == collection_id.lower(),
        )
        .first()
    )
    if not col:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found in workspace.",
        )

    docs = (
        db.query(RegulatoryDocumentModel)
        .filter(
            RegulatoryDocumentModel.workspace_id == auth_ctx.workspace.id,
            func.lower(RegulatoryDocumentModel.collection_id) == collection_id.lower(),
        )
        .order_by(RegulatoryDocumentModel.created_at.desc())
        .all()
    )

    try:
        pub = _get_index_publisher(col.id)
        manifest = pub.get_active_manifest()
        published_ids = set(manifest.document_ids) if manifest else set()
    except Exception:
        published_ids = set()

    doc_summaries = [
        CollectionDocumentSummaryResponse(
            document_id=d.id,
            workspace_id=d.workspace_id,
            collection_id=d.collection_id,
            title=d.title,
            circular_number=d.circular_number,
            is_published=d.id in published_ids,
            created_at=d.created_at,
        )
        for d in docs
    ]
    return CollectionDocumentListResponse(documents=doc_summaries, total=len(doc_summaries))


@workspace_collections_router.get(
    "/{collection_id}/documents/{document_id}/download",
    status_code=status.HTTP_200_OK,
    summary="Download source PDF of an authorized collection document",
)
def download_collection_document(
    workspace_id: uuid.UUID,
    collection_id: str,
    document_id: str,
    auth_ctx: WorkspaceAuthContext = Depends(require_workspace_reader),
    db: Session = Depends(get_db_session),
):
    col = (
        db.query(CollectionModel)
        .filter(
            CollectionModel.workspace_id == auth_ctx.workspace.id,
            func.lower(CollectionModel.id) == collection_id.lower(),
        )
        .first()
    )
    if not col:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found in workspace.",
        )

    doc = (
        db.query(RegulatoryDocumentModel)
        .filter(
            RegulatoryDocumentModel.id == document_id,
            RegulatoryDocumentModel.workspace_id == auth_ctx.workspace.id,
            func.lower(RegulatoryDocumentModel.collection_id) == collection_id.lower(),
        )
        .first()
    )
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found in collection.",
        )

    # Locate document source in configured storage provider
    settings = get_settings()
    storage_adapter = get_storage_adapter(settings)

    candidate_uri: Optional[str] = None
    # 1. Standard upload URI
    std_uri = f"upload://{col.workspace_id}/{col.id}/{doc.id}.pdf"
    if storage_adapter.exists(std_uri):
        candidate_uri = std_uri
    else:
        # 2. Check latest ingestion job source_url
        job = (
            db.query(IngestionJobModel)
            .filter(
                IngestionJobModel.workspace_id == col.workspace_id,
                IngestionJobModel.collection_id == col.id,
                IngestionJobModel.document_id == doc.id,
            )
            .order_by(IngestionJobModel.created_at.desc())
            .first()
        )
        if job and job.source_url and storage_adapter.exists(job.source_url):
            candidate_uri = job.source_url
        else:
            # 3. Check DocumentVersionModel storage_path
            ver = (
                db.query(DocumentVersionModel)
                .filter(DocumentVersionModel.document_id == doc.id)
                .order_by(DocumentVersionModel.version_number.desc())
                .first()
            )
            if ver and ver.storage_path and storage_adapter.exists(ver.storage_path):
                candidate_uri = ver.storage_path

    if not candidate_uri:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Source document file artifact is not available in storage.",
        )

    def stream_generator():
        with storage_adapter.open_stream(candidate_uri) as stream:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                yield chunk

    safe_filename = f"{doc.id}.pdf"
    return StreamingResponse(
        stream_generator(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{safe_filename}"'},
    )


# -------------------------------------------------------------------------
# Direct Collection Endpoints (Lookup & Mutation)
# -------------------------------------------------------------------------

@collections_router.get(
    "/{collection_id}",
    response_model=CollectionDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Get collection metadata by ID",
)
def get_collection(
    collection_id: str,
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db_session),
) -> CollectionDetailResponse:
    """
    Returns collection metadata.
    System/public collections are globally readable by any authenticated user.
    Private collections require active membership in the owning workspace.
    Fails closed with 404 for unauthorized private collections to prevent enumeration.
    """
    col = db.query(CollectionModel).filter(CollectionModel.id == collection_id).first()
    if not col:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found.",
        )

    # System collections or public collections are globally accessible
    if col.is_system or col.is_public:
        return CollectionDetailResponse.model_validate(col)

    # For private collections, verify user has membership in owning workspace
    mem = (
        db.query(WorkspaceMembershipModel)
        .filter(
            WorkspaceMembershipModel.workspace_id == col.workspace_id,
            WorkspaceMembershipModel.user_id == user.id,
        )
        .first()
    )
    if not mem:
        logger.warning(f"Unauthorized collection access attempt: user={user.id} col={collection_id}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found.",
        )

    return CollectionDetailResponse.model_validate(col)


@collections_router.patch(
    "/{collection_id}",
    response_model=CollectionDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Update collection metadata",
)
def update_collection(
    collection_id: str,
    payload: CollectionUpdateRequest,
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db_session),
) -> CollectionDetailResponse:
    """
    Updates collection metadata (name, description).
    System collections are strictly immutable and cannot be updated.
    Private collections require owner or admin role in the owning workspace.
    """
    col = db.query(CollectionModel).filter(CollectionModel.id == collection_id).first()
    if not col:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found.",
        )

    if col.is_system:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="System collections are immutable and cannot be modified.",
        )

    mem = (
        db.query(WorkspaceMembershipModel)
        .filter(
            WorkspaceMembershipModel.workspace_id == col.workspace_id,
            WorkspaceMembershipModel.user_id == user.id,
        )
        .first()
    )
    if not mem or mem.role not in ("owner", "admin"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND if not mem else status.HTTP_403_FORBIDDEN,
            detail="Collection not found." if not mem else "Insufficient permissions to update collection.",
        )

    if payload.name is not None:
        col.name = payload.name
    if payload.description is not None:
        col.description = payload.description

    db.commit()
    db.refresh(col)

    logger.info(f"Collection updated: {col.id} by user={user.id}")
    return CollectionDetailResponse.model_validate(col)

None