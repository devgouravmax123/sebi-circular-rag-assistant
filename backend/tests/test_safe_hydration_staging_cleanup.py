"""
Milestone 12 Wave 2 - Task 12.2D Phase B: Safe Hydration Staging Cleanup Tests.

Validates:
1. Dry-run reports eligible abandoned staging without deleting it.
2. Expired abandoned staging is deleted only when ownership lock can be acquired.
3. A live hydrator holding the ownership lock is skipped.
4. Recent staging is preserved.
5. Malformed staging names are ignored.
6. Completed snapshot directories are never touched.
7. `_corrupt_*` quarantine directories are never touched.
8. Symlinks/reparse points are rejected where supported.
9. Root confinement prevents traversal or outside-root deletion.
10. Cleanup respects max_entries.
11. Lock release occurs after hydration success and failure.
12. Concurrent cleanup and hydration cannot delete an active staging directory.
13. Cleanup errors are reported but do not break query or publication paths.
"""
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from typing import Dict, Any, Optional, Tuple, BinaryIO
from unittest.mock import patch
import pytest

from app.schemas.publication import SnapshotManifest, ActiveSnapshotPointer
from app.retrieval.snapshot_store import (
    DurableSnapshotStore,
    SnapshotIntegrityError,
    REQUIRED_BUNDLE_ARTIFACTS,
    compute_file_sha256,
)
from app.storage.base import (
    BaseStorageAdapter,
    StorageObjectMetadata,
    StorageFileNotFoundError,
    ConditionalWriteResult,
)


