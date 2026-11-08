"""
Guest Session & Ephemeral Research REST API endpoints (Milestones 11, 12, 13).
Path prefix: /api/v1/guest
"""
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import logging
from typing import Optional, Dict, Any, List
import uuid

from fastapi import APIRouter, Depends, HTTPException, Security, UploadFile, File, Form, Header, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.core.config import Settings, get_settings
from app.core.auth import decode_access_token
from app.api.deps import get_db_session_optional
from app.models.guest import GuestSessionModel, GuestQueryHistoryModel
from app.models.collection import CollectionModel
from app.persistence.models import RegulatoryDocumentModel, IngestionJobModel
from app.schemas.guest import (
    GuestQuotas,
    GuestSessionCreateResponse,
    GuestSessionStatusResponse,
    GuestSessionTerminationResponse,
    GuestCollectionCreateRequest,
    GuestCollectionResponse,
    GuestUploadResponse,
    GuestHistoryItem,
    GuestHistoryResponse,
)
from app.storage import get_storage_adapter
from app.services.guest_service import (
    _memory_guest_sessions,
    _memory_guest_history,
    _memory_guest_collections,
    _memory_guest_documents,
    _memory_guest_jobs,
    get_guest_workspace_id,
    get_guest_canonical_collection_id,
    ensure_guest_workspace,
    purge_guest_session_artifacts,
    cleanup_expired_guest_sessions,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/guest", tags=["guest"])
http_bearer = HTTPBearer(auto_error=False)


def get_guest_session_context(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(http_bearer),
    settings: Settings = Depends(get_settings),
    db: Optional[Session] = Depends(get_db_session_optional),
) -> tuple[str, Dict[str, Any]]:
    """
    Validates the ephemeral guest session from the Bearer token.
    Fails closed with HTTP 401 if the token is missing, expired, or deactivated.
    """
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Guest session token required. Please initialize a guest session.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials.strip()
    try:
        payload = decode_access_token(token, settings)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Guest session has expired (24-hour limit reached).",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid guest session token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not payload.get("is_guest"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not a guest session token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    session_id_str = payload.get("session_id")
    if not session_id_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed guest session token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Server-side verification (database or memory fallback)
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    if db is not None:
        try:
            sess_uuid = uuid.UUID(session_id_str)
            db_sess = db.query(GuestSessionModel).filter(
                GuestSessionModel.id == sess_uuid,
                GuestSessionModel.session_token_hash == token_hash,
            ).first()

            if not db_sess:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Guest session not found or terminated.",
                )

            if not db_sess.is_active:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Guest session has been terminated and its temporary data deleted.",
                )

            now_utc = datetime.now(timezone.utc)
            if db_sess.expires_at < now_utc:
                db_sess.is_active = False
                db.commit()
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Guest session has expired (24-hour limit reached).",
                )

            session_dict = {
                "id": str(db_sess.id),
                "created_at": db_sess.created_at,
                "expires_at": db_sess.expires_at,
                "is_active": db_sess.is_active,
                "collections_count": db_sess.collections_count,
                "documents_count": db_sess.documents_count,
                "bytes_uploaded": db_sess.bytes_uploaded,
                "queries_count": db_sess.queries_count,
            }
            return session_id_str, session_dict
        except (ValueError, HTTPException):
            raise
        except Exception as e:
            if db is not None:
                try:
                    db.rollback()
                except Exception:
                    pass
            logger.warning(f"Database lookup failed for guest session, falling back to memory: {e}")

    # Memory fallback
    mem_sess = _memory_guest_sessions.get(session_id_str)
    if not mem_sess:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Guest session not found or terminated.",
        )

    if not mem_sess.get("is_active"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Guest session has been terminated and its temporary data deleted.",
        )

    now_utc = datetime.now(timezone.utc)
    if mem_sess.get("expires_at", now_utc) < now_utc:
        mem_sess["is_active"] = False
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Guest session has expired (24-hour limit reached).",
        )

    return session_id_str, mem_sess


# -------------------------------------------------------------------------
# Guest Session Lifecycle Endpoints
# -------------------------------------------------------------------------

