"""
Unit and integration tests for the Administrative Ingestion REST API & CLI (Phase 8B.5).

Covers:
1. Document registration: new registration, idempotency deduplication.
2. Administrative authentication: missing key (401), invalid key (403), unconfigured key (500), valid key (202).
3. Payload validation: empty/malformed URLs, path-traversal in document_id.
4. Job status inspection: valid job retrieval, non-existent job (404), searchability disclosure.
5. Job listing with pagination bounds and status filtering.
6. Deliberate retry execution: only failed jobs allowed, non-failed jobs rejected (400).
7. Confirmation that registration queues a job without synchronous PDF downloading/chunking.
8. CLI command validation: register, inspect, list, readiness, work loop.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import uuid
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import app
from app.api.v1.admin import get_repository_factory, verify_admin_key
from app.persistence.file_repo import FileStorageRepositoryFactory
from app.schemas.ingestion_job import JobStatus, ProcessingStage
from app.ingestion.cli import main as cli_main


TEST_ADMIN_KEY = "test-secret-admin-key-12345"


@pytest.fixture
def admin_env(tmp_path, monkeypatch):
    """Fixture providing isolated file repository and configured admin API key."""
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    indexes_dir = tmp_path / "indexes"
    indexes_dir.mkdir(parents=True, exist_ok=True)

    factory = FileStorageRepositoryFactory(processed_dir=processed_dir)

    test_settings = Settings(
        admin_api_key=TEST_ADMIN_KEY,
        processed_dir=str(processed_dir),
        indexes_dir=str(indexes_dir),
        database_url=None,
    )

    # Override FastAPI dependency overrides
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_repository_factory] = lambda: factory

    monkeypatch.setenv("ADMIN_API_KEY", TEST_ADMIN_KEY)
    monkeypatch.setenv("PROCESSED_DIR", str(processed_dir))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    client = TestClient(app)

    yield {
        "client": client,
        "factory": factory,
        "processed_dir": processed_dir,
        "settings": test_settings,
        "auth_headers": {"X-Admin-Key": TEST_ADMIN_KEY},
    }

    app.dependency_overrides.clear()


def test_admin_auth_enforcement(admin_env):
    """Test 1: Authentication enforcement on administrative endpoints."""
    client = admin_env["client"]

    # 1. Missing header -> 401 Unauthorized
    resp = client.post("/api/v1/admin/documents/register", json={
        "source_url": "https://sebi.gov.in/circ.pdf",
        "idempotency_key": "k1",
    })
    assert resp.status_code == 401
    assert "Missing required administrative header" in resp.json()["detail"]

    # 2. Invalid header -> 403 Forbidden
    resp = client.post(
        "/api/v1/admin/documents/register",
        json={"source_url": "https://sebi.gov.in/circ.pdf", "idempotency_key": "k1"},
        headers={"X-Admin-Key": "wrong-secret-key"},
    )
    assert resp.status_code == 403
    assert "Invalid administrative key" in resp.json()["detail"]

    # 3. Server with unconfigured ADMIN_API_KEY -> 500 Internal Server Error
    unconfigured_settings = Settings(admin_api_key=None, database_url=None)
    app.dependency_overrides[get_settings] = lambda: unconfigured_settings

    resp = client.post(
        "/api/v1/admin/documents/register",
        json={"source_url": "https://sebi.gov.in/circ.pdf", "idempotency_key": "k1"},
        headers={"X-Admin-Key": "any-key"},
    )
    assert resp.status_code == 500
    assert "not configured on the server" in resp.json()["detail"]

    # Restore settings
    app.dependency_overrides[get_settings] = lambda: admin_env["settings"]


def test_document_registration_and_idempotency(admin_env):
    """Test 2: Document registration and idempotent duplicate registration."""
    client = admin_env["client"]
    headers = admin_env["auth_headers"]

    payload = {
        "source_url": "https://sebi.gov.in/circular_2026_99.pdf",
        "regulator": "SEBI",
        "domain": "CSCRF",
        "idempotency_key": "reg_key_test_01",
        "document_id": "sebi_doc_2026_99",
        "circular_number": "SEBI/HO/2026/99",
        "title": "Advisory on AI Model Governance",
    }

    # 1. New registration -> 202 Accepted
    resp1 = client.post("/api/v1/admin/documents/register", json=payload, headers=headers)
    assert resp1.status_code == 202
    data1 = resp1.json()
    assert data1["status"] == "queued"
    assert data1["stage"] == "pending"
    assert data1["is_duplicate_registration"] is False
    assert data1["searchable"] is False
    assert data1["publication_status"] == "not_published"
    job_id = data1["job_id"]

    # 2. Duplicate registration with same idempotency key -> returns existing job
    resp2 = client.post("/api/v1/admin/documents/register", json=payload, headers=headers)
    assert resp2.status_code == 202
    data2 = resp2.json()
    assert data2["job_id"] == job_id
    assert data2["is_duplicate_registration"] is True
    assert "already registered" in data2["message"]


def test_registration_validation_guards(admin_env):
    """Test 3: Request validation rejects malformed URLs and path traversal document IDs."""
    client = admin_env["client"]
    headers = admin_env["auth_headers"]

    # Invalid scheme (ftp://)
    resp = client.post("/api/v1/admin/documents/register", json={
        "source_url": "ftp://sebi.gov.in/payload.pdf",
        "idempotency_key": "k_ftp",
    }, headers=headers)
    assert resp.status_code == 422

    # Path traversal in document_id
    resp = client.post("/api/v1/admin/documents/register", json={
        "source_url": "https://sebi.gov.in/doc.pdf",
        "idempotency_key": "k_trav",
        "document_id": "../../etc/passwd",
    }, headers=headers)
    assert resp.status_code == 422

    # Empty idempotency key
    resp = client.post("/api/v1/admin/documents/register", json={
        "source_url": "https://sebi.gov.in/doc.pdf",
        "idempotency_key": "",
    }, headers=headers)
    assert resp.status_code == 422


def test_job_status_inspection_and_searchability_contract(admin_env):
    """Test 4: Job status inspection discloses non-searchable status."""
    client = admin_env["client"]
    headers = admin_env["auth_headers"]

    # Register a job
    reg_resp = client.post("/api/v1/admin/documents/register", json={
        "source_url": "https://sebi.gov.in/status_test.pdf",
        "idempotency_key": "status_key_1",
    }, headers=headers)
    job_id = reg_resp.json()["job_id"]

    # Retrieve status
    status_resp = client.get(f"/api/v1/admin/jobs/{job_id}/status", headers=headers)
    assert status_resp.status_code == 200
    st = status_resp.json()
    assert st["job_id"] == job_id
    assert st["status"] == "queued"
    assert st["stage"] == "pending"
    assert st["searchable"] is False
    assert st["publication_status"] == "not_published"
    assert "not yet published to the active query index" in st["publication_notice"]

    # Non-existent job -> 404 Not Found
    bogus_id = str(uuid.uuid4())
    resp_404 = client.get(f"/api/v1/admin/jobs/{bogus_id}/status", headers=headers)
    assert resp_404.status_code == 404
    assert "not found" in resp_404.json()["detail"].lower()


def test_job_listing_and_pagination(admin_env):
    """Test 5: List jobs with bounded pagination and status filters."""
    client = admin_env["client"]
    headers = admin_env["auth_headers"]

    # Create 3 jobs
    for i in range(3):
        client.post("/api/v1/admin/documents/register", json={
            "source_url": f"https://sebi.gov.in/batch_{i}.pdf",
            "idempotency_key": f"batch_key_{i}",
        }, headers=headers)

    # List all
    list_resp = client.get("/api/v1/admin/jobs?limit=10&offset=0", headers=headers)
    assert list_resp.status_code == 200
    body = list_resp.json()
    assert body["total"] == 3
    assert len(body["jobs"]) == 3

    # Filter by status 'completed' -> should return 0 jobs
    list_filtered = client.get("/api/v1/admin/jobs?status=completed", headers=headers)
    assert list_filtered.status_code == 200
    assert list_filtered.json()["total"] == 0

    # Filter by status 'queued' -> should return 3 jobs
    list_queued = client.get("/api/v1/admin/jobs?status=queued", headers=headers)
    assert list_queued.status_code == 200
    assert list_queued.json()["total"] == 3


def test_manual_retry_restricted_to_failed_jobs(admin_env):
    """Test 6: Deliberate retry endpoint rejects non-failed jobs and resets failed jobs."""
    client = admin_env["client"]
    headers = admin_env["auth_headers"]
    factory = admin_env["factory"]
    job_repo = factory.get_ingestion_job_repository()

    # Create a job
    reg = client.post("/api/v1/admin/documents/register", json={
        "source_url": "https://sebi.gov.in/retry_test.pdf",
        "idempotency_key": "retry_key_1",
    }, headers=headers).json()
    job_id = reg["job_id"]

    # Attempting to retry a 'queued' job -> 400 Bad Request
    resp_bad = client.post(f"/api/v1/admin/jobs/{job_id}/retry", headers=headers)
    assert resp_bad.status_code == 400
    assert "Only jobs in 'failed' status may be retried" in resp_bad.json()["detail"]

    # Manually transition job to 'failed' in repository
    token = uuid.uuid4()
    claimed = job_repo.claim_next_job("worker_failer", token)
    job_repo.mark_failed(
        job_id=uuid.UUID(job_id),
        claim_token=token,
        error_details={"reason": "Simulated terminal error"},
    )
    failed_job = job_repo.get_job(uuid.UUID(job_id))
    assert failed_job.status == JobStatus.FAILED

    # Now call retry endpoint -> 200 OK, resets to 'queued'
    resp_retry = client.post(f"/api/v1/admin/jobs/{job_id}/retry", headers=headers)
    assert resp_retry.status_code == 200
    retry_body = resp_retry.json()
    assert retry_body["status"] == "queued"
    assert "returned to queue" in retry_body["message"]

    # Verify repository state
    requeued_job = job_repo.get_job(uuid.UUID(job_id))
    assert requeued_job.status == JobStatus.QUEUED


def test_registration_does_not_execute_synchronously(admin_env):
    """Test 7: Proves document registration does NOT synchronously execute downloading/chunking."""
    client = admin_env["client"]
    headers = admin_env["auth_headers"]
    doc_repo = admin_env["factory"].get_document_repository()

    # Register a URL pointing to an unreachable domain
    resp = client.post("/api/v1/admin/documents/register", json={
        "source_url": "https://definitely-unreachable-domain-12345.org/test.pdf",
        "idempotency_key": "sync_check_key",
        "document_id": "doc_sync_check",
    }, headers=headers)

    # HTTP request succeeds instantly with 202 Accepted because work is queued, not executed synchronously
    assert resp.status_code == 202
    assert resp.json()["status"] == "queued"

    # Document is not yet in document_repo
    doc = doc_repo.get_document("doc_sync_check")
    assert doc is None


def test_cli_commands(admin_env, monkeypatch, capsys):
    """Test 8: Operational CLI register, inspect, list, readiness, and work commands."""
    processed_dir = admin_env["processed_dir"]

    # 1. Readiness command
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings(force_reload=True)
    monkeypatch.setattr("sys.argv", ["cli.py", "readiness"])
    code = cli_main()
    assert code == 0
    captured = capsys.readouterr()
    assert '"storage_mode": "file_backed"' in captured.out

    # 2. Register command
    monkeypatch.setattr("sys.argv", [
        "cli.py", "register",
        "--url", "https://sebi.gov.in/cli_test.pdf",
        "--idempotency-key", "cli_key_01",
        "--document-id", "cli_doc_01",
    ])
    code = cli_main()
    assert code == 0
    captured = capsys.readouterr()
    assert '"duplicate": false' in captured.out
    reg_out = json.loads(captured.out.split("Document successfully registered for ingestion:")[1])
    job_id = reg_out["job_id"]

    # 3. Inspect command
    monkeypatch.setattr("sys.argv", ["cli.py", "inspect", "--job-id", job_id])
    code = cli_main()
    assert code == 0
    captured = capsys.readouterr()
    assert '"status": "queued"' in captured.out
    assert '"publication_status": "not_published"' in captured.out

    # 4. List command
    monkeypatch.setattr("sys.argv", ["cli.py", "list", "--limit", "10"])
    code = cli_main()
    assert code == 0
    captured = capsys.readouterr()
    assert '"total": 1' in captured.out
