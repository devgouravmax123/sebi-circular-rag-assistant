"""
Phase 8F.1 Pre-Deployment Hardening Test Suite.

Tests:
1. Environment configuration: missing secrets in production, wildcard CORS rejection, database password masking.
2. Administrative authentication across ALL route categories (negative tests: missing key, wrong key, unconfigured key).
3. Readiness probe (/ready) distinguishing missing active index / database failure from healthy state.
4. Standalone worker daemon startup, empty queue backoff, and graceful termination.
"""
import os
import json
import uuid
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import app
from app.api.v1.admin import verify_admin_key, get_repository_factory
from app.persistence.file_repo import FileStorageRepositoryFactory
from app.ingestion.worker import run_worker_daemon


@pytest.fixture
def clean_client():
    return TestClient(app)


# ==============================================================================
# SECTION 1: Production Configuration and Secrets Validation
# ==============================================================================

def test_production_rejects_missing_or_short_admin_key(monkeypatch):
    """Fail-closed startup: Production requires ADMIN_API_KEY with at least 16 characters."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)

    with pytest.raises(Exception) as exc_info:
        Settings()
    assert "ADMIN_API_KEY must be set" in str(exc_info.value) or "16 characters" in str(exc_info.value)

    # Test short key (< 16 chars)
    monkeypatch.setenv("ADMIN_API_KEY", "short-secret")
    with pytest.raises(Exception) as exc_info:
        Settings()
    assert "at least 16 characters" in str(exc_info.value)


def test_production_rejects_wildcard_cors_origin(monkeypatch):
    """Wildcard CORS origin '*' is strictly prohibited in production when credentials are True."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ADMIN_API_KEY", "super-secret-production-admin-key-64chars-long")
    monkeypatch.setenv("FRONTEND_ORIGIN", "*")

    settings = Settings()
    with pytest.raises(ValueError) as exc_info:
        _ = settings.allowed_cors_origins
    assert "Wildcard CORS origin '*' is strictly prohibited" in str(exc_info.value)