@router.post(
    "/session",
    response_model=GuestSessionCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new isolated, 24-hour ephemeral guest session",
)
def create_guest_session(
    settings: Settings = Depends(get_settings),
    db: Optional[Session] = Depends(get_db_session_optional),
) -> GuestSessionCreateResponse:
    """
    Creates a new guest session with:
    - Cryptographically random session ID
    - Signed 24-hour JWT token
    - Strict resource quotas to protect free-tier capacity
    """
    session_uuid = uuid.uuid4()
    session_id_str = str(session_uuid)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    # Encode token with explicit guest claim and 24-hour expiration
    payload = {
        "sub": session_id_str,
        "session_id": session_id_str,
        "is_guest": True,
        "exp": expires_at,
        "iat": datetime.now(timezone.utc),
        "jti": str(uuid.uuid4()),
    }
    secret_key = settings.get_auth_secret_key
    token = jwt.encode(payload, secret_key, algorithm=settings.auth_algorithm)
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    # Persist in DB if available
    if db is not None:
        try:
            # Ensure isolated system workspace for guest
            ensure_guest_workspace(db, session_id_str)

            db_sess = GuestSessionModel(
                id=session_uuid,
                session_token_hash=token_hash,
                created_at=datetime.now(timezone.utc),
                expires_at=expires_at,
                is_active=True,
                collections_count=0,
                documents_count=0,
                bytes_uploaded=0,
                queries_count=0,
            )
            db.add(db_sess)
            db.commit()
        except Exception as e:
            if db:
                db.rollback()
            logger.warning(f"Could not persist guest session to DB, using memory: {e}")

    # In-memory cache
    _memory_guest_sessions[session_id_str] = {
        "id": session_id_str,
        "token_hash": token_hash,
        "created_at": datetime.now(timezone.utc),
        "expires_at": expires_at,
        "is_active": True,
        "collections_count": 0,
        "documents_count": 0,
        "bytes_uploaded": 0,
        "queries_count": 0,
    }
    _memory_guest_history[session_id_str] = []
    _memory_guest_collections[session_id_str] = []
    _memory_guest_documents[session_id_str] = []
    _memory_guest_jobs[session_id_str] = []

    logger.info(f"Created ephemeral guest session: {session_id_str} (TTL: 24h)")
    return GuestSessionCreateResponse(
        session_id=session_id_str,
        token=token,
        expires_at=expires_at,
        quotas=GuestQuotas(),
    )


@router.get(
    "/session",
    response_model=GuestSessionStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Get real-time guest session status and quota usage",
)
def get_guest_session_status(
    auth_ctx: tuple[str, Dict[str, Any]] = Depends(get_guest_session_context),
) -> GuestSessionStatusResponse:
    """Returns the time remaining and resource consumption for the calling guest session."""
    session_id, sess_data = auth_ctx
    now_utc = datetime.now(timezone.utc)
    expires_at = sess_data["expires_at"]
    remaining = max(0, int((expires_at - now_utc).total_seconds()))

    return GuestSessionStatusResponse(
        session_id=session_id,
        is_active=sess_data["is_active"],
        expires_at=expires_at,
        time_remaining_seconds=remaining,
        collections_count=sess_data.get("collections_count", 0),
        documents_count=sess_data.get("documents_count", 0),
        bytes_uploaded=sess_data.get("bytes_uploaded", 0),
        queries_count=sess_data.get("queries_count", 0),
        quotas=GuestQuotas(),
    )


@router.post(
    "/session/terminate",
    response_model=GuestSessionTerminationResponse,
    status_code=status.HTTP_200_OK,
    summary="End guest session and permanently delete all temporary session data",
)
def terminate_guest_session(
    auth_ctx: tuple[str, Dict[str, Any]] = Depends(get_guest_session_context),
    settings: Settings = Depends(get_settings),
    db: Optional[Session] = Depends(get_db_session_optional),
) -> GuestSessionTerminationResponse:
    """
    Terminates the guest session and executes server-side deletion:
    1. Cancels active/queued ingestion worker jobs to prevent republication races.
    2. Removes all uploaded files and index blobs from storage.
    3. Deletes database records (documents, jobs, query records, workspace).
    4. Invalidates session token immediately.
    """
    session_id, _ = auth_ctx
    res = purge_guest_session_artifacts(session_id=session_id, db=db, settings=settings)

    logger.info(f"Terminated guest session {session_id}: cleaned {res['deleted_documents_count']} docs, {res['deleted_files_count']} files")
    return GuestSessionTerminationResponse(
        session_id=session_id,
        is_active=False,
        cleaned_up=True,
        deleted_documents_count=res["deleted_documents_count"],
        deleted_bytes=res["deleted_bytes"],
        message="Session ended. All temporary files, collections, and query history have been deleted from the server.",
    )