class InMemoryStorageAdapter(BaseStorageAdapter):
    """Deterministic in-memory storage adapter for cleanup and hydration tests."""

    def __init__(self):
        self.objects: Dict[str, bytes] = {}
        self.download_call_counts: Dict[str, int] = {}
        self._lock = threading.RLock()

    def get_metadata(self, key: str) -> Optional[StorageObjectMetadata]:
        with self._lock:
            if key not in self.objects:
                return None
            data = self.objects[key]
            return StorageObjectMetadata(
                key=key,
                source_uri=f"mem://{key}",
                byte_count=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                etag=hashlib.sha256(data).hexdigest(),
            )

    def get_bytes(self, key: str) -> bytes:
        with self._lock:
            if key not in self.objects:
                raise StorageFileNotFoundError(f"Key not found: {key}")
            return self.objects[key]

    def put_bytes(self, key: str, data: bytes, content_type: Optional[str] = None) -> StorageObjectMetadata:
        with self._lock:
            self.objects[key] = data
            return self.get_metadata(key)

    def put_bytes_conditional(
        self,
        key: str,
        data: bytes,
        expected_validator: Optional[str] = None,
        content_type: Optional[str] = None,
    ) -> ConditionalWriteResult:
        with self._lock:
            self.objects[key] = data
            sha = hashlib.sha256(data).hexdigest()
            return ConditionalWriteResult(success=True, validator=sha, byte_count=len(data))

    def upload_file(self, source_path: str | Path, key: str, content_type: Optional[str] = None) -> StorageObjectMetadata:
        data = Path(source_path).read_bytes()
        return self.put_bytes(key, data, content_type=content_type)

    def download_file(self, key: str, destination_path: str | Path) -> None:
        with self._lock:
            self.download_call_counts[key] = self.download_call_counts.get(key, 0) + 1
            if key not in self.objects:
                raise StorageFileNotFoundError(f"Key not found: {key}")
            data = self.objects[key]
        dest = Path(destination_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    def delete_object(self, key: str) -> bool:
        with self._lock:
            return self.objects.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        with self._lock:
            return key in self.objects

    def delete_file(self, file_path: str) -> bool:
        return self.delete_object(file_path)

    def open_stream(self, key: str) -> BinaryIO:
        data = self.get_bytes(key)
        return io.BytesIO(data)

    def save_upload_stream(
        self,
        stream: BinaryIO,
        key: str,
        max_bytes: Optional[int] = None,
        content_type: Optional[str] = None,
    ) -> StorageObjectMetadata:
        data = stream.read()
        return self.put_bytes(key, data, content_type=content_type)

    def materialize_to_local(
        self,
        key: str,
        target_local_path: Optional[str | Path] = None,
    ) -> Path:
        if target_local_path:
            p = Path(target_local_path)
        else:
            fd, tmp = tempfile.mkstemp()
            os.close(fd)
            p = Path(tmp)
        self.download_file(key, p)
        return p

    def list_keys(self, prefix: str) -> list[str]:
        with self._lock:
            return [k for k in self.objects if k.startswith(prefix)]


def seed_remote_bundle(
    adapter: InMemoryStorageAdapter,
    collection_id: str,
    snapshot_id: str,
    manifest_data: Optional[Dict[str, Any]] = None,
) -> Tuple[SnapshotManifest, ActiveSnapshotPointer]:
    """Helper to seed remote bundle artifacts and active pointer in memory."""
    prefix = f"indexes/{collection_id}/snapshots/{snapshot_id}"
    chunk_bytes = json.dumps([{"chunk_id": "c1", "content": "text"}]).encode("utf-8")
    doc_bytes = json.dumps({"d1": {"title": "Doc 1"}}).encode("utf-8")
    npy_bytes = b"NUMPY_MOCK_BYTES_V1"
    meta_bytes = json.dumps({"dims": 384, "count": 1}).encode("utf-8")

    adapter.put_bytes(f"{prefix}/chunks.json", chunk_bytes)
    adapter.put_bytes(f"{prefix}/documents.json", doc_bytes)
    adapter.put_bytes(f"{prefix}/dense_index.npy", npy_bytes)
    adapter.put_bytes(f"{prefix}/dense_index_meta.json", meta_bytes)

    checksums = {
        "chunks.json": hashlib.sha256(chunk_bytes).hexdigest(),
        "documents.json": hashlib.sha256(doc_bytes).hexdigest(),
        "dense_index.npy": hashlib.sha256(npy_bytes).hexdigest(),
        "dense_index_meta.json": hashlib.sha256(meta_bytes).hexdigest(),
    }

    manifest = SnapshotManifest(
        format_version="1.0.0",
        collection_id=collection_id,
        snapshot_id=snapshot_id,
        created_at=datetime.now(timezone.utc),
        artifact_checksums=checksums,
        total_chunks=1,
        total_documents=1,
        corpus_fingerprint="mock_corpus_fingerprint_001",
    )
    manifest_bytes = manifest.model_dump_json(indent=2).encode("utf-8")
    adapter.put_bytes(f"{prefix}/manifest.json", manifest_bytes)
    manifest_chk = hashlib.sha256(manifest_bytes).hexdigest()

    pointer = ActiveSnapshotPointer(
        collection_id=collection_id,
        snapshot_id=snapshot_id,
        manifest_checksum=manifest_chk,
        activated_at=datetime.now(timezone.utc),
        total_documents=1,
        total_chunks=1,
    )
    pointer_bytes = pointer.model_dump_json(indent=2).encode("utf-8")
    adapter.put_bytes(f"indexes/{collection_id}/active_snapshot.json", pointer_bytes)
    return manifest, pointer


def set_dir_mtime(path: Path, past_seconds: int) -> None:
    """Sets the mtime and atime of a directory to a past timestamp."""
    target_time = time.time() - past_seconds
    os.utime(path, (target_time, target_time))


# =============================================================================
# Test 1: Dry-run reports eligible abandoned staging without deleting it
# =============================================================================
def test_dry_run_reports_without_deleting(tmp_path: Path):
    adapter = InMemoryStorageAdapter()
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path)
    cid = "col_test_cleanup"
    snap_dir = tmp_path / cid / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)

    staging = snap_dir / "_staging_hydrate_snap001_deadbeef"
    staging.mkdir()
    (staging / "partial_chunk.json").write_text("partial data", encoding="utf-8")
    set_dir_mtime(staging, past_seconds=7200)

    res = store.cleanup_abandoned_hydration_staging(cid, min_age_seconds=3600, dry_run=True)

    assert res["dry_run"] is True
    assert res["candidates_found"] == 1
    assert str(staging) in res["candidates"]
    assert len(res["deleted_paths"]) == 0
    assert staging.is_dir(), "Staging directory must NOT be deleted in dry_run mode"
    assert (staging / "partial_chunk.json").is_file()


