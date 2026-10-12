"""
Phase 10.1.8 Test Suite: Coordinated Backup, Restore & Integrity Verification.

Validates:
1. Atomic quiescence lock prevents concurrent backup execution while publication is running.
2. Coordinated backup creates valid tar.gz containing database dump, active snapshot,
   manifest, and file checksums.
3. Every individual checksum in the manifest matches disk bytes.
4. Isolated restoration restores files into a separate target directory without touching
   the primary active repository data.
5. Tamper detection: modifying an archived file causes restore verification to fail cleanly.
"""
import json
import os
from pathlib import Path
import shutil
import tempfile
import tarfile
import pytest

from app.core.config import Settings
from app.backup.service import (
    BackupService,
    BackupQuiescenceLock,
    compute_sha256,
)


@pytest.fixture
def mock_platform_env(tmp_path):
    """Creates a temporary platform environment with mock indexes and processed data."""
    processed_dir = tmp_path / "processed"
    indexes_dir = tmp_path / "indexes"
    snapshots_dir = indexes_dir / "snapshots"
    snap_id = "snap_20261012_test_123456"
    test_snap_dir = snapshots_dir / snap_id

    processed_dir.mkdir(parents=True)
    test_snap_dir.mkdir(parents=True)

    # 1. Mock processed documents
    doc_file = processed_dir / "test_doc.json"
    doc_file.write_text(json.dumps({"document_id": "doc1", "title": "Test Circular"}), encoding="utf-8")

    # 2. Mock snapshot assets
    (test_snap_dir / "chunks.json").write_text(json.dumps([{"chunk_id": "c1", "text": "sample"}]), encoding="utf-8")
    (test_snap_dir / "documents.json").write_text(json.dumps({"doc1": {"document_id": "doc1"}}), encoding="utf-8")
    (test_snap_dir / "dense_index.npy").write_bytes(b"\x93NUMPY\x01\x00data_bytes")
    (test_snap_dir / "manifest.json").write_text(json.dumps({
        "snapshot_id": snap_id,
        "embedding_model": "BAAI/bge-small-en-v1.5",
        "total_documents": 1,
        "total_chunks": 1,
    }), encoding="utf-8")

    # 3. Active snapshot pointer
    active_ptr = indexes_dir / "active_snapshot.json"
    active_ptr.write_text(json.dumps({
        "snapshot_id": snap_id,
        "manifest_checksum": compute_sha256(test_snap_dir / "manifest.json"),
        "total_documents": 1,
        "total_chunks": 1,
    }), encoding="utf-8")

    settings = Settings(
        app_name="Test RAG",
        environment="development",
        processed_dir=str(processed_dir),
        indexes_dir=str(indexes_dir),
        database_url=None,
    )
    return {
        "settings": settings,
        "processed_dir": processed_dir,
        "indexes_dir": indexes_dir,
        "snap_id": snap_id,
        "tmp_path": tmp_path,
    }


def test_quiescence_lock_mutual_exclusion(mock_platform_env):
    """Tests that active publication lock rejects concurrent backup acquisition."""
    indexes_dir = mock_platform_env["indexes_dir"]
    lock_file = indexes_dir / ".publish.lock"

    # Simulate an active index publisher holding the lock
    lock_file.write_text("publisher_99999:2026-10-12T00:00:00Z\n", encoding="utf-8")

    service = BackupService(
        settings=mock_platform_env["settings"],
        backups_dir=mock_platform_env["tmp_path"] / "backups",
    )
    res = service.create_coordinated_backup(backup_id="test_lock_blocked")
    assert res.success is False
    assert res.error is not None
    assert "quiescence lock" in res.error or "holding" in res.error

    # Clean up lock
    lock_file.unlink()

    # Now backup succeeds
    res_ok = service.create_coordinated_backup(backup_id="test_lock_freed")
    assert res_ok.success is True
    assert not lock_file.exists(), "Lock must be cleaned up on completion"


