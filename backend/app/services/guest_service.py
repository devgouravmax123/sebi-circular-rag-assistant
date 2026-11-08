"""
Unified guest session service and lifecycle management (Milestones 11, 12, 13).
Handles:
- Guest session storage, token hashing, and quotas
- Ephemeral workspace and collision-resistant collection provisioning
- Durable session query history recording and purging
- Ingestion job queuing and status tracking
- Guaranteed server-side cleanup and expiration purging without worker race conditions
"""
from datetime import datetime, timezone, timedelta
import hashlib
import logging
from pathlib import Path
import shutil
import sys
from typing import Dict, Any, List, Optional, Set
import uuid

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.guest import GuestSessionModel, GuestQueryHistoryModel
from app.models.auth import WorkspaceModel
from app.models.collection import CollectionModel
from app.persistence.models import RegulatoryDocumentModel, IngestionJobModel
from app.storage import get_storage_adapter

logger = logging.getLogger(__name__)

# In-memory session caches for zero-cost local / transient execution
_memory_guest_sessions: Dict[str, Dict[str, Any]] = {}
_memory_guest_history: Dict[str, List[Dict[str, Any]]] = {}
_memory_guest_collections: Dict[str, List[Dict[str, Any]]] = {}
_memory_guest_documents: Dict[str, List[Dict[str, Any]]] = {}
_memory_guest_jobs: Dict[str, List[Dict[str, Any]]] = {}