# =============================================================================
# Test 2: Expired abandoned staging is deleted only when ownership lock can be acquired
# =============================================================================
def test_expired_staging_deleted_when_lock_acquirable(tmp_path: Path):
    adapter = InMemoryStorageAdapter()
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path)
    cid = "col_test_cleanup"
    snap_dir = tmp_path / cid / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)

    staging = snap_dir / "_staging_hydrate_snap001_deadbeef"
    staging.mkdir()
    (staging / "partial_chunk.json").write_text("partial data", encoding="utf-8")
    set_dir_mtime(staging, past_seconds=7200)

    # Pre-create lockfile to verify it gets cleaned up too
    lock_file = snap_dir / f".{staging.name}.lock"
    lock_file.write_bytes(b"")

    res = store.cleanup_abandoned_hydration_staging(cid, min_age_seconds=3600, dry_run=False)

    assert res["dry_run"] is False
    assert res["candidates_found"] == 1
    assert str(staging) in res["deleted_paths"]
    assert not staging.exists(), "Abandoned staging directory should be removed"
    assert lock_file.exists(), "Associated staging lock file must be retained as persistent coordination file"


# =============================================================================
# Test 2B: A subsequent process/thread can acquire the retained lockfile
# =============================================================================
def test_subsequent_process_can_acquire_retained_lockfile(tmp_path: Path):
    adapter = InMemoryStorageAdapter()
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path)
    cid = "col_test_cleanup"
    snap_dir = tmp_path / cid / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)

    staging = snap_dir / "_staging_hydrate_snap001_reacquire"
    staging.mkdir()
    set_dir_mtime(staging, past_seconds=7200)

    lock_file = snap_dir / f".{staging.name}.lock"
    lock_file.write_bytes(b"")

    # Run cleanup to delete directory while retaining lockfile
    res = store.cleanup_abandoned_hydration_staging(cid, min_age_seconds=3600, dry_run=False)
    assert not staging.exists()
    assert lock_file.exists(), "Lockfile must be retained"

    # Verify a subsequent process/thread can acquire the retained lockfile
    with store._try_acquire_staging_lock(lock_file) as (acquired1, _):
        assert acquired1 is True, "First subsequent attempt must acquire retained lockfile"

        # While held, a concurrent process attempt fails
        with store._try_acquire_staging_lock(lock_file) as (acquired2, reason2):
            assert acquired2 is False, "Concurrent attempt while held must fail"
            assert reason2 is not None

    # After release, another attempt succeeds again
    with store._try_acquire_staging_lock(lock_file) as (acquired3, _):
        assert acquired3 is True, "Subsequent attempt after release must acquire successfully"


# =============================================================================
# Test 2C: Cleanup does not unlink or replace lockfile during concurrent coordination
# =============================================================================
def test_cleanup_does_not_unlink_or_replace_lockfile_during_concurrent_coordination(tmp_path: Path):
    adapter = InMemoryStorageAdapter()
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path)
    cid = "col_test_cleanup"
    snap_dir = tmp_path / cid / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)

    staging = snap_dir / "_staging_hydrate_snap001_concoord"
    staging.mkdir()
    set_dir_mtime(staging, past_seconds=7200)
    lock_file = snap_dir / f".{staging.name}.lock"
    lock_file.write_bytes(b"")

    cleaner1_holding = threading.Event()
    cleaner2_done = threading.Event()

    def cleaner_thread():
        # Cleaner 1 holds lock on the staging lockfile
        with store._try_acquire_staging_lock(lock_file) as (acquired, _):
            assert acquired is True
            cleaner1_holding.set()
            # Wait until cleaner 2 attempts its scan
            cleaner2_done.wait(timeout=5.0)
            # Delete staging directory
            shutil.rmtree(staging, ignore_errors=True)

    t = threading.Thread(target=cleaner_thread)
    t.start()
    assert cleaner1_holding.wait(timeout=5.0)

    try:
        # Concurrent cleaner 2 runs while Cleaner 1 holds the lock
        res2 = store.cleanup_abandoned_hydration_staging(cid, min_age_seconds=3600, dry_run=False)
        assert len(res2["deleted_paths"]) == 0
        skipped = [s for s in res2["skipped"] if s["path"] == str(staging)]
        assert len(skipped) == 1
        assert skipped[0]["reason"] == "locked_or_in_use"
    finally:
        cleaner2_done.set()
        t.join(timeout=5.0)

    # After Cleaner 1 finishes, staging is gone and lockfile is retained
    assert not staging.exists()
    assert lock_file.exists(), "Lockfile must remain persistent and never unlinked"

    # Subsequent cleanup sweep finds no candidates and does not touch or delete the lockfile
    res3 = store.cleanup_abandoned_hydration_staging(cid, min_age_seconds=3600, dry_run=False)
    assert res3["candidates_found"] == 0
    assert len(res3["deleted_paths"]) == 0
    assert lock_file.exists(), "Lockfile must remain intact after subsequent cleanup sweeps"