def verify_maintenance_key(
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    x_maintenance_key: Optional[str] = Header(None, alias="X-Maintenance-Key"),
    settings: Settings = Depends(get_settings),
) -> str:
    """
    Enforce administrative / maintenance authentication via constant-time comparison.
    Accepts X-Admin-Key or X-Maintenance-Key matching ADMIN_API_KEY (or auth_secret_key in dev/test).
    Fails with 401 if header is missing, 403 if invalid, 500 if unconfigured in production.
    """
    configured_key = settings.admin_api_key
    if not configured_key or not configured_key.strip():
        if settings.is_production:
            logger.error("Maintenance authentication rejected: ADMIN_API_KEY is not configured on server")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Administrative authentication is not configured on the server.",
            )
        configured_key = settings.get_auth_secret_key

    provided = x_maintenance_key or x_admin_key
    if not provided:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing required maintenance authorization header ('X-Admin-Key' or 'X-Maintenance-Key').",
        )

    if not hmac.compare_digest(provided.strip(), configured_key.strip()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid administrative / maintenance key.",
        )

    return provided


@router.post(
    "/cleanup-expired",
    status_code=status.HTTP_200_OK,
    summary="Trigger expired guest session cleanup (protected endpoint for external schedulers / cron)",
)
def trigger_expired_cleanup(
    settings: Settings = Depends(get_settings),
    db: Optional[Session] = Depends(get_db_session_optional),
    auth_key: str = Depends(verify_maintenance_key),
) -> Dict[str, Any]:
    """
    Idempotent endpoint allowing authorized external cron triggers (GitHub Actions, cron-job.org)
    to clean up expired guest sessions without requiring a paid background worker.
    Requires valid 'X-Admin-Key' or 'X-Maintenance-Key' header.
    """
    cleaned = cleanup_expired_guest_sessions(db=db, settings=settings)
    return {
        "status": "success",
        "cleaned_sessions_count": cleaned,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# -------------------------------------------------------------------------
# Guest Collection & Document Operations
# -------------------------------------------------------------------------

@router.post(
    "/collections",
    response_model=GuestCollectionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a temporary collection for this guest session",
)
def create_guest_collection(
    payload: GuestCollectionCreateRequest,
    auth_ctx: tuple[str, Dict[str, Any]] = Depends(get_guest_session_context),
    db: Optional[Session] = Depends(get_db_session_optional),
) -> GuestCollectionResponse:
    """Creates a temporary guest collection with collision-resistant identifier and 1-collection quota."""
    session_id, sess_data = auth_ctx
    quotas = GuestQuotas()

    existing_collections = _memory_guest_collections.get(session_id, [])
    if len(existing_collections) >= quotas.max_collections:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Guest quota exceeded: Guests can create at most {quotas.max_collections} temporary collection. Sign up for persistent unlimited collections.",
        )

    col_id = get_guest_canonical_collection_id(session_id)
    created_at = datetime.now(timezone.utc)
    guest_ws_id = get_guest_workspace_id(session_id)

    col_obj = {
        "id": col_id,
        "name": payload.name.strip(),
        "description": payload.description.strip() if payload.description else None,
        "policy_name": "general_static",
        "created_at": created_at,
        "is_temporary": True,
    }
    existing_collections.append(col_obj)
    _memory_guest_collections[session_id] = existing_collections

    # Update quota counter
    sess_data["collections_count"] = len(existing_collections)
    if db is not None:
        try:
            ensure_guest_workspace(db, session_id)
            db_col = db.query(CollectionModel).filter(CollectionModel.id == col_id).first()
            if not db_col:
                db_col = CollectionModel(
                    id=col_id,
                    workspace_id=guest_ws_id,
                    name=payload.name.strip(),
                    description=payload.description.strip() if payload.description else None,
                    policy_name="general_static",
                    enforce_regulatory_graph=False,
                    is_system=False,
                    is_public=False,
                )
                db.add(db_col)

            db_sess = db.query(GuestSessionModel).filter(GuestSessionModel.id == uuid.UUID(session_id)).first()
            if db_sess:
                db_sess.collections_count = len(existing_collections)
            db.commit()
        except Exception as e:
            if db:
                db.rollback()
            logger.warning(f"Error persisting guest collection in DB: {e}")

    return GuestCollectionResponse(**col_obj)