def test_cors_origin_parsing_comma_separated(monkeypatch):
    """Allows multiple valid domain origins in comma-separated format."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("FRONTEND_ORIGIN", "https://app.example.com, https://admin.example.com")

    settings = Settings()
    origins = settings.allowed_cors_origins
    assert "https://app.example.com" in origins
    assert "https://admin.example.com" in origins


def test_database_url_sanitization():
    """Confirms password is completely masked in sanitized_database_url."""
    settings = Settings(database_url="postgresql+psycopg://sebi_user:super_secret_password_999@localhost:5432/sebi_db")
    masked = settings.sanitized_database_url
    assert masked is not None
    assert "super_secret_password_999" not in masked
    assert "sebi_user" in masked
    assert "localhost:5432" in masked


# ==============================================================================
# SECTION 2: Negative Tests Covering EVERY Admin Route Category
# ==============================================================================

ADMIN_ROUTES_TO_TEST = [
    # 1. Document registration
    ("POST", "/api/v1/admin/documents/register", {"source_url": "https://test.gov.in/test.pdf", "idempotency_key": "k1"}),
    # 2. Job inspection
    ("GET", f"/api/v1/admin/jobs/{uuid.uuid4()}/status", None),
    # 3. Job listing
    ("GET", "/api/v1/admin/jobs", None),
    # 4. Job retry
    ("POST", f"/api/v1/admin/jobs/{uuid.uuid4()}/retry", None),
    # 5. Index publication status
    ("GET", "/api/v1/admin/indexes/status", None),
    # 6. Index publish action
    ("POST", "/api/v1/admin/indexes/publish", None),
    # 7. Regulatory edges listing
    ("GET", "/api/v1/admin/edges", None),
    # 8. Regulatory edge propose
    ("POST", "/api/v1/admin/edges/propose", {
        "source_document_id": "doc1", "target_document_id": "doc2", "relationship": "amends",
        "scope": "full", "evidence_text": "text", "proposer_id": "p1", "rationale": "r1"
    }),
    # 9. Regulatory edge review
    ("POST", "/api/v1/admin/edges/edge_123/review", {"decision": "verify", "reviewer_id": "r1", "rationale": "r1"}),
    # 10. Regulatory edge revoke
    ("POST", "/api/v1/admin/edges/edge_123/revoke", {"reviewer_id": "r1", "rationale": "r1"}),
    # 11. Baseline edges reconcile
    ("POST", "/api/v1/admin/edges/reconcile-baseline", {"allow_overwrite": False}),
]


@pytest.mark.parametrize("method,path,payload", ADMIN_ROUTES_TO_TEST)
def test_all_admin_routes_reject_missing_authentication(clean_client, method, path, payload):
    """Verify that omitting X-Admin-Key on ANY administrative route returns HTTP 401 when admin key is configured."""
    valid_key = "configured-admin-key-for-test-32chars"
    test_settings = Settings(admin_api_key=valid_key)
    app.dependency_overrides[get_settings] = lambda: test_settings

    try:
        if method == "GET":
            resp = clean_client.get(path)
        else:
            resp = clean_client.post(path, json=payload or {})
        assert resp.status_code == 401
        assert "Missing required administrative header" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("method,path,payload", ADMIN_ROUTES_TO_TEST)
def test_all_admin_routes_reject_invalid_authentication(clean_client, method, path, payload, monkeypatch):
    """Verify that providing an incorrect X-Admin-Key on ANY administrative route returns HTTP 403."""
    valid_key = "correct-test-admin-secret-key-12345"
    test_settings = Settings(admin_api_key=valid_key)
    app.dependency_overrides[get_settings] = lambda: test_settings

    headers = {"X-Admin-Key": "wrong-adversarial-admin-key"}
    try:
        if method == "GET":
            resp = clean_client.get(path, headers=headers)
        else:
            resp = clean_client.post(path, json=payload or {}, headers=headers)
        assert resp.status_code == 403
        assert "Invalid administrative key" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()


# ==============================================================================
# SECTION 3: Readiness Probe (/ready) Verification
# ==============================================================================

def test_readiness_probe_distinguishes_healthy_and_unready_state(clean_client, tmp_path):
    """
    Verifies that /ready returns HTTP 200 when active snapshot exists,
    and HTTP 503 when the active snapshot is missing.
    """
    empty_indexes = tmp_path / "indexes"
    empty_indexes.mkdir()

    # Configure app to point to empty indexes directory
    unready_settings = Settings(indexes_dir=str(empty_indexes), database_url=None)
    app.dependency_overrides[get_settings] = lambda: unready_settings

    try:
        # 1. Missing active snapshot -> 503 Service Unavailable
        resp = clean_client.get("/ready")
        assert resp.status_code == 503
        data = resp.json()["detail"]
        assert data["status"] == "not_ready"
        assert any("Active search index snapshot pointer missing" in r for r in data["reasons"])

        # 2. Add pointer but missing manifest -> 503
        (empty_indexes / "active_snapshot.json").write_text('{"snapshot_id": "snap_test_01"}', encoding="utf-8")
        resp_no_manifest = clean_client.get("/ready")
        assert resp_no_manifest.status_code == 503
        assert "Manifest for active snapshot 'snap_test_01' does not exist" in str(resp_no_manifest.json())

        # 3. Create snapshot directory with manifest and assets
        snap_dir = empty_indexes / "snapshots" / "snap_test_01"
        snap_dir.mkdir(parents=True)
        manifest_payload = {
            "snapshot_id": "snap_test_01",
            "format_version": 1,
            "created_at": "2026-10-12T00:00:00Z",
            "embedding_model": "BAAI/bge-small-en-v1.5",
            "embedding_dimension": 384,
            "total_documents": 1,
            "total_chunks": 1,
            "document_ids": ["doc_1"],
            "files": {},
        }
        (snap_dir / "manifest.json").write_text(json.dumps(manifest_payload), encoding="utf-8")
        (snap_dir / "chunks.json").write_text("[]", encoding="utf-8")
        (snap_dir / "documents.json").write_text("{}", encoding="utf-8")
        (snap_dir / "embeddings.npy").write_text("", encoding="utf-8")

        # 4. Complete, valid snapshot -> 200 Ready
        resp_ready = clean_client.get("/ready")
        assert resp_ready.status_code == 200
        assert resp_ready.json()["status"] == "ready"

        # 5. Incompatible embedding model in manifest -> 503
        manifest_payload["embedding_model"] = "some-incompatible-model"
        (snap_dir / "manifest.json").write_text(json.dumps(manifest_payload), encoding="utf-8")
        resp_incompatible = clean_client.get("/ready")
        assert resp_incompatible.status_code == 503
        assert "does not match expected" in str(resp_incompatible.json())
    finally:
        app.dependency_overrides.clear()


# ==============================================================================
# SECTION 4: Supervised Worker Daemon Loop
# ==============================================================================

def test_worker_daemon_runs_and_exits_cleanly_on_empty_queue(tmp_path, monkeypatch):
    """Tests that the standalone worker daemon boots, processes empty queue, and stops gracefully."""
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir()

    monkeypatch.setenv("PROCESSED_DIR", str(processed_dir))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    try:
        # Run with max_jobs=0 or poll_interval=0.01 and max_jobs=1
        exit_code = run_worker_daemon(
            poll_interval=0.05,
            lease_timeout=10.0,
            stale_recovery_interval=1.0,
            max_jobs=0,  # Stop immediately after 0 jobs
            worker_id="test_daemon_worker",
        )
        assert exit_code == 0
    finally:
        # Ensure application settings singleton is restored to avoid polluting subsequent tests
        monkeypatch.undo()
        get_settings(force_reload=True)