# =============================================================================
# Test 3: A live hydrator holding the ownership lock is skipped
# =============================================================================
def test_live_hydrator_holding_lock_is_skipped(tmp_path: Path):
    adapter = InMemoryStorageAdapter()
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path)
    cid = "col_test_cleanup"
    snap_dir = tmp_path / cid / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)

    staging = snap_dir / "_staging_hydrate_snap001_live1234"
    staging.mkdir()
    (staging / "in_progress.npy").write_bytes(b"downloading...")
    # Even if mtime is somehow in the past:
    set_dir_mtime(staging, past_seconds=7200)

    lock_file = snap_dir / f".{staging.name}.lock"

    # Acquire lock in thread to simulate active hydrator holding Level 1 lock
    lock_held_event = threading.Event()
    release_event = threading.Event()

    def live_hydrator():
        with store._acquire_process_lock(lock_file):
            lock_held_event.set()
            release_event.wait(timeout=5.0)

    t = threading.Thread(target=live_hydrator)
    t.start()
    assert lock_held_event.wait(timeout=5.0)

    try:
        res = store.cleanup_abandoned_hydration_staging(cid, min_age_seconds=3600, dry_run=False)

        assert res["candidates_found"] == 0
        assert len(res["deleted_paths"]) == 0
        skipped_entries = [s for s in res["skipped"] if s["path"] == str(staging)]
        assert len(skipped_entries) == 1
        assert skipped_entries[0]["reason"] == "locked_or_in_use"
        assert staging.is_dir(), "Live hydrator staging directory must NOT be deleted"
    finally:
        release_event.set()
        t.join(timeout=5.0)


# =============================================================================
# Test 4: Recent staging is preserved
# =============================================================================
def test_recent_staging_is_preserved(tmp_path: Path):
    adapter = InMemoryStorageAdapter()
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path)
    cid = "col_test_cleanup"
    snap_dir = tmp_path / cid / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)

    staging = snap_dir / "_staging_hydrate_snap001_fresh123"
    staging.mkdir()
    # Age is only 10 seconds (well below 3600s threshold)
    set_dir_mtime(staging, past_seconds=10)

    res = store.cleanup_abandoned_hydration_staging(cid, min_age_seconds=3600, dry_run=False)

    assert res["candidates_found"] == 0
    assert len(res["deleted_paths"]) == 0
    skipped_entries = [s for s in res["skipped"] if s["path"] == str(staging)]
    assert len(skipped_entries) == 1
    assert skipped_entries[0]["reason"] == "recent_staging_below_min_age"
    assert staging.is_dir()


# =============================================================================
# Test 5: Malformed staging names are ignored
# =============================================================================
def test_malformed_staging_names_ignored(tmp_path: Path):
    adapter = InMemoryStorageAdapter()
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path)
    cid = "col_test_cleanup"
    snap_dir = tmp_path / cid / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)

    malformed_dirs = [
        snap_dir / "_staging_hydrate_",
        snap_dir / "_staging_hydrate_snaponly",
        snap_dir / "_staging_hydrate_snap_001_invalid$suffix",
    ]

    for d in malformed_dirs:
        d.mkdir()
        set_dir_mtime(d, past_seconds=7200)

    res = store.cleanup_abandoned_hydration_staging(cid, min_age_seconds=3600, dry_run=False)

    assert res["candidates_found"] == 0
    assert len(res["deleted_paths"]) == 0
    for d in malformed_dirs:
        assert d.is_dir(), f"Malformed directory {d.name} must not be deleted"
        skipped_match = [s for s in res["skipped"] if s["path"] == str(d)]
        assert len(skipped_match) == 1
        assert skipped_match[0]["reason"] == "malformed_staging_name"


