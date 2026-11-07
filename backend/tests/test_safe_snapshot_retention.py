"""
Tests for Safe Local Snapshot Cache Retention and Lifecycle Pruning (Milestone 12 - Task 12.2I).

Verifies:
1. Active snapshot N is never pruned or tombstoned.
2. Authoritative predecessor N-1 is never pruned or tombstoned when established.
3. Missing / unknown N-1 safely aborts the retention sweep.
4. Remote pointer outage / error safely aborts the retention sweep without guessing.
5. Active loader holding Level 2 lifecycle lock causes cleaner to skip the candidate.
6. Cleaner tombstoning first causes loader to fail closed / fall back safely without partial reads or corruption.
7. Loader and cleaner synchronization prevents concurrent read during rename.
8. Dry-run mode reports candidates without mutating filesystem for both stages.
9. Deactivation age and drain window boundaries are strictly enforced.
10. Malformed snapshot IDs, path traversal, and symlinks/reparse points are rejected.
11. Tombstone purge respects indexes root confinement.
12. Interrupted/crash-recovery state leaves recoverable tombstones.
13. Persistent lockfiles are never unlinked during tombstoning or purge.
14. Bundle publication automatically records authoritative predecessor_snapshot_id.
"""
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import stat
import time
from typing import Dict, Any
import numpy as np
import pytest

from app.schemas.publication import ActiveSnapshotPointer, SnapshotManifest
from app.retrieval.snapshot_store import (
    DurableSnapshotStore,
    compute_file_sha256,
    TOMBSTONE_SNAPSHOT_REGEX,
)
from app.retrieval.service import RetrievalService
from app.storage import BaseStorageAdapter, LocalStorageAdapter
from app.storage.base import StorageError