@router.get(
    "/collections",
    response_model=List[GuestCollectionResponse],
    status_code=status.HTTP_200_OK,
    summary="List temporary collections for this guest session",
)
def list_guest_collections(
    auth_ctx: tuple[str, Dict[str, Any]] = Depends(get_guest_session_context),
    db: Optional[Session] = Depends(get_db_session_optional),
) -> List[GuestCollectionResponse]:
    """Lists only collections owned by the authenticated guest session."""
    session_id, _ = auth_ctx
    guest_ws_id = get_guest_workspace_id(session_id)

    if db is not None:
        try:
            db_cols = (
                db.query(CollectionModel)
                .filter(CollectionModel.workspace_id == guest_ws_id)
                .all()
            )
            if db_cols:
                return [
                    GuestCollectionResponse(
                        id=c.id,
                        name=c.name,
                        description=c.description,
                        policy_name=c.policy_name,
                        created_at=c.created_at,
                        is_temporary=True,
                    )
                    for c in db_cols
                ]
        except Exception as e:
            if db is not None:
                try:
                    db.rollback()
                except Exception:
                    pass
            logger.warning(f"Error listing guest collections from DB: {e}")

    return [GuestCollectionResponse(**c) for c in _memory_guest_collections.get(session_id, [])]


@router.post(
    "/upload",
    response_model=GuestUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a PDF document to temporary guest storage",
)
async def upload_guest_document(
    collection_id: str = Form(...),
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    auth_ctx: tuple[str, Dict[str, Any]] = Depends(get_guest_session_context),
    settings: Settings = Depends(get_settings),
    db: Optional[Session] = Depends(get_db_session_optional),
) -> GuestUploadResponse:
    """
    Accepts an uploaded PDF document into the guest's isolated temporary storage.
    Enforces strict quotas: max 2 documents, max 10MB per document.
    Enqueues standard IngestionJobModel and RegulatoryDocumentModel in DB so
    the background IngestionWorker can process it automatically.
    """
    session_id, sess_data = auth_ctx
    quotas = GuestQuotas()

    docs = _memory_guest_documents.get(session_id, [])
    if len(docs) >= quotas.max_documents:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Guest quota reached: Guests can upload at most {quotas.max_documents} documents. Create a free account for unlimited document ingestion.",
        )

    filename = file.filename or ""
    if not filename.lower().endswith(".pdf") and file.content_type != "application/pdf":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only PDF documents are supported for ingestion.",
        )

    storage_adapter = get_storage_adapter(settings)
    doc_id = f"guest_doc_{uuid.uuid4().hex}"
    guest_ws_id = get_guest_workspace_id(session_id)

    try:
        save_res = storage_adapter.save_upload_stream(
            workspace_id=guest_ws_id,
            collection_id=collection_id,
            document_id=doc_id,
            stream=file.file,
            original_filename=filename,
        )
    except Exception as e:
        logger.error(f"Error saving guest upload: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to store guest upload: {str(e)}",
        )

    doc_title = title.strip() if title and title.strip() else (filename or f"Document {doc_id}")
    job_uuid = uuid.uuid4()
    job_id = str(job_uuid)

    doc_record = {
        "id": doc_id,
        "title": doc_title,
        "collection_id": collection_id,
        "source_uri": save_res.source_uri,
        "file_size_bytes": save_res.byte_count,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "is_published": False,
    }
    docs.append(doc_record)
    _memory_guest_documents[session_id] = docs

    job_record = {
        "id": job_id,
        "document_id": doc_id,
        "collection_id": collection_id,
        "status": "queued",
        "stage": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "error_message": "Document queued. Ingestion worker required for background chunking and index publication.",
    }
    jobs = _memory_guest_jobs.get(session_id, [])
    jobs.append(job_record)
    _memory_guest_jobs[session_id] = jobs

    # Update session usage counters
    sess_data["documents_count"] = len(docs)
    sess_data["bytes_uploaded"] = sess_data.get("bytes_uploaded", 0) + save_res.byte_count

    if db is not None:
        try:
            ensure_guest_workspace(db, session_id)

            db_col = db.query(CollectionModel).filter(CollectionModel.id == collection_id).first()
            if not db_col:
                db_col = CollectionModel(
                    id=collection_id,
                    workspace_id=guest_ws_id,
                    name=collection_id,
                    policy_name="general_static",
                    is_system=False,
                    is_public=False,
                )
                db.add(db_col)
                db.flush()

            db_doc = db.query(RegulatoryDocumentModel).filter(RegulatoryDocumentModel.id == doc_id).first()
            if not db_doc:
                db_doc = RegulatoryDocumentModel(
                    id=doc_id,
                    domain_id="SEBI_CSCRF",
                    title=doc_title,
                    workspace_id=guest_ws_id,
                    collection_id=collection_id,
                )
                db.add(db_doc)
                db.flush()

            db_job = IngestionJobModel(
                id=job_uuid,
                source_url=save_res.source_uri,
                idempotency_key=f"guest_upload_{save_res.sha256[:16]}_{job_uuid.hex[:8]}",
                document_id=doc_id,
                workspace_id=guest_ws_id,
                collection_id=collection_id,
                sha256=save_res.sha256,
                file_size_bytes=save_res.byte_count,
                status="queued",
                stage="pending",
                attempt_count=0,
                max_attempts=3,
            )
            db.add(db_job)

            db_sess = db.query(GuestSessionModel).filter(GuestSessionModel.id == uuid.UUID(session_id)).first()
            if db_sess:
                db_sess.documents_count = len(docs)
                db_sess.bytes_uploaded = sess_data["bytes_uploaded"]

            db.commit()
        except Exception as e:
            if db:
                db.rollback()
            logger.warning(f"Error persisting guest upload records to DB: {e}")

    return GuestUploadResponse(
        job_id=job_id,
        document_id=doc_id,
        collection_id=collection_id,
        status="queued",
        message="Document uploaded to temporary session storage. Ingestion worker required to chunk and publish index snapshot.",
    )