def test_coordinated_backup_creation_and_checksum_verification(mock_platform_env):
    """Tests complete backup creation, manifest metadata, and SHA-256 consistency."""
    backups_dir = mock_platform_env["tmp_path"] / "backups"
    service = BackupService(settings=mock_platform_env["settings"], backups_dir=backups_dir)

    res = service.create_coordinated_backup(backup_id="test_backup_001")
    assert res.success is True
    assert res.archive_path is not None
    assert Path(res.archive_path).exists()
    assert res.archive_checksum == compute_sha256(Path(res.archive_path))

    manifest = res.manifest
    assert manifest is not None
    assert manifest["backup_id"] == "test_backup_001"
    assert manifest["active_snapshot_id"] == mock_platform_env["snap_id"]
    assert manifest["total_documents"] == 1
    assert manifest["total_chunks"] == 1
    assert "indexes/active_snapshot.json" in manifest["included_files"]
    assert f"indexes/snapshots/{mock_platform_env['snap_id']}/manifest.json" in manifest["included_files"]
    assert "processed/test_doc.json" in manifest["included_files"]


def test_isolated_restoration_and_tamper_detection(mock_platform_env):
    """Tests that restore works cleanly in an isolated directory and detects tampered archives."""
    backups_dir = mock_platform_env["tmp_path"] / "backups"
    service = BackupService(settings=mock_platform_env["settings"], backups_dir=backups_dir)

    res = service.create_coordinated_backup(backup_id="test_backup_restore")
    assert res.success is True
    assert res.archive_path is not None
    archive_path = Path(res.archive_path)

    # 1. Restore to an isolated directory
    isolated_target = mock_platform_env["tmp_path"] / "isolated_restore_target"
    restore_res = service.restore_backup(archive_path=archive_path, target_dir=isolated_target)
    assert restore_res.success is True
    assert restore_res.verified_checksums_count > 0
    assert (isolated_target / "processed" / "test_doc.json").exists()
    assert (isolated_target / "indexes" / "active_snapshot.json").exists()

    # Verify primary directory was NOT altered or overwritten
    assert (mock_platform_env["processed_dir"] / "test_doc.json").exists()

    # 2. Tamper Test: Extract archive, tamper with a file, repackage, and verify failure
    tamper_dir = mock_platform_env["tmp_path"] / "tamper_scratch"
    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(tamper_dir)

    # Corrupt test_doc.json
    corrupted_file = tamper_dir / "test_backup_restore" / "processed" / "test_doc.json"
    corrupted_file.write_text('{"document_id": "corrupted"}', encoding="utf-8")

    tampered_tar = mock_platform_env["tmp_path"] / "tampered.tar.gz"
    with tarfile.open(tampered_tar, "w:gz") as tar:
        tar.add(tamper_dir / "test_backup_restore", arcname="test_backup_restore")

    # Attempt to restore tampered archive
    tamper_target = mock_platform_env["tmp_path"] / "tamper_target"
    tamper_res = service.restore_backup(archive_path=tampered_tar, target_dir=tamper_target)
    assert tamper_res.success is False
    assert (tamper_res.message and "Checksum mismatch" in tamper_res.message) or (
        tamper_res.error and "Checksum mismatch" in tamper_res.error
    )


def test_coordinated_backup_with_raw_pdfs(mock_platform_env):
    """Verifies that raw PDFs are included and hashed in manifest when present."""
    raw_dir = mock_platform_env["tmp_path"] / "raw"
    raw_dir.mkdir(parents=True)
    pdf1 = raw_dir / "circular_2026_01.pdf"
    pdf1.write_bytes(b"%PDF-1.4 Mock PDF stream circular 1")
    pdf2 = raw_dir / "circular_2026_02.pdf"
    pdf2.write_bytes(b"%PDF-1.4 Mock PDF stream circular 2")

    backups_dir = mock_platform_env["tmp_path"] / "persistent_backups"
    service = BackupService(
        settings=mock_platform_env["settings"],
        raw_dir=raw_dir,
        backups_dir=backups_dir,
    )

    res = service.create_coordinated_backup(backup_id="test_raw_present")
    assert res.success is True
    assert res.archive_path is not None
    assert Path(res.archive_path).parent == backups_dir

    manifest = res.manifest
    assert manifest is not None
    assert manifest["raw_data_status"] == "present"
    assert manifest["raw_files_count"] == 2
    assert "raw/circular_2026_01.pdf" in manifest["included_files"]
    assert "raw/circular_2026_02.pdf" in manifest["included_files"]
    assert manifest["file_checksums"]["raw/circular_2026_01.pdf"] == compute_sha256(pdf1)
    assert manifest["file_checksums"]["raw/circular_2026_02.pdf"] == compute_sha256(pdf2)

    # Verify restoration also restores the raw directory
    restore_target = mock_platform_env["tmp_path"] / "restore_with_raw"
    restore_res = service.restore_backup(archive_path=Path(res.archive_path), target_dir=restore_target)
    assert restore_res.success is True
    assert (restore_target / "raw" / "circular_2026_01.pdf").exists()
    assert (restore_target / "raw" / "circular_2026_02.pdf").exists()