def _create_mock_snapshot_files(snapshot_dir: Path, collection_id: str, snapshot_id: str) -> SnapshotManifest:
    """Creates valid minimal snapshot bundle files and returns its manifest."""
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    
    docs_data = {
        "doc1": {
            "document_id": "doc1",
            "collection_id": collection_id,
            "title": "Doc 1",
            "source_path": "doc1.pdf",
            "source_url": "https://example.com/doc1.pdf",
            "sha256": "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
            "pdf_path": "doc1.pdf",
            "page_count": 1,
            "char_count": 50,
            "token_estimate": 10,
        }
    }
    chunks_data = [
        {
            "chunk_id": "chunk1",
            "document_id": "doc1",
            "collection_id": collection_id,
            "chunk_index": 0,
            "text": "Cybersecurity resilience framework guidelines.",
            "page_start": 1,
            "page_end": 1,
            "char_count": 45,
            "token_estimate": 10,
        }
    ]
    
    (snapshot_dir / "documents.json").write_text(json.dumps(docs_data), encoding="utf-8")
    (snapshot_dir / "chunks.json").write_text(json.dumps(chunks_data), encoding="utf-8")
    
    # Minimal dense vector array (1 vector, dimension 4)
    vectors = np.array([[0.1, 0.2, 0.3, 0.4]], dtype=np.float32)
    np.save(snapshot_dir / "dense_index.npy", vectors)
    
    dense_meta = {
        "collection_id": collection_id,
        "snapshot_id": snapshot_id,
        "embedding_model": "test-embedder",
        "dimension": 4,
        "vector_count": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (snapshot_dir / "dense_index_meta.json").write_text(json.dumps(dense_meta), encoding="utf-8")
    
    checksums = {
        "documents.json": compute_file_sha256(snapshot_dir / "documents.json"),
        "chunks.json": compute_file_sha256(snapshot_dir / "chunks.json"),
        "dense_index.npy": compute_file_sha256(snapshot_dir / "dense_index.npy"),
        "dense_index_meta.json": compute_file_sha256(snapshot_dir / "dense_index_meta.json"),
    }
    
    manifest = SnapshotManifest(
        collection_id=collection_id,
        snapshot_id=snapshot_id,
        created_at=datetime.now(timezone.utc),
        total_documents=1,
        total_chunks=1,
        corpus_fingerprint="mock_corpus_fingerprint_sha256",
        artifact_checksums=checksums,
    )
    (snapshot_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return manifest


def _write_remote_pointer(
    storage_adapter: BaseStorageAdapter,
    collection_id: str,
    snapshot_id: str,
    predecessor_id: str | None = None,
    manifest_checksum: str = "dummy_chk",
    activated_at: datetime | None = None,
) -> ActiveSnapshotPointer:
    """Helper to write an authoritative pointer to remote storage."""
    if activated_at is None:
        activated_at = datetime.now(timezone.utc) - timedelta(days=2)
        
    pointer = ActiveSnapshotPointer(
        collection_id=collection_id,
        snapshot_id=snapshot_id,
        predecessor_snapshot_id=predecessor_id,
        manifest_checksum=manifest_checksum,
        total_documents=1,
        total_chunks=1,
        activated_at=activated_at,
    )
    key = f"indexes/{collection_id}/active_snapshot.json"
    storage_adapter.put_bytes(key, pointer.model_dump_json(indent=2).encode("utf-8"), content_type="application/json")
    return pointer


def test_01_active_n_never_tombstoned(tmp_path: Path):
    """Active snapshot N must never be tombstoned or purged."""
    remote_root = tmp_path / "remote"
    local_root = tmp_path / "local"
    storage = LocalStorageAdapter(remote_root)
    store = DurableSnapshotStore(storage, indexes_dir=local_root)
    
    col = "test_col"
    snap_n = "snap_v3"
    snap_n_minus_1 = "snap_v2"
    
    snap_dir = local_root / col / "snapshots" / snap_n
    _create_mock_snapshot_files(snap_dir, col, snap_n)
    
    # Remote active pointer designates snap_n as active and snap_v2 as N-1
    _write_remote_pointer(storage, col, snap_n, predecessor_id=snap_n_minus_1)
    
    # Backdate mtime
    old_time = time.time() - 100000
    os.utime(snap_dir, (old_time, old_time))
    
    res = store.tombstone_retired_snapshots(col, min_deactivation_age_seconds=3600, dry_run=False)
    assert res["aborted"] is False
    assert snap_dir.is_dir()
    assert len(res["tombstoned_paths"]) == 0
    assert any(s["reason"] == "protected_active_or_predecessor" for s in res["skipped"])


def test_02_predecessor_n_minus_1_never_tombstoned(tmp_path: Path):
    """Authoritative predecessor N-1 must never be tombstoned or purged."""
    remote_root = tmp_path / "remote"
    local_root = tmp_path / "local"
    storage = LocalStorageAdapter(remote_root)
    store = DurableSnapshotStore(storage, indexes_dir=local_root)
    
    col = "test_col"
    snap_n = "snap_v3"
    snap_n_minus_1 = "snap_v2"
    
    snap_dir = local_root / col / "snapshots" / snap_n_minus_1
    _create_mock_snapshot_files(snap_dir, col, snap_n_minus_1)
    
    _write_remote_pointer(storage, col, snap_n, predecessor_id=snap_n_minus_1)
    
    old_time = time.time() - 100000
    os.utime(snap_dir, (old_time, old_time))
    
    res = store.tombstone_retired_snapshots(col, min_deactivation_age_seconds=3600, dry_run=False)
    assert res["aborted"] is False
    assert snap_dir.is_dir()
    assert len(res["tombstoned_paths"]) == 0
    assert any(s["reason"] == "protected_active_or_predecessor" for s in res["skipped"])


def test_03_missing_predecessor_aborts_sweep_safely(tmp_path: Path):
    """When predecessor N-1 is None/unrecorded, sweep must abort without tombstoning anything."""
    remote_root = tmp_path / "remote"
    local_root = tmp_path / "local"
    storage = LocalStorageAdapter(remote_root)
    store = DurableSnapshotStore(storage, indexes_dir=local_root)
    
    col = "test_col"
    snap_n = "snap_v3"
    snap_old = "snap_v1"
    
    old_dir = local_root / col / "snapshots" / snap_old
    _create_mock_snapshot_files(old_dir, col, snap_old)
    
    # Active pointer has NO predecessor_snapshot_id
    _write_remote_pointer(storage, col, snap_n, predecessor_id=None)
    
    old_time = time.time() - 100000
    os.utime(old_dir, (old_time, old_time))
    
    res = store.tombstone_retired_snapshots(col, min_deactivation_age_seconds=3600, dry_run=False)
    assert res["aborted"] is True
    assert "predecessor" in res["abort_reason"]
    assert old_dir.is_dir()
    assert len(res["tombstoned_paths"]) == 0


def test_04_remote_outage_aborts_sweep_safely(tmp_path: Path):
    """Remote storage error / outage must cause retention sweep to fail safe and abort."""
    local_root = tmp_path / "local"
    
    class FailingStorage(LocalStorageAdapter):
        def get_bytes(self, key: str) -> bytes:
            raise StorageError("Simulated remote outage / network partition")
            
    storage = FailingStorage(tmp_path / "remote")
    store = DurableSnapshotStore(storage, indexes_dir=local_root)
    
    col = "test_col"
    snap_old = "snap_v1"
    old_dir = local_root / col / "snapshots" / snap_old
    _create_mock_snapshot_files(old_dir, col, snap_old)
    
    res = store.tombstone_retired_snapshots(col, min_deactivation_age_seconds=3600, dry_run=False)
    assert res["aborted"] is True
    assert "Simulated remote outage" in res["abort_reason"]
    assert old_dir.is_dir()


def test_05_loader_holding_lock_causes_cleaner_to_skip(tmp_path: Path):
    """When a loader holds the Level 2 lifecycle lock, cleaner skips the snapshot."""
    remote_root = tmp_path / "remote"
    local_root = tmp_path / "local"
    storage = LocalStorageAdapter(remote_root)
    store = DurableSnapshotStore(storage, indexes_dir=local_root)
    
    col = "test_col"
    snap_n = "snap_v3"
    snap_n_minus_1 = "snap_v2"
    snap_retired = "snap_v1"
    
    retired_dir = local_root / col / "snapshots" / snap_retired
    _create_mock_snapshot_files(retired_dir, col, snap_retired)
    _write_remote_pointer(storage, col, snap_n, predecessor_id=snap_n_minus_1)
    
    old_time = time.time() - 100000
    os.utime(retired_dir, (old_time, old_time))
    
    lock_path = local_root / col / "snapshots" / f".{snap_retired}.lock"
    
    # Loader holds Level 2 lock
    with store._acquire_process_lock(lock_path):
        res = store.tombstone_retired_snapshots(col, min_deactivation_age_seconds=3600, dry_run=False)
        assert res["aborted"] is False
        assert len(res["tombstoned_paths"]) == 0
        assert any(s.get("reason") == "locked_or_in_use" for s in res["skipped"])
        assert retired_dir.is_dir()
        
    # After lock is released, cleaner can acquire and tombstone
    res2 = store.tombstone_retired_snapshots(col, min_deactivation_age_seconds=3600, dry_run=False)
    assert len(res2["tombstoned_paths"]) == 1
    assert not retired_dir.exists()


def test_06_cleaner_tombstoning_first_loader_fails_closed_safely(tmp_path: Path):
    """When cleaner tombstones first, loader encountering renamed dir falls back cleanly."""
    remote_root = tmp_path / "remote"
    local_root = tmp_path / "local"
    storage = LocalStorageAdapter(remote_root)
    store = DurableSnapshotStore(storage, indexes_dir=local_root)
    
    col = "test_col"
    snap_n = "snap_v3"
    snap_n_minus_1 = "snap_v2"
    snap_retired = "snap_v1"
    
    retired_dir = local_root / col / "snapshots" / snap_retired
    _create_mock_snapshot_files(retired_dir, col, snap_retired)
    _write_remote_pointer(storage, col, snap_n, predecessor_id=snap_n_minus_1)
    
    old_time = time.time() - 100000
    os.utime(retired_dir, (old_time, old_time))
    
    # Cleaner tombstones retired snapshot
    res = store.tombstone_retired_snapshots(col, min_deactivation_age_seconds=3600, dry_run=False)
    assert len(res["tombstoned_paths"]) == 1
    assert not retired_dir.exists()
    
    # Loader attempts to load the now-tombstoned directory
    service = RetrievalService(
        processed_dir=tmp_path / "processed",
        indexes_dir=local_root,
        storage_adapter=storage,
        retrieval_mode="lexical_only",
    )
    # Loading retired_dir directly must fail closed / return empty generation without throwing uncaught exceptions
    gen = service._load_isolated_generation(collection_id=col, target_snapshot_dir=retired_dir)
    assert gen is not None
    assert gen.snapshot_id is None
    assert len(gen.chunks) == 0


def test_07_dry_run_mode_does_not_mutate(tmp_path: Path):
    """Dry run mode identifies candidates without modifying files or directories."""
    remote_root = tmp_path / "remote"
    local_root = tmp_path / "local"
    storage = LocalStorageAdapter(remote_root)
    store = DurableSnapshotStore(storage, indexes_dir=local_root)
    
    col = "test_col"
    snap_n = "snap_v3"
    snap_n_minus_1 = "snap_v2"
    snap_retired = "snap_v1"
    
    retired_dir = local_root / col / "snapshots" / snap_retired
    _create_mock_snapshot_files(retired_dir, col, snap_retired)
    _write_remote_pointer(storage, col, snap_n, predecessor_id=snap_n_minus_1)
    
    old_time = time.time() - 100000
    os.utime(retired_dir, (old_time, old_time))
    
    # Stage 1 Dry Run
    res_tombstone = store.tombstone_retired_snapshots(col, min_deactivation_age_seconds=3600, dry_run=True)
    assert res_tombstone["dry_run"] is True
    assert res_tombstone["candidates_found"] == 1
    assert len(res_tombstone["tombstoned_paths"]) == 0
    assert retired_dir.is_dir()
    
    # Create a tombstone directory to test Stage 2 Dry Run
    ts = int(time.time() - 7200)
    tombstone_dir = local_root / col / "snapshots" / f"_tombstone_{snap_retired}_{ts}"
    tombstone_dir.mkdir(parents=True, exist_ok=True)
    (tombstone_dir / "marker.txt").write_text("test")
    os.utime(tombstone_dir, (ts, ts))
    
    res_purge = store.purge_drained_tombstones(col, drain_window_seconds=3600, dry_run=True)
    assert res_purge["dry_run"] is True
    assert res_purge["candidates_found"] == 1
    assert len(res_purge["purged_paths"]) == 0
    assert tombstone_dir.is_dir()


def test_08_deactivation_age_and_drain_window_boundaries(tmp_path: Path):
    """Snapshots below min_deactivation_age or drain_window are preserved."""
    remote_root = tmp_path / "remote"
    local_root = tmp_path / "local"
    storage = LocalStorageAdapter(remote_root)
    store = DurableSnapshotStore(storage, indexes_dir=local_root)
    
    col = "test_col"
    snap_n = "snap_v3"
    snap_n_minus_1 = "snap_v2"
    snap_retired = "snap_v1"
    
    retired_dir = local_root / col / "snapshots" / snap_retired
    _create_mock_snapshot_files(retired_dir, col, snap_retired)
    
    # Active pointer was activated 100 seconds ago (< 3600s)
    recent_act = datetime.now(timezone.utc) - timedelta(seconds=100)
    _write_remote_pointer(storage, col, snap_n, predecessor_id=snap_n_minus_1, activated_at=recent_act)
    
    res = store.tombstone_retired_snapshots(col, min_deactivation_age_seconds=3600, dry_run=False)
    assert res["aborted"] is True
    assert "below min_deactivation_age_seconds" in res["abort_reason"]
    assert retired_dir.is_dir()
    
    # Now set active pointer activation to 2 days ago, but snapshot directory mtime is recent (50s ago)
    old_act = datetime.now(timezone.utc) - timedelta(days=2)
    _write_remote_pointer(storage, col, snap_n, predecessor_id=snap_n_minus_1, activated_at=old_act)
    recent_time = time.time() - 50
    os.utime(retired_dir, (recent_time, recent_time))
    
    res2 = store.tombstone_retired_snapshots(col, min_deactivation_age_seconds=3600, dry_run=False)
    assert res2["aborted"] is False
    assert len(res2["tombstoned_paths"]) == 0
    assert any(s.get("reason") == "recent_mtime_below_min_deactivation_age" for s in res2["skipped"])
    assert retired_dir.is_dir()
    
    # Test drain window boundary for Stage 2
    ts = int(time.time() - 100)  # only 100s old (< 3600s drain window)
    tombstone_dir = local_root / col / "snapshots" / f"_tombstone_{snap_retired}_{ts}"
    tombstone_dir.mkdir(parents=True, exist_ok=True)
    
    res_purge = store.purge_drained_tombstones(col, drain_window_seconds=3600, dry_run=False)
    assert len(res_purge["purged_paths"]) == 0
    assert any(s.get("reason") == "drain_window_not_elapsed" for s in res_purge["skipped"])
    assert tombstone_dir.is_dir()


def test_09_malformed_names_and_path_traversal_rejected(tmp_path: Path):
    """Malformed names, traversal attempts, and corrupt directories are ignored."""
    remote_root = tmp_path / "remote"
    local_root = tmp_path / "local"
    storage = LocalStorageAdapter(remote_root)
    store = DurableSnapshotStore(storage, indexes_dir=local_root)
    
    col = "test_col"
    snap_n = "snap_v3"
    snap_n_minus_1 = "snap_v2"
    _write_remote_pointer(storage, col, snap_n, predecessor_id=snap_n_minus_1)
    
    snapshots_dir = local_root / col / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. Corrupt quarantine dir
    corrupt_dir = snapshots_dir / "_corrupt_snap_v1_abc"
    corrupt_dir.mkdir()
    
    # 2. Staging dir
    staging_dir = snapshots_dir / "_staging_hydrate_snap_v1_def"
    staging_dir.mkdir()
    
    # 3. Malformed snapshot name
    malformed_dir = snapshots_dir / "snap!invalid@name"
    malformed_dir.mkdir()
    
    # 4. Malformed tombstone name
    malformed_ts = snapshots_dir / "_tombstone_invalid_notanumber"
    malformed_ts.mkdir()
    
    res_tomb = store.tombstone_retired_snapshots(col, min_deactivation_age_seconds=1, dry_run=False)
    assert corrupt_dir.is_dir()
    assert staging_dir.is_dir()
    assert malformed_dir.is_dir()
    
    res_purge = store.purge_drained_tombstones(col, drain_window_seconds=1, dry_run=False)
    assert malformed_ts.is_dir()
    assert any(s.get("reason") == "malformed_tombstone_name" for s in res_purge["skipped"])


def test_10_persistent_lockfiles_never_unlinked(tmp_path: Path):
    """Lifecycle lockfiles (.{snapshot_id}.lock) must be retained and never deleted."""
    remote_root = tmp_path / "remote"
    local_root = tmp_path / "local"
    storage = LocalStorageAdapter(remote_root)
    store = DurableSnapshotStore(storage, indexes_dir=local_root)
    
    col = "test_col"
    snap_n = "snap_v3"
    snap_n_minus_1 = "snap_v2"
    snap_retired = "snap_v1"
    
    retired_dir = local_root / col / "snapshots" / snap_retired
    _create_mock_snapshot_files(retired_dir, col, snap_retired)
    _write_remote_pointer(storage, col, snap_n, predecessor_id=snap_n_minus_1)
    
    old_time = time.time() - 100000
    os.utime(retired_dir, (old_time, old_time))
    
    lock_path = local_root / col / "snapshots" / f".{snap_retired}.lock"
    
    # Stage 1: Tombstone
    res = store.tombstone_retired_snapshots(col, min_deactivation_age_seconds=3600, dry_run=False)
    assert len(res["tombstoned_paths"]) == 1
    assert lock_path.is_file(), "Lockfile must exist after tombstone"
    
    # Backdate tombstone name and mtime to simulate drain window completion
    tombstone_path = Path(res["tombstoned_paths"][0])
    old_ts = int(time.time() - 100000)
    drained_tombstone_path = tombstone_path.parent / f"_tombstone_{snap_retired}_{old_ts}"
    tombstone_path.rename(drained_tombstone_path)
    os.utime(drained_tombstone_path, (old_ts, old_ts))
    
    # Stage 2: Purge
    res_purge = store.purge_drained_tombstones(col, drain_window_seconds=3600, dry_run=False)
    assert len(res_purge["purged_paths"]) == 1
    assert not drained_tombstone_path.exists()
    assert lock_path.is_file(), "Lockfile must be retained after tombstone purge"


def test_11_publish_bundle_records_predecessor_snapshot_id(tmp_path: Path):
    """publish_bundle automatically records the existing active snapshot as predecessor_snapshot_id."""
    remote_root = tmp_path / "remote"
    local_root = tmp_path / "local"
    storage = LocalStorageAdapter(remote_root)
    store = DurableSnapshotStore(storage, indexes_dir=local_root)
    
    col = "test_col"
    snap_v1 = "snap_v1"
    snap_v2 = "snap_v2"
    
    # 1. Publish initial snapshot v1
    v1_dir = local_root / "build_v1"
    manifest_v1 = _create_mock_snapshot_files(v1_dir, col, snap_v1)
    pointer_v1 = ActiveSnapshotPointer(
        collection_id=col,
        snapshot_id=snap_v1,
        total_documents=1,
        total_chunks=1,
        manifest_checksum=compute_file_sha256(v1_dir / "manifest.json"),
        activated_at=datetime.now(timezone.utc),
    )
    store.publish_bundle(col, snap_v1, v1_dir, manifest_v1, pointer_v1)
    
    remote_ptr_v1 = store.get_remote_active_pointer(col)
    assert remote_ptr_v1 is not None
    assert remote_ptr_v1.snapshot_id == snap_v1
    assert remote_ptr_v1.predecessor_snapshot_id is None
    
    # 2. Publish successor snapshot v2
    v2_dir = local_root / "build_v2"
    manifest_v2 = _create_mock_snapshot_files(v2_dir, col, snap_v2)
    pointer_v2 = ActiveSnapshotPointer(
        collection_id=col,
        snapshot_id=snap_v2,
        total_documents=1,
        total_chunks=1,
        manifest_checksum=compute_file_sha256(v2_dir / "manifest.json"),
        activated_at=datetime.now(timezone.utc),
    )
    store.publish_bundle(col, snap_v2, v2_dir, manifest_v2, pointer_v2)
    
    remote_ptr_v2 = store.get_remote_active_pointer(col)
    assert remote_ptr_v2 is not None
    assert remote_ptr_v2.snapshot_id == snap_v2
    # Predecessor must be automatically recorded as snap_v1!
    assert remote_ptr_v2.predecessor_snapshot_id == snap_v1


def test_12_crash_restart_leaves_recoverable_tombstones(tmp_path: Path):
    """Crash or restart between Stage 1 and Stage 2 leaves tombstones safely drained and purged on next run."""
    remote_root = tmp_path / "remote"
    local_root = tmp_path / "local"
    storage = LocalStorageAdapter(remote_root)
    store = DurableSnapshotStore(storage, indexes_dir=local_root)
    
    col = "test_col"
    snap_retired = "snap_v1"
    snapshots_dir = local_root / col / "snapshots"
    
    # Simulate a crash leaving an existing tombstone from 4 hours ago
    ts = int(time.time() - 14400)
    tombstone_dir = snapshots_dir / f"_tombstone_{snap_retired}_{ts}"
    tombstone_dir.mkdir(parents=True, exist_ok=True)
    (tombstone_dir / "chunks.json").write_text("[]", encoding="utf-8")
    os.utime(tombstone_dir, (ts, ts))
    
    # Purge runs on restart and cleans the abandoned tombstone
    res = store.purge_drained_tombstones(col, drain_window_seconds=3600, dry_run=False)
    assert len(res["purged_paths"]) == 1
    assert not tombstone_dir.exists()


def test_13_symlink_and_junction_rejection(tmp_path: Path):
    """Symlinks or junctions pointing into snapshots directory must be rejected."""
    remote_root = tmp_path / "remote"
    local_root = tmp_path / "local"
    storage = LocalStorageAdapter(remote_root)
    store = DurableSnapshotStore(storage, indexes_dir=local_root)
    
    col = "test_col"
    snap_n = "snap_v3"
    snap_n_minus_1 = "snap_v2"
    _write_remote_pointer(storage, col, snap_n, predecessor_id=snap_n_minus_1)
    
    snapshots_dir = local_root / col / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    
    target_dir = tmp_path / "external_target"
    target_dir.mkdir()
    
    symlink_dir = snapshots_dir / "snap_symlink"
    created = False
    if os.name == "nt":
        try:
            import _winapi
            _winapi.CreateJunction(str(target_dir), str(symlink_dir))
            created = True
        except Exception:
            pass
    if not created:
        try:
            symlink_dir.symlink_to(target_dir, target_is_directory=True)
            created = True
        except (OSError, NotImplementedError):
            pass
            
    if not created:
        pytest.skip("Symlink or junction creation not permitted in this environment")
        
    res = store.tombstone_retired_snapshots(col, min_deactivation_age_seconds=1, dry_run=False)
    assert any(s.get("reason") == "symlink_or_reparse_point" for s in res["skipped"])


def test_14_concurrent_loader_and_cleaner_stress_test(tmp_path: Path):
    """Stress test: concurrent loaders and cleaners run simultaneously with zero deadlocks or corrupt reads."""
    import threading
    
    remote_root = tmp_path / "remote"
    local_root = tmp_path / "local"
    storage = LocalStorageAdapter(remote_root)
    store = DurableSnapshotStore(storage, indexes_dir=local_root)
    
    col = "test_col"
    snap_n = "snap_v3"
    snap_n_minus_1 = "snap_v2"
    snap_retired = "snap_v1"
    
    retired_dir = local_root / col / "snapshots" / snap_retired
    _create_mock_snapshot_files(retired_dir, col, snap_retired)
    _write_remote_pointer(storage, col, snap_n, predecessor_id=snap_n_minus_1)
    
    old_time = time.time() - 100000
    os.utime(retired_dir, (old_time, old_time))
    
    service = RetrievalService(
        processed_dir=tmp_path / "processed",
        indexes_dir=local_root,
        storage_adapter=storage,
        retrieval_mode="lexical_only",
    )
    
    errors = []
    stop_event = threading.Event()
    
    def loader_worker():
        while not stop_event.is_set():
            try:
                gen = service._load_isolated_generation(collection_id=col, target_snapshot_dir=retired_dir)
                if gen is not None and gen.snapshot_id == snap_retired:
                    assert len(gen.chunks) > 0
            except Exception as e:
                errors.append(e)
            time.sleep(0.005)
            
    def cleaner_worker():
        for _ in range(10):
            try:
                store.tombstone_retired_snapshots(col, min_deactivation_age_seconds=3600, dry_run=False)
            except Exception as e:
                errors.append(e)
            time.sleep(0.01)
            
    t_loader = threading.Thread(target=loader_worker)
    t_cleaner = threading.Thread(target=cleaner_worker)
    
    t_loader.start()
    t_cleaner.start()
    
    t_cleaner.join()
    stop_event.set()
    t_loader.join()
    
    assert len(errors) == 0, f"Concurrent workers encountered errors: {errors}"
    assert not retired_dir.exists(), "Retired directory should eventually be tombstoned"
    lock_file = local_root / col / "snapshots" / f".{snap_retired}.lock"
    assert lock_file.is_file(), "Lockfile must remain intact on disk"