# =============================================================================
# Test 6: Completed snapshot directories are never touched
# =============================================================================
def test_completed_snapshot_directories_never_touched(tmp_path: Path):
    adapter = InMemoryStorageAdapter()
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path)
    cid = "col_test_cleanup"
    snap_dir = tmp_path / cid / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)

    completed = snap_dir / "snap_20261102_001"
    completed.mkdir()
    (completed / "manifest.json").write_text("{}", encoding="utf-8")
    for artifact in REQUIRED_BUNDLE_ARTIFACTS:
        (completed / artifact).write_bytes(b"data")
    set_dir_mtime(completed, past_seconds=86400 * 30)

    res = store.cleanup_abandoned_hydration_staging(cid, min_age_seconds=3600, dry_run=False)

    assert res["candidates_found"] == 0
    assert len(res["deleted_paths"]) == 0
    assert completed.is_dir(), "Completed snapshot directory must remain completely untouched"
    assert (completed / "manifest.json").is_file()
    for s in res["skipped"]:
        assert s["path"] != str(completed)


# =============================================================================
# Test 7: _corrupt_* quarantine directories are never touched
# =============================================================================
def test_corrupt_quarantine_directories_never_touched(tmp_path: Path):
    adapter = InMemoryStorageAdapter()
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path)
    cid = "col_test_cleanup"
    snap_dir = tmp_path / cid / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)

    corrupt = snap_dir / "_corrupt_snap001_deadc0de"
    corrupt.mkdir()
    (corrupt / "corrupt_data.bin").write_bytes(b"corrupt")
    set_dir_mtime(corrupt, past_seconds=86400 * 30)

    res = store.cleanup_abandoned_hydration_staging(cid, min_age_seconds=3600, dry_run=False)

    assert res["candidates_found"] == 0
    assert len(res["deleted_paths"]) == 0
    assert corrupt.is_dir(), "Quarantine directory must not be deleted by staging cleanup"
    assert (corrupt / "corrupt_data.bin").is_file()


# =============================================================================
# Test 8: Symlinks/reparse points are rejected where supported
# =============================================================================
def test_symlinks_reparse_points_rejected(tmp_path: Path):
    adapter = InMemoryStorageAdapter()
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path)
    cid = "col_test_cleanup"
    snap_dir = tmp_path / cid / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)

    target_dir = tmp_path / "external_target_dir"
    target_dir.mkdir()
    (target_dir / "precious.txt").write_text("critical data", encoding="utf-8")

    link_path = snap_dir / "_staging_hydrate_snap001_symlink1"
    try:
        link_path.symlink_to(target_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        # On Windows without developer mode/admin rights, symlink creation may require mock
        with patch("app.retrieval.snapshot_store._is_symlink_or_reparse_point", return_value=True):
            # Create normal dir to test that if identified as symlink/reparse, it gets skipped
            link_path.mkdir(exist_ok=True)
            set_dir_mtime(link_path, past_seconds=7200)
            res = store.cleanup_abandoned_hydration_staging(cid, min_age_seconds=3600, dry_run=False)
            assert res["candidates_found"] == 0
            assert len(res["deleted_paths"]) == 0
            skipped_entry = [s for s in res["skipped"] if s["path"] == str(link_path)]
            assert len(skipped_entry) == 1
            assert skipped_entry[0]["reason"] == "symlink_or_reparse_point"
            return

    set_dir_mtime(link_path, past_seconds=7200)
    res = store.cleanup_abandoned_hydration_staging(cid, min_age_seconds=3600, dry_run=False)

    assert res["candidates_found"] == 0
    assert len(res["deleted_paths"]) == 0
    assert (target_dir / "precious.txt").is_file(), "External symlink target must not be touched"
    skipped_entry = [s for s in res["skipped"] if s["path"] == str(link_path)]
    assert len(skipped_entry) == 1
    assert skipped_entry[0]["reason"] == "symlink_or_reparse_point"


# =============================================================================
# Test 9: Root confinement prevents traversal or outside-root deletion
# =============================================================================
def test_root_confinement_prevents_traversal(tmp_path: Path):
    adapter = InMemoryStorageAdapter()
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path)

    # 1. Invalid collection_id path traversal attempt
    res = store.cleanup_abandoned_hydration_staging(
        collection_id="../escape_collection",
        min_age_seconds=3600,
        dry_run=False,
    )
    assert len(res["errors"]) == 1
    assert len(res["deleted_paths"]) == 0

    # 2. Candidate that resolves outside snapshots_dir
    cid = "col_confinement"
    snap_dir = tmp_path / cid / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)

    staging = snap_dir / "_staging_hydrate_snap001_traversal1"
    staging.mkdir()
    set_dir_mtime(staging, past_seconds=7200)

    orig_resolve = Path.resolve

    def selective_resolve(path_obj):
        if path_obj.name == staging.name:
            return tmp_path / "outside" / "escaped" / path_obj.name
        return orig_resolve(path_obj)

    with patch.object(Path, "resolve", selective_resolve):
        res = store.cleanup_abandoned_hydration_staging(cid, min_age_seconds=3600, dry_run=False)
        assert len(res["deleted_paths"]) == 0
        skipped_entry = [s for s in res["skipped"] if s["path"] == str(staging)]
        assert len(skipped_entry) == 1
        assert skipped_entry[0]["reason"] == "path_traversal_rejected"