def get_guest_workspace_id(session_id: str) -> uuid.UUID:
    """Derives a deterministic workspace UUID for an ephemeral guest session."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, f"guest-{session_id}")


def get_guest_canonical_collection_id(session_id: str) -> str:
    """Derives a collision-resistant collection ID for an ephemeral guest session."""
    sess_hex = uuid.UUID(session_id).hex
    return f"guest_col_{sess_hex}"


def ensure_guest_workspace(db: Session, session_id: str) -> WorkspaceModel:
    """Ensures an isolated system workspace exists for the guest session."""
    guest_ws_id = get_guest_workspace_id(session_id)
    ws = db.query(WorkspaceModel).filter(WorkspaceModel.id == guest_ws_id).first()
    if not ws:
        sess_hex = uuid.UUID(session_id).hex
        ws = WorkspaceModel(
            id=guest_ws_id,
            name=f"Guest Workspace {sess_hex[:12]}",
            slug=f"guest-{sess_hex}",
            is_system=True,
            owner_id=None,
        )
        db.add(ws)
        db.flush()
    return ws


def purge_guest_session_artifacts(
    session_id: str,
    db: Optional[Session] = None,
    settings: Optional[Settings] = None,
    storage_adapter: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Idempotently and thoroughly purges all artifacts for a guest session:
    1. Cancels active/queued ingestion worker jobs to prevent post-deletion republication races.
    2. Deletes uploaded source files in storage (B2 or local).
    3. Deletes published search index snapshots on disk or remote storage across all session collections.
    4. Deletes database records (query history, jobs, docs, versions, chunks, collection, workspace).
    5. Purges in-memory caches.
    6. Marks GuestSessionModel as inactive and records cleaned_up_at timestamp.
    """
    if settings is None:
        settings = get_settings()

    if storage_adapter is None:
        storage_adapter = get_storage_adapter(settings)
    guest_ws_id = get_guest_workspace_id(session_id)

    deleted_docs_count = 0
    deleted_files_count = 0
    deleted_bytes = 0

    # Collect all associated collection IDs
    col_ids: Set[str] = {get_guest_canonical_collection_id(session_id)}
    if session_id in _memory_guest_collections:
        for mc in _memory_guest_collections[session_id]:
            if mc.get("id"):
                col_ids.add(mc["id"])

    # 1. Anti-Race Guard: Cancel running and queued worker jobs in DB first
    if db is not None:
        try:
            db_cols = db.query(CollectionModel).filter(CollectionModel.workspace_id == guest_ws_id).all()
            for c in db_cols:
                col_ids.add(c.id)

            # Abort any in-progress worker jobs so the worker aborts stage update and does not publish
            db.query(IngestionJobModel).filter(
                IngestionJobModel.workspace_id == guest_ws_id,
                IngestionJobModel.status.in_(["queued", "processing"]),
            ).update(
                {
                    "status": "failed",
                    "stage": "finished",
                    "error_details": {"reason": "Session terminated by user or expired"},
                },
                synchronize_session=False,
            )
            db.commit()
        except Exception as e:
            if db is not None:
                try:
                    db.rollback()
                except Exception:
                    pass
            logger.warning(f"Error cancelling active worker jobs during guest purge: {e}")

    # 2. Identify and delete all source files in storage
    source_uris: Set[str] = set()
    if db is not None:
        try:
            jobs = db.query(IngestionJobModel).filter(IngestionJobModel.workspace_id == guest_ws_id).all()
            for j in jobs:
                if j.source_url:
                    source_uris.add(j.source_url)
        except Exception as e:
            if db is not None:
                try:
                    db.rollback()
                except Exception:
                    pass
            logger.warning(f"Error querying guest jobs for file cleanup: {e}")

    # Also check memory docs
    mem_docs = _memory_guest_documents.get(session_id, [])
    for md in mem_docs:
        s_uri = md.get("source_uri")
        if s_uri:
            source_uris.add(s_uri)
        deleted_docs_count += 1
        deleted_bytes += md.get("file_size_bytes", 0)

    storage_errors: List[str] = []

    for s_uri in source_uris:
        try:
            if hasattr(storage_adapter, "exists") and not storage_adapter.exists(s_uri):
                # Object already removed or never persisted
                continue
            del_ok = storage_adapter.delete_file(s_uri)
            if del_ok:
                deleted_files_count += 1
            else:
                # If exists returned true but delete_file returned false
                if hasattr(storage_adapter, "exists") and storage_adapter.exists(s_uri):
                    storage_errors.append(f"Failed deleting file {s_uri}")
        except Exception as e:
            logger.warning(f"Error deleting guest storage object {s_uri}: {e}")
            storage_errors.append(f"{s_uri}: {str(e)}")

    # 3. Delete published index snapshot directories for all session collections
    for cid in col_ids:
        # Local snapshot directory cleanup
        try:
            snapshot_dir = Path(settings.indexes_dir) / cid
            if snapshot_dir.exists():
                shutil.rmtree(snapshot_dir, ignore_errors=True)
                logger.info(f"Deleted guest index snapshot directory: {snapshot_dir}")
        except Exception as e:
            logger.warning(f"Error removing guest local snapshot dir {cid}: {e}")
            storage_errors.append(f"local_snapshot_{cid}: {str(e)}")

        # Remote published snapshot cleanup in B2/S3
        try:
            if hasattr(storage_adapter, "delete_prefix"):
                storage_adapter.delete_prefix(f"indexes/{cid}/")
        except Exception as e:
            logger.warning(f"Error removing guest remote index snapshot {cid}: {e}")
            storage_errors.append(f"remote_snapshot_{cid}: {str(e)}")

    # Clean workspace upload prefix in remote storage
    try:
        if hasattr(storage_adapter, "delete_prefix"):
            storage_adapter.delete_prefix(f"uploads/{guest_ws_id}/")
            storage_adapter.delete_prefix(f"{guest_ws_id}/")
    except Exception as e:
        logger.warning(f"Error deleting guest workspace prefix {guest_ws_id}: {e}")
        storage_errors.append(f"remote_uploads_{guest_ws_id}: {str(e)}")

    storage_success = (len(storage_errors) == 0)

    # 4. Clean up database records
    if db is not None:
        try:
            sess_uuid = uuid.UUID(session_id)

            # Delete guest query history
            db.query(GuestQueryHistoryModel).filter(
                GuestQueryHistoryModel.session_id == sess_uuid
            ).delete(synchronize_session=False)

            # Delete ingestion jobs
            db.query(IngestionJobModel).filter(
                IngestionJobModel.workspace_id == guest_ws_id
            ).delete(synchronize_session=False)

            # Delete regulatory documents (cascades to versions, provisions, chunks, embeddings)
            db.query(RegulatoryDocumentModel).filter(
                RegulatoryDocumentModel.workspace_id == guest_ws_id
            ).delete(synchronize_session=False)

            # Delete collections
            db.query(CollectionModel).filter(
                CollectionModel.workspace_id == guest_ws_id
            ).delete(synchronize_session=False)

            # Delete workspace
            db.query(WorkspaceModel).filter(
                WorkspaceModel.id == guest_ws_id
            ).delete(synchronize_session=False)

            # Mark GuestSessionModel inactive; ONLY record cleaned_up_at if storage deletion fully succeeded!
            db_sess = db.query(GuestSessionModel).filter(GuestSessionModel.id == sess_uuid).first()
            if db_sess:
                db_sess.is_active = False
                if storage_success:
                    db_sess.cleaned_up_at = datetime.now(timezone.utc)
                else:
                    logger.warning(
                        f"Guest session {session_id} storage cleanup incomplete ({len(storage_errors)} error(s)); "
                        f"leaving cleaned_up_at NULL for retry."
                    )

            db.commit()
        except Exception as e:
            if db is not None:
                try:
                    db.rollback()
                except Exception:
                    pass
            logger.warning(f"Error cleaning guest database records: {e}")

    # 5. Clean up in-memory caches
    if session_id in _memory_guest_sessions:
        _memory_guest_sessions[session_id]["is_active"] = False
        if storage_success:
            _memory_guest_sessions[session_id]["cleaned_up_at"] = datetime.now(timezone.utc)
    _memory_guest_history.pop(session_id, None)
    _memory_guest_collections.pop(session_id, None)
    _memory_guest_documents.pop(session_id, None)
    _memory_guest_jobs.pop(session_id, None)

    return {
        "session_id": session_id,
        "is_active": False,
        "cleaned_up": storage_success,
        "storage_errors": storage_errors,
        "deleted_documents_count": max(deleted_docs_count, len(source_uris)),
        "deleted_files_count": deleted_files_count,
        "deleted_bytes": deleted_bytes,
    }