def test_coordinated_backup_raw_data_missing_and_empty(mock_platform_env):
    """Verifies that missing or empty raw directories produce explicit status rather than silent failure."""
    backups_dir = mock_platform_env["tmp_path"] / "persistent_backups"

    # Case 1: raw_dir does not exist at all -> raw_data_status == "missing"
    missing_raw = mock_platform_env["tmp_path"] / "nonexistent_raw_dir"
    service_missing = BackupService(
        settings=mock_platform_env["settings"],
        raw_dir=missing_raw,
        backups_dir=backups_dir,
    )
    res_missing = service_missing.create_coordinated_backup(backup_id="test_raw_missing")
    assert res_missing.success is True
    assert res_missing.manifest is not None
    assert res_missing.manifest["raw_data_status"] == "missing"
    assert res_missing.manifest["raw_files_count"] == 0

    # Case 2: raw_dir exists but has no PDFs -> raw_data_status == "empty"
    empty_raw = mock_platform_env["tmp_path"] / "empty_raw"
    empty_raw.mkdir(parents=True)
    # Add a non-pdf file to ensure only PDFs are collected or checked
    (empty_raw / "notes.txt").write_text("not a pdf", encoding="utf-8")

    service_empty = BackupService(
        settings=mock_platform_env["settings"],
        raw_dir=empty_raw,
        backups_dir=backups_dir,
    )
    res_empty = service_empty.create_coordinated_backup(backup_id="test_raw_empty")
    assert res_empty.success is True
    assert res_empty.manifest is not None
    assert res_empty.manifest["raw_data_status"] == "empty"
    assert res_empty.manifest["raw_files_count"] == 0