# =============================================================================
# Test 10: Cleanup respects max_entries
# =============================================================================
def test_cleanup_respects_max_entries(tmp_path: Path):
    adapter = InMemoryStorageAdapter()
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path)
    cid = "col_test_max_entries"
    snap_dir = tmp_path / cid / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)

    for i in range(5):
        staging = snap_dir / f"_staging_hydrate_snap{i}_abc{i}1234"
        staging.mkdir()
        set_dir_mtime(staging, past_seconds=7200)

    res = store.cleanup_abandoned_hydration_staging(
        cid,
        min_age_seconds=3600,
        dry_run=False,
        max_entries=2,
    )

    assert len(res["deleted_paths"]) == 2
    assert res["candidates_found"] == 2
    remaining_staging = [d for d in snap_dir.iterdir() if d.is_dir() and d.name.startswith("_staging_hydrate_")]
    assert len(remaining_staging) == 3, "Only max_entries (2) should be removed, leaving 3 intact"


# =============================================================================
# Test 11: Lock release occurs after hydration success and failure
# =============================================================================
def test_lock_release_after_hydration_success_and_failure(tmp_path: Path):
    adapter = InMemoryStorageAdapter()
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path)
    cid = "col_lock_lifecycle"

    # Case A: Success lifecycle
    seed_remote_bundle(adapter, cid, "snap_success_01")
    hydrated_path = store.hydrate_snapshot(cid)
    assert hydrated_path is not None
    assert hydrated_path.is_dir()

    # Verify no staging locks remain active or lingering
    snap_dir = tmp_path / cid / "snapshots"
    lock_files = list(snap_dir.glob("._staging_hydrate_*.lock"))
    for lf in lock_files:
        # Non-blocking lock attempt must succeed immediately
        with store._try_acquire_staging_lock(lf) as (acquired, _):
            assert acquired is True, f"Lingering lock held on {lf}"

    # Case B: Failure lifecycle
    seed_remote_bundle(adapter, cid, "snap_failure_01")
    # Corrupt manifest sha in active pointer to trigger SnapshotIntegrityError
    corrupt_ptr = ActiveSnapshotPointer(
        collection_id=cid,
        snapshot_id="snap_failure_01",
        manifest_checksum="0000000000000000000000000000000000000000000000000000000000000000",
        activated_at=datetime.now(timezone.utc),
        total_documents=1,
        total_chunks=1,
    )
    adapter.put_bytes(f"indexes/{cid}/active_snapshot.json", corrupt_ptr.model_dump_json(indent=2).encode("utf-8"))

    with pytest.raises(SnapshotIntegrityError):
        store.hydrate_snapshot(cid, force_refresh=True)

    # Verify all locks were released on failure
    lock_files = list(snap_dir.glob("._staging_hydrate_*.lock"))
    for lf in lock_files:
        with store._try_acquire_staging_lock(lf) as (acquired, _):
            assert acquired is True, f"Lingering lock held after failure on {lf}"