@router.get(
    "/documents",
    response_model=List[Dict[str, Any]],
    status_code=status.HTTP_200_OK,
    summary="List uploaded documents in this guest session",
)
def list_guest_documents(
    auth_ctx: tuple[str, Dict[str, Any]] = Depends(get_guest_session_context),
    db: Optional[Session] = Depends(get_db_session_optional),
) -> List[Dict[str, Any]]:
    """Returns documents uploaded in the calling guest session."""
    session_id, _ = auth_ctx
    guest_ws_id = get_guest_workspace_id(session_id)

    if db is not None:
        try:
            db_docs = (
                db.query(RegulatoryDocumentModel)
                .filter(RegulatoryDocumentModel.workspace_id == guest_ws_id)
                .all()
            )
            if db_docs:
                completed_doc_ids = {
                    j.document_id for j in db.query(IngestionJobModel)
                    .filter(
                        IngestionJobModel.workspace_id == guest_ws_id,
                        IngestionJobModel.status == "completed",
                    )
                    .all()
                    if j.document_id
                }
                return [
                    {
                        "id": d.id,
                        "title": d.title,
                        "collection_id": d.collection_id,
                        "created_at": d.created_at.isoformat() if d.created_at else None,
                        "is_published": d.id in completed_doc_ids,
                    }
                    for d in db_docs
                ]
        except Exception as e:
            if db is not None:
                try:
                    db.rollback()
                except Exception:
                    pass
            logger.warning(f"Error listing guest documents from DB: {e}")

    return _memory_guest_documents.get(session_id, [])


@router.get(
    "/jobs",
    response_model=List[Dict[str, Any]],
    status_code=status.HTTP_200_OK,
    summary="List ingestion jobs in this guest session",
)
def list_guest_jobs(
    auth_ctx: tuple[str, Dict[str, Any]] = Depends(get_guest_session_context),
    db: Optional[Session] = Depends(get_db_session_optional),
) -> List[Dict[str, Any]]:
    """Returns ingestion jobs tracked for this guest session."""
    session_id, _ = auth_ctx
    guest_ws_id = get_guest_workspace_id(session_id)

    if db is not None:
        try:
            db_jobs = (
                db.query(IngestionJobModel)
                .filter(IngestionJobModel.workspace_id == guest_ws_id)
                .order_by(IngestionJobModel.created_at.desc())
                .all()
            )
            if db_jobs:
                return [
                    {
                        "id": str(j.id),
                        "document_id": j.document_id,
                        "collection_id": j.collection_id,
                        "status": j.status,
                        "stage": j.stage or "pending",
                        "created_at": j.created_at.isoformat() if j.created_at else None,
                        "error_message": (
                            j.error_details.get("reason")
                            if isinstance(j.error_details, dict)
                            else str(j.error_details) if j.error_details else None
                        ),
                    }
                    for j in db_jobs
                ]
        except Exception as e:
            if db is not None:
                try:
                    db.rollback()
                except Exception:
                    pass
            logger.warning(f"Error listing guest jobs from DB: {e}")

    return _memory_guest_jobs.get(session_id, [])