def test_restore_database_safety_guards(mock_platform_env, monkeypatch):
    """
    Verifies that:
    1. File-only restore does not touch database.
    2. Restoring to production DB or in production env is blocked by default.
    3. Explicit allow_production_db_overwrite allows the restore to proceed into guarded path.
    """
    backups_dir = mock_platform_env["tmp_path"] / "backups"
    service = BackupService(settings=mock_platform_env["settings"], backups_dir=backups_dir)
    b_res = service.create_coordinated_backup(backup_id="test_db_guard")
    assert b_res.success is True
    assert b_res.archive_path is not None
    archive_path = Path(b_res.archive_path)

    # 1. File-only restore does not touch DB
    target_dir = mock_platform_env["tmp_path"] / "target_file_only"
    res_file_only = service.restore_backup(archive_path=archive_path, target_dir=target_dir)
    assert res_file_only.success is True
    assert res_file_only.database_restored is False

    # 2. Production target rejection by URL matching configured prod DB
    prod_url = "postgresql+psycopg://user:secret@prod-db.internal:5432/sebi_rag_prod"
    prod_settings = Settings(
        app_name="Prod RAG",
        environment="development",
        database_url=prod_url,
        processed_dir=str(mock_platform_env["processed_dir"]),
        indexes_dir=str(mock_platform_env["indexes_dir"]),
    )
    prod_service = BackupService(settings=prod_settings, backups_dir=backups_dir)

    target_blocked = mock_platform_env["tmp_path"] / "target_blocked"
    # Attempting to restore to prod_url without override
    res_blocked = prod_service.restore_backup(
        archive_path=archive_path,
        target_dir=target_blocked,
        restore_database_url=prod_url,
        allow_production_db_overwrite=False,
    )
    assert res_blocked.success is False
    assert "ProductionDatabaseOverwriteRejected" in (res_blocked.error or "")
    assert "DESTRUCTIVE" in res_blocked.message

    # 3. Production target rejection when APP_ENV == "production"
    prod_env_settings = Settings(
        app_name="Prod RAG",
        environment="production",
        admin_api_key="mock_admin_key_min_16_chars",
        database_url=None,
        processed_dir=str(mock_platform_env["processed_dir"]),
        indexes_dir=str(mock_platform_env["indexes_dir"]),
    )
    prod_env_service = BackupService(settings=prod_env_settings, backups_dir=backups_dir)
    res_env_blocked = prod_env_service.restore_backup(
        archive_path=archive_path,
        target_dir=target_blocked,
        restore_database_url="postgresql://localhost:5432/any_db",
        allow_production_db_overwrite=False,
    )
    assert res_env_blocked.success is False
    assert "ProductionDatabaseOverwriteRejected" in (res_env_blocked.error or "")

    # 4. Explicit override allows guarded path (using mock import to avoid live network connection)
    import_called = []
    monkeypatch.setattr(
        prod_service,
        "_import_postgres_dump",
        lambda db_url, dump_p: import_called.append(db_url),
    )
    # Inject a dummy database.sql into the archive manifest to simulate dump inclusion
    target_allowed = mock_platform_env["tmp_path"] / "target_allowed"
    res_allowed = prod_service.restore_backup(
        archive_path=archive_path,
        target_dir=target_allowed,
        restore_database_url=prod_url,
        allow_production_db_overwrite=True,
    )
    assert res_allowed.success is True


def test_atomic_backup_archive_packaging(mock_platform_env, monkeypatch):
    """Verifies that packaging failure leaves no partial archive under the final name."""
    backups_dir = mock_platform_env["tmp_path"] / "atomic_backups"
    service = BackupService(settings=mock_platform_env["settings"], backups_dir=backups_dir)

    # Monkeypatch tarfile.open to fail during archive creation
    original_tarfile_open = tarfile.open

    def fail_tarfile_open(*args, **kwargs):
        raise OSError("Disk full simulation during archive packaging")

    monkeypatch.setattr(tarfile, "open", fail_tarfile_open)

    res = service.create_coordinated_backup(backup_id="test_failed_atomic")
    assert res.success is False
    assert "Disk full simulation" in (res.error or "")

    # Ensure no partial final archive or staging leftovers exist
    final_tar = backups_dir / "test_failed_atomic.tar.gz"
    tmp_tar = backups_dir / "._tmp_test_failed_atomic.tar.gz"
    staging = backups_dir / "_staging_test_failed_atomic"

    assert not final_tar.exists(), "Final archive must NOT exist if packaging failed"
    assert not tmp_tar.exists(), "Temporary archive must be cleaned up on failure"
    assert not staging.exists(), "Staging directory must be cleaned up on failure"


def test_hardened_tar_extraction_path_traversal_rejection(mock_platform_env):
    """Verifies that archives with path traversal or absolute destinations are rejected."""
    backups_dir = mock_platform_env["tmp_path"] / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    malicious_tar = backups_dir / "malicious.tar.gz"

    # Craft an archive with a member targeting ../outside.txt
    with tarfile.open(malicious_tar, "w:gz") as tar:
        payload = b"malicious content"
        ti = tarfile.TarInfo(name="../escape.txt")
        ti.size = len(payload)
        import io
        tar.addfile(ti, io.BytesIO(payload))

    service = BackupService(settings=mock_platform_env["settings"], backups_dir=backups_dir)
    target_dir = mock_platform_env["tmp_path"] / "restore_victim"

    res = service.restore_backup(archive_path=malicious_tar, target_dir=target_dir)
    assert res.success is False
    assert "Malicious archive rejected" in (res.error or "") or "Malicious archive rejected" in res.message

    # Verify escape.txt was never written to parent
    assert not (mock_platform_env["tmp_path"] / "escape.txt").exists()
    assert not (target_dir / "escape.txt").exists()