# =============================================================================
# Test 12: Concurrent cleanup and hydration cannot delete an active staging directory
# =============================================================================
def test_concurrent_cleanup_and_hydration_safety(tmp_path: Path):
    adapter = InMemoryStorageAdapter()
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path)
    cid = "col_concurrent_cleanup"
    snap_id = "snap_concurrent_01"
    seed_remote_bundle(adapter, cid, snap_id)

    download_started = threading.Event()
    cleanup_done = threading.Event()
    original_download_file = adapter.download_file

    def slow_download_file(key: str, dest_path: str | Path):
        # When downloading chunks.json inside staging, pause and signal cleanup to run
        res = original_download_file(key, dest_path)
        if "chunks.json" in key:
            download_started.set()
            # Wait for cleanup thread to finish its sweep before proceeding with hydration
            assert cleanup_done.wait(timeout=5.0), "Timed out waiting for cleanup sweep"
        return res

    adapter.download_file = slow_download_file

    cleanup_results = {}

    def run_cleanup():
        # Wait until hydration has entered staging download and holds staging lock
        assert download_started.wait(timeout=5.0)
        # Force min_age_seconds=0 to test pure lock-based mutual exclusion
        cleanup_results["res"] = store.cleanup_abandoned_hydration_staging(
            cid,
            min_age_seconds=0,
            dry_run=False,
        )
        cleanup_done.set()

    cleanup_thread = threading.Thread(target=run_cleanup)
    cleanup_thread.start()

    # Hydration runs on main thread
    hydrated_dir = store.hydrate_snapshot(cid)

    cleanup_thread.join(timeout=5.0)

    assert hydrated_dir is not None
    assert hydrated_dir.is_dir()
    assert store.is_cache_valid(hydrated_dir)

    # Verify cleanup safely skipped the active hydrator's staging directory
    res = cleanup_results.get("res")
    assert res is not None
    assert len(res["deleted_paths"]) == 0
    assert len(res["skipped"]) >= 1
    assert any(s["reason"] == "locked_or_in_use" for s in res["skipped"])


# =============================================================================
# Test 13: Cleanup errors are reported but do not break query or publication paths
# =============================================================================
def test_cleanup_errors_do_not_break_query_or_publication(tmp_path: Path):
    adapter = InMemoryStorageAdapter()
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path)
    cid = "col_test_err_resilience"
    snap_dir = tmp_path / cid / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)

    staging = snap_dir / "_staging_hydrate_snap001_err1234"
    staging.mkdir()
    set_dir_mtime(staging, past_seconds=7200)

    # Simulate unexpected filesystem permission error during rmtree
    with patch("shutil.rmtree", side_effect=PermissionError("Simulated WinError 5 Access Denied")):
        res = store.cleanup_abandoned_hydration_staging(cid, min_age_seconds=3600, dry_run=False)

        assert len(res["deleted_paths"]) == 0
        assert len(res["errors"]) == 1
        assert "Simulated WinError 5 Access Denied" in res["errors"][0]["error"]

    # Verify publication is completely unimpeded
    snap_id = "snap_published_01"
    pub_dir = tmp_path / cid / "snapshots" / snap_id
    pub_dir.mkdir(parents=True, exist_ok=True)
    for art in REQUIRED_BUNDLE_ARTIFACTS:
        (pub_dir / art).write_bytes(b"content")

    manifest = SnapshotManifest(
        format_version="1.0.0",
        collection_id=cid,
        snapshot_id=snap_id,
        created_at=datetime.now(timezone.utc),
        artifact_checksums={art: compute_file_sha256(pub_dir / art) for art in REQUIRED_BUNDLE_ARTIFACTS},
        total_chunks=1,
        total_documents=1,
        corpus_fingerprint="pub_fingerprint_001",
    )
    (pub_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    pointer = ActiveSnapshotPointer(
        collection_id=cid,
        snapshot_id=snap_id,
        manifest_checksum=compute_file_sha256(pub_dir / "manifest.json"),
        activated_at=datetime.now(timezone.utc),
        total_documents=1,
        total_chunks=1,
    )

    # Publication succeeds without hindrance
    store.publish_bundle(cid, snap_id, pub_dir, manifest, pointer)
    remote_ptr = store.get_remote_active_pointer(cid)
    assert remote_ptr is not None
    assert remote_ptr.snapshot_id == snap_id
