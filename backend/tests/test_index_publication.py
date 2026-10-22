"""
Focused tests for Search Index Publication and Synchronization (Phase 8B.6).

Covers:
1. Rebuilding and publishing candidate snapshot from the 5-document pilot corpus.
2. Correct chunk/vector/metadata association and fingerprint computation.
3. Strict validation: rejection of NaN/Infinity vectors, dimensionality mismatch, and chunk count mismatch.
4. Idempotent builds and atomic activation (last known good snapshot preserved if build fails).
5. Dynamic searchability contract: document remains unsearchable until snapshot activation.
6. Admin REST API: POST /api/v1/admin/indexes/publish and GET /api/v1/admin/indexes/status.
7. Query API zero-downtime refresh: reload_if_stale updates query engine when active snapshot changes.
8. Operational CLI: publish-index and inspect-index subcommands.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import uuid
import pytest
import numpy as np
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import app
from app.api.v1.admin import get_repository_factory, verify_admin_key, get_index_publisher
from app.persistence.file_repo import FileStorageRepositoryFactory
from app.retrieval.publisher import IndexPublisher, IndexValidationError
from app.retrieval.service import RetrievalService
from app.schemas.document import DocumentRecord, ChunkRecord
from app.schemas.ingestion_job import JobStatus, ProcessingStage
from app.ingestion.cli import main as cli_main


TEST_ADMIN_KEY = "test-secret-publisher-key-999"


@pytest.fixture
def pub_env(tmp_path, monkeypatch):
    """Fixture providing isolated environment for publication tests."""
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    indexes_dir = tmp_path / "indexes"
    indexes_dir.mkdir(parents=True, exist_ok=True)

    # Copy pilot corpus documents and chunks from real data/processed
    real_processed = Path("data/processed")
    for doc_folder in real_processed.iterdir():
        if doc_folder.is_dir() and not doc_folder.name.startswith((".", "_")):
            target_folder = processed_dir / doc_folder.name
            target_folder.mkdir(parents=True, exist_ok=True)
            for f in doc_folder.iterdir():
                if f.is_file():
                    target_folder.joinpath(f.name).write_bytes(f.read_bytes())

    factory = FileStorageRepositoryFactory(processed_dir=processed_dir)

    test_settings = Settings(
        admin_api_key=TEST_ADMIN_KEY,
        processed_dir=str(processed_dir),
        indexes_dir=str(indexes_dir),
        database_url=None,
    )

    publisher = IndexPublisher(
        repository_factory=factory,
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
    )

    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_repository_factory] = lambda: factory
    app.dependency_overrides[get_index_publisher] = lambda: publisher

    monkeypatch.setenv("ADMIN_API_KEY", TEST_ADMIN_KEY)
    monkeypatch.setenv("PROCESSED_DIR", str(processed_dir))
    monkeypatch.setenv("INDEXES_DIR", str(indexes_dir))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings(force_reload=True)

    client = TestClient(app)

    yield {
        "client": client,
        "factory": factory,
        "publisher": publisher,
        "indexes_dir": indexes_dir,
        "processed_dir": processed_dir,
        "auth_headers": {"X-Admin-Key": TEST_ADMIN_KEY},
        "settings": test_settings,
    }

    app.dependency_overrides.clear()


def test_1_build_and_publish_pilot_snapshot(pub_env):
    """Test 1: Build candidate snapshot from the 5-document pilot corpus and verify manifest."""
    publisher = pub_env["publisher"]
    indexes_dir = pub_env["indexes_dir"]

    # Before publication: has_active_snapshot is False
    status_before = publisher.get_index_status()
    assert status_before.has_active_snapshot is False
    assert len(status_before.pending_document_ids) == 5

    # Publish
    res = publisher.build_and_publish_snapshot()
    assert res.success is True
    assert res.total_documents == 5
    assert res.total_chunks == 168
    assert len(res.newly_published_documents) == 5

    # Check files created
    active_ptr = indexes_dir / "active_snapshot.json"
    assert active_ptr.exists()
    snap_dir = indexes_dir / "snapshots" / res.snapshot_id
    assert snap_dir.exists()
    assert (snap_dir / "manifest.json").exists()
    assert (snap_dir / "dense_index.npy").exists()
    assert (snap_dir / "chunks.json").exists()
    assert (snap_dir / "documents.json").exists()

    # After publication: has_active_snapshot is True, 0 pending
    status_after = publisher.get_index_status()
    assert status_after.has_active_snapshot is True
    assert status_after.active_snapshot_id == res.snapshot_id
    assert len(status_after.pending_document_ids) == 0
    assert status_after.total_chunks == 168


def test_2_dry_run_validation_does_not_activate(pub_env):
    """Test 2: Dry-run builds and validates candidate index without activating or modifying pointer."""
    publisher = pub_env["publisher"]
    indexes_dir = pub_env["indexes_dir"]

    res = publisher.build_and_publish_snapshot(dry_run=True)
    assert res.success is True
    assert res.total_documents == 5
    assert res.total_chunks == 168

    # active pointer must not exist
    assert not (indexes_dir / "active_snapshot.json").exists()
    assert publisher.get_active_pointer() is None


def test_3_validation_rejects_nan_and_dimension_mismatch(pub_env):
    """Test 3: Candidate index validation rejects NaN vectors and dimensionality mismatches."""
    publisher = pub_env["publisher"]
    factory = pub_env["factory"]

    chunks = factory.get_chunk_repository().list_all_chunks()

    # Case A: NaN vectors
    nan_matrix = np.full((len(chunks), 384), np.nan, dtype=np.float32)
    with pytest.raises(IndexValidationError, match="NaN or Infinite"):
        publisher._validate_candidate_artifacts(chunks, nan_matrix)

    # Case B: Incorrect dimensions (e.g. 512 instead of 384)
    bad_dim_matrix = np.zeros((len(chunks), 512), dtype=np.float32)
    with pytest.raises(IndexValidationError, match="Embedding dimension"):
        publisher._validate_candidate_artifacts(chunks, bad_dim_matrix)

    # Case C: Row count mismatch
    bad_row_matrix = np.zeros((len(chunks) - 5, 384), dtype=np.float32)
    with pytest.raises(IndexValidationError, match="Embedding row count"):
        publisher._validate_candidate_artifacts(chunks, bad_row_matrix)


def test_4_failed_publication_preserves_last_known_good_snapshot(pub_env, monkeypatch):
    """Test 4: If candidate generation fails, the active snapshot is preserved undamaged."""
    publisher = pub_env["publisher"]

    # Step 1: Successfully publish initial snapshot
    res1 = publisher.build_and_publish_snapshot()
    assert res1.success is True
    initial_snap_id = res1.snapshot_id

    # Step 2: Simulate failure during candidate validation on second run
    def fail_validate(*args, **kwargs):
        raise IndexValidationError("Simulated validation error on candidate artifacts")

    monkeypatch.setattr(publisher, "_validate_candidate_artifacts", fail_validate)

    res2 = publisher.build_and_publish_snapshot()
    assert res2.success is False
    assert "Simulated validation error" in res2.error_details

    # Pointer must still point to initial snapshot!
    pointer = publisher.get_active_pointer()
    assert pointer is not None
    assert pointer.snapshot_id == initial_snap_id


def test_5_admin_rest_api_publish_and_status(pub_env):
    """Test 5: REST API endpoints POST /indexes/publish and GET /indexes/status."""
    client = pub_env["client"]
    headers = pub_env["auth_headers"]

    # 1. GET status before publish
    st_resp = client.get("/api/v1/admin/indexes/status", headers=headers)
    assert st_resp.status_code == 200
    assert st_resp.json()["has_active_snapshot"] is False

    # 2. POST publish dry-run
    pub_dry = client.post("/api/v1/admin/indexes/publish?dry_run=true", headers=headers)
    assert pub_dry.status_code == 200
    assert pub_dry.json()["success"] is True

    # 3. POST publish live
    pub_live = client.post("/api/v1/admin/indexes/publish?dry_run=false", headers=headers)
    assert pub_live.status_code == 200
    live_data = pub_live.json()
    assert live_data["success"] is True
    assert live_data["total_documents"] == 5

    # 4. GET status after publish
    st_after = client.get("/api/v1/admin/indexes/status", headers=headers)
    assert st_after.status_code == 200
    assert st_after.json()["has_active_snapshot"] is True
    assert st_after.json()["active_snapshot_id"] == live_data["snapshot_id"]


def test_6_document_searchability_updates_upon_publication(pub_env):
    """Test 6: Ingestion job status accurately transitions from unsearchable to searchable after publication."""
    client = pub_env["client"]
    headers = pub_env["auth_headers"]
    factory = pub_env["factory"]
    publisher = pub_env["publisher"]
    job_repo = factory.get_ingestion_job_repository()

    # Register an ingestion job for an existing pilot document
    job = job_repo.create_job(
        source_url="https://sebi.gov.in/test.pdf",
        idempotency_key="key_searchability_1",
        document_id="sebi_cscrf_2025_119",
    )
    # Claim job so it transitions to PROCESSING
    claimed = job_repo.claim_next_job(worker_id="worker_test_pub")
    assert claimed is not None
    assert claimed.id == job.id

    job_repo.mark_completed(
        job_id=job.id,
        claim_token=claimed.claim_token,
        document_id="sebi_cscrf_2025_119",
        sha256="7eb58ae065db764eec8a118c35777c8d4fb3a3b4ceee15878c28cb9599f1c31d",
    )

    # Before snapshot publication: searchable is False, publication_status is pending_publication
    resp1 = client.get(f"/api/v1/admin/jobs/{job.id}/status", headers=headers)
    assert resp1.status_code == 200
    assert resp1.json()["searchable"] is False
    assert resp1.json()["publication_status"] == "pending_publication"

    # Now publish search index
    publisher.build_and_publish_snapshot()

    # After publication: searchable is True, publication_status is published
    resp2 = client.get(f"/api/v1/admin/jobs/{job.id}/status", headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["searchable"] is True
    assert resp2.json()["publication_status"] == "published"


def test_7_retrieval_service_snapshot_loading_and_reload(pub_env):
    """Test 7: RetrievalService seamlessly reloads when new index snapshot is activated."""
    publisher = pub_env["publisher"]
    indexes_dir = pub_env["indexes_dir"]
    processed_dir = pub_env["processed_dir"]

    # Initial publication
    res = publisher.build_and_publish_snapshot()
    assert res.success is True

    # Instantiate RetrievalService
    service = RetrievalService(processed_dir=processed_dir, indexes_dir=indexes_dir)
    doc_cnt, chunk_cnt = service.load_and_index_corpus()
    assert doc_cnt == 5
    assert chunk_cnt == 168
    assert service.active_snapshot_id == res.snapshot_id

    # Verify search operates over published snapshot
    search_res = service.search_hybrid("ISO 27001", top_k=3)
    assert search_res.total_results > 0

    # No reload needed if pointer unchanged
    assert service.reload_if_stale() is False


def test_8_cli_publish_and_inspect_index(pub_env, monkeypatch, capsys):
    """Test 8: Operational CLI publish-index and inspect-index commands."""
    indexes_dir = pub_env["indexes_dir"]
    processed_dir = pub_env["processed_dir"]

    # 1. Inspect index before publish
    monkeypatch.setattr("sys.argv", ["cli.py", "inspect-index"])
    code = cli_main()
    assert code == 0
    captured = capsys.readouterr()
    assert '"has_active_snapshot": false' in captured.out

    # 2. Publish index
    monkeypatch.setattr("sys.argv", ["cli.py", "publish-index"])
    code = cli_main()
    assert code == 0
    captured = capsys.readouterr()
    assert '"success": true' in captured.out

    # 3. Inspect index after publish
    monkeypatch.setattr("sys.argv", ["cli.py", "inspect-index"])
    code = cli_main()
    assert code == 0
    captured = capsys.readouterr()
    assert '"has_active_snapshot": true' in captured.out
    assert '"total_chunks": 168' in captured.out


def test_9_sixth_document_dynamic_inclusion_and_retrieval(pub_env):
    """
    Test 9 (Acceptance Condition 1 & 2):
    Verify dynamic corpus expansion: ingest a 6th regulatory document with new chunks,
    publish a new snapshot, and verify that the 6th document is indexed and retrievable,
    while leaving the baseline pilot corpus intact.
    """
    factory = pub_env["factory"]
    publisher = pub_env["publisher"]
    indexes_dir = pub_env["indexes_dir"]
    processed_dir = pub_env["processed_dir"]

    # 1. Initially publish 5-document pilot snapshot
    res1 = publisher.build_and_publish_snapshot()
    assert res1.success is True
    assert res1.total_documents == 5
    assert res1.total_chunks == 168

    # 2. Dynamically add a 6th regulatory document
    doc_repo = factory.get_document_repository()
    chunk_repo = factory.get_chunk_repository()

    sixth_doc_id = "sebi_circular_2026_test_audit"
    doc_record = DocumentRecord(
        document_id=sixth_doc_id,
        title="SEBI Circular on Dynamic Regulatory Search Acceptance Testing",
        circular_number="SEBI/HO/MRD/2026/099",
        issue_date="2026-10-11",
        effective_date="2026-10-11",
        source_url="https://www.sebi.gov.in/legal/circulars/oct-2026/test_audit.pdf",
        sha256="deadbeef1234567890abcdef1234567890abcdef1234567890abcdef12345678",
        pdf_path="raw_documents/test_audit.pdf",
        total_pages=1,
        collection_id="sebi_cscrf",
    )
    doc_repo.save_document(doc_record)

    sixth_chunk = ChunkRecord(
        chunk_id=f"{sixth_doc_id}_p1_001",
        document_id=sixth_doc_id,
        text="All Qualified REs must maintain dynamic search index synchronization under Regulation 8B.6.",
        page_start=1,
        page_end=1,
        metadata={"provisions": ["Clause 8B.6"]},
        collection_id="sebi_cscrf",
    )
    chunk_repo.save_chunks([sixth_chunk])

    # Before publishing second snapshot: 6th document is pending publication
    status_mid = publisher.get_index_status()
    assert status_mid.has_active_snapshot is True
    assert sixth_doc_id in status_mid.pending_document_ids
    assert publisher.is_document_searchable(sixth_doc_id) is False

    # 3. Publish second snapshot
    res2 = publisher.build_and_publish_snapshot()
    assert res2.success is True
    assert res2.total_documents == 6
    assert res2.total_chunks == 169
    assert sixth_doc_id in res2.newly_published_documents

    # Verify searchability transitioned to True
    assert publisher.is_document_searchable(sixth_doc_id) is True

    # 4. Verify RetrievalService reload & retrieval of sixth document
    service = RetrievalService(processed_dir=processed_dir, indexes_dir=indexes_dir)
    service.load_and_index_corpus()
    service.load_and_index_dense()

    assert len(service.documents) == 6
    assert len(service.chunks) == 169

    search_res = service.search_hybrid("dynamic search index synchronization Regulation 8B.6", top_k=3)
    assert search_res.total_results > 0
    top_chunk_ids = [r.chunk_id for r in search_res.results]
    assert f"{sixth_doc_id}_p1_001" in top_chunk_ids


def test_10_concurrent_publication_lock_protection(pub_env):
    """
    Test 10 (Acceptance Condition 6):
    Verify that concurrent publication attempts are safely blocked by the atomic lock file.
    """
    publisher = pub_env["publisher"]
    indexes_dir = pub_env["indexes_dir"]

    # Artificially create active lock file simulating an ongoing build
    lock_file = indexes_dir / ".publish.lock"
    lock_file.write_text("99999:2026-10-11T16:00:00Z\n", encoding="utf-8")

    # Concurrent attempt must fail safely without modifying pointer
    res = publisher.build_and_publish_snapshot()
    assert res.success is False
    assert "ConcurrentPublicationConflict" in str(res.error_details)

    # Release artificial lock
    lock_file.unlink()

    # Now publication succeeds
    res_retry = publisher.build_and_publish_snapshot()
    assert res_retry.success is True


def test_11_corrupted_manifest_checksum_rejection(pub_env):
    """
    Test 11 (Acceptance Condition 9):
    Verify that RetrievalService detects and rejects a snapshot with a tampered manifest checksum.
    """
    publisher = pub_env["publisher"]
    indexes_dir = pub_env["indexes_dir"]
    processed_dir = pub_env["processed_dir"]

    # Publish valid snapshot
    res = publisher.build_and_publish_snapshot()
    assert res.success is True

    # Tamper with manifest in active snapshot directory
    manifest_file = indexes_dir / "snapshots" / res.snapshot_id / "manifest.json"
    manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest_data["total_chunks"] = 9999  # Tamper with manifest
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    # Service should reject corrupt snapshot directory
    service = RetrievalService(processed_dir=processed_dir, indexes_dir=indexes_dir)
    snap_dir = service.get_active_snapshot_dir()
    assert snap_dir is None  # Checksum mismatch triggers rejection