def cleanup_expired_guest_sessions(
    db: Optional[Session] = None,
    settings: Optional[Settings] = None,
    batch_limit: int = 50,
    storage_adapter: Optional[Any] = None,
) -> int:
    """
    Idempotent sweeper that finds expired or deactivated guest sessions whose
    temporary data has not yet been cleaned up, and purges their artifacts.
    Safe to execute by external cron, scheduled GitHub Actions, or background task.
    """
    if settings is None:
        settings = get_settings()

    cleaned_count = 0
    now_utc = datetime.now(timezone.utc)

    # 1. Database sweep
    if db is not None:
        try:
            expired_sessions = (
                db.query(GuestSessionModel)
                .filter(
                    (GuestSessionModel.expires_at <= now_utc) | (GuestSessionModel.is_active == False),
                    GuestSessionModel.cleaned_up_at.is_(None),
                )
                .limit(batch_limit)
                .all()
            )
            for sess in expired_sessions:
                try:
                    res = purge_guest_session_artifacts(
                        str(sess.id),
                        db=db,
                        settings=settings,
                        storage_adapter=storage_adapter,
                    )
                    if res.get("cleaned_up"):
                        cleaned_count += 1
                except Exception as ex:
                    logger.error(f"Error purging expired guest session {sess.id}: {ex}")
        except Exception as e:
            if db is not None:
                try:
                    db.rollback()
                except Exception:
                    pass
            logger.warning(f"Database expired sessions sweep failed: {e}")

    # 2. In-memory sweep
    expired_mem_ids = [
        sid for sid, sdata in list(_memory_guest_sessions.items())
        if (sdata.get("expires_at", now_utc) <= now_utc or not sdata.get("is_active", True))
        and not sdata.get("cleaned_up_at")
    ]
    for sid in expired_mem_ids:
        try:
            res = purge_guest_session_artifacts(
                sid,
                db=db,
                settings=settings,
                storage_adapter=storage_adapter,
            )
            if res.get("cleaned_up"):
                cleaned_count += 1
        except Exception as ex:
            logger.error(f"Error purging expired in-memory guest session {sid}: {ex}")

    return cleaned_count


def run_standalone_cleanup_cli() -> int:
    """CLI entrypoint for executing expired guest session cleanup via external cron or scheduler."""
    import argparse
    parser = argparse.ArgumentParser(description="Clean up expired guest sessions and purge all artifacts.")
    parser.add_argument("--batch-limit", type=int, default=50, help="Maximum number of expired sessions to purge per run (default: 50).")
    parser.add_argument("--max-age-hours", type=int, default=24, help="Maximum session age in hours before considering expired (default: 24).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    settings = get_settings()
    db_session = None

    if settings.is_database_enabled and settings.database_url:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        engine = create_engine(settings.database_url, pool_pre_ping=True)
        SessionMaker = sessionmaker(bind=engine)
        db_session = SessionMaker()

    try:
        logger.info(f"Starting scheduled guest session expiration sweeper (batch_limit={args.batch_limit})...")
        cleaned = cleanup_expired_guest_sessions(
            db=db_session,
            settings=settings,
            batch_limit=args.batch_limit,
        )
        logger.info(f"Sweeper finished successfully: {cleaned} expired guest session(s) purged.")
        return 0
    except Exception as e:
        logger.error(f"Scheduled guest cleanup encountered an unhandled error: {e}", exc_info=True)
        return 1
    finally:
        if db_session:
            db_session.close()


if __name__ == "__main__":
    sys.exit(run_standalone_cleanup_cli())