# -------------------------------------------------------------------------
# Guest Research Query & Session History
# -------------------------------------------------------------------------

@router.get(
    "/history",
    response_model=GuestHistoryResponse,
    status_code=status.HTTP_200_OK,
    summary="Get session-scoped query history for this guest session",
)
def get_guest_history(
    auth_ctx: tuple[str, Dict[str, Any]] = Depends(get_guest_session_context),
    db: Optional[Session] = Depends(get_db_session_optional),
) -> GuestHistoryResponse:
    """Returns queries executed within this ephemeral session, pulling from DB when available."""
    session_id, _ = auth_ctx

    if db is not None:
        try:
            sess_uuid = uuid.UUID(session_id)
            db_hist = (
                db.query(GuestQueryHistoryModel)
                .filter(GuestQueryHistoryModel.session_id == sess_uuid)
                .order_by(GuestQueryHistoryModel.created_at.desc())
                .all()
            )
            if db_hist:
                return GuestHistoryResponse(
                    items=[
                        GuestHistoryItem(
                            id=str(h.id),
                            query_text=h.query_text,
                            collection_id=h.collection_id,
                            disposition=h.disposition,
                            answer_text=h.answer_text,
                            created_at=h.created_at.isoformat() if h.created_at else None,
                            citations_count=h.citations_count,
                        )
                        for h in db_hist
                    ],
                    total=len(db_hist),
                )
        except Exception as e:
            if db is not None:
                try:
                    db.rollback()
                except Exception:
                    pass
            logger.warning(f"Error querying guest history from DB: {e}")

    history_items = _memory_guest_history.get(session_id, [])
    return GuestHistoryResponse(
        items=[GuestHistoryItem(**h) for h in history_items],
        total=len(history_items),
    )


@router.post(
    "/history/record",
    status_code=status.HTTP_201_CREATED,
    summary="Record a completed query into the guest session history",
)
def record_guest_query(
    payload: Dict[str, Any],
    auth_ctx: tuple[str, Dict[str, Any]] = Depends(get_guest_session_context),
    db: Optional[Session] = Depends(get_db_session_optional),
) -> Dict[str, str]:
    """Records a research query and grounded answer into the guest session history."""
    session_id, sess_data = auth_ctx
    history = _memory_guest_history.get(session_id, [])
    hist_uuid = uuid.uuid4()

    history_entry = {
        "id": f"ghist_{hist_uuid.hex[:8]}",
        "query_text": payload.get("query_text", ""),
        "collection_id": payload.get("collection_id", ""),
        "disposition": payload.get("disposition", "UNKNOWN"),
        "answer_text": payload.get("answer_text"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "citations_count": payload.get("citations_count", 0),
    }
    history.insert(0, history_entry)
    _memory_guest_history[session_id] = history

    sess_data["queries_count"] = len(history)
    if db is not None:
        try:
            sess_uuid = uuid.UUID(session_id)
            db_entry = GuestQueryHistoryModel(
                id=hist_uuid,
                session_id=sess_uuid,
                collection_id=payload.get("collection_id", ""),
                query_text=payload.get("query_text", ""),
                disposition=payload.get("disposition", "UNKNOWN"),
                answer_text=payload.get("answer_text"),
                citations_count=payload.get("citations_count", 0),
            )
            db.add(db_entry)

            db_sess = db.query(GuestSessionModel).filter(GuestSessionModel.id == sess_uuid).first()
            if db_sess:
                db_sess.queries_count = len(history)

            db.commit()
        except Exception as e:
            if db:
                db.rollback()
            logger.warning(f"Error persisting guest history in DB: {e}")

    return {"status": "ok"}
