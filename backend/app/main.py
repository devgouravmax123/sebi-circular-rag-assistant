from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
import os
import logging

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

app = FastAPI(
    title="SEBI Circular Q&A Assistant API",
    description="Educational API for querying official SEBI circulars with supersession awareness",
    version="0.1.0",
)

# Configure CORS using validated origins from Settings
try:
    allowed_origins = settings.allowed_cors_origins
except ValueError as e:
    logger.critical(f"Fatal CORS configuration error: {e}")
    raise SystemExit(f"Configuration error: {e}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


from app.api.v1.query import router as query_router
from app.api.v1.admin import router as admin_router

app.include_router(query_router)
app.include_router(admin_router)


@app.get("/health")
def get_health() -> dict:
    """Liveness probe endpoint."""
    return {"status": "ok"}


from fastapi import Depends

@app.get("/ready")
def get_readiness(current_settings: "Settings" = Depends(get_settings)) -> dict:
    """
    Readiness probe endpoint distinguishing healthy dependencies from broken state.
    Checks:
    1. Active search index pointer existence, valid readable snapshot manifest, and embedding model compatibility.
    2. Database connectivity if DATABASE_URL is configured.
    """
    import json
    errors = []

    # 1. Search index readiness and manifest validity
    indexes_dir = Path(current_settings.indexes_dir)
    active_ptr_path = indexes_dir / "active_snapshot.json"
    if not active_ptr_path.exists():
        errors.append("Active search index snapshot pointer missing or not published")
    else:
        try:
            with open(active_ptr_path, "r", encoding="utf-8") as f:
                ptr_data = json.load(f)
            snap_id = ptr_data.get("snapshot_id")
            if not snap_id:
                errors.append("Active search index pointer is missing 'snapshot_id'")
            else:
                snap_dir = indexes_dir / "snapshots" / snap_id
                manifest_path = snap_dir / "manifest.json"
                if not manifest_path.exists():
                    errors.append(f"Manifest for active snapshot '{snap_id}' does not exist on disk")
                else:
                    with open(manifest_path, "r", encoding="utf-8") as mf:
                        manifest_data = json.load(mf)
                    # Verify model compatibility only when in hybrid mode
                    if not current_settings.is_lexical_only:
                        from app.retrieval.dense_embedder import DEFAULT_MODEL_NAME
                        snap_model = manifest_data.get("embedding_model")
                        if snap_model and snap_model != DEFAULT_MODEL_NAME:
                            errors.append(
                                f"Active snapshot embedding model '{snap_model}' does not match expected '{DEFAULT_MODEL_NAME}'"
                            )
                    # Verify core index files exist matching publisher assets
                    for required_file in ("chunks.json", "documents.json"):
                        if not (snap_dir / required_file).exists():
                            errors.append(f"Active snapshot is missing required index asset: {required_file}")
                    # Dense vector asset check only required when hybrid retrieval is active
                    if not current_settings.is_lexical_only:
                        if not ((snap_dir / "dense_index.npy").exists() or (snap_dir / "embeddings.npy").exists()):
                            errors.append("Active snapshot is missing required index asset: dense_index.npy")
        except Exception as e:
            errors.append(f"Failed to read or validate active search index snapshot: {str(e)}")


    # 2. Database readiness if configured
    if current_settings.is_database_enabled and current_settings.database_url:
        try:
            from sqlalchemy import create_engine, text
            engine = create_engine(current_settings.database_url, pool_pre_ping=True)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1;"))
        except Exception as e:
            errors.append(f"Database connectivity failed: {str(e)}")

    if errors:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "not_ready", "reasons": errors},
        )

    return {
        "status": "ready",
        "storage_mode": "postgresql" if current_settings.is_database_enabled else "file_backed",
        "index_active": True,
    }
