"""
Focused concurrency tests for Milestone 12 Wave 2 - Task 12.2B: Safe Concurrent Snapshot Hydration.

Validates:
1. Existing valid cache is reused without redundant re-download or deletion.
2. Concurrent hydrators for the same snapshot complete safely and reuse the validated cache.
3. Exactly one staging result is promoted; racing hydrators detect and reuse the validated directory.
4. Simultaneous rename OS collisions (FileExistsError) are handled gracefully without corruption.
5. Incomplete or failed staging downloads never corrupt an existing valid cache directory.
6. Corrupted/incomplete existing cache is safely quarantined and replaced when idle.
7. Locked existing cache fails closed safely without uncoordinated deletion.
8. Staging cleanup strictly purges only the current operation's own staging directory.
"""
import concurrent.futures
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import io
import tempfile
from typing import Dict, Any, Optional, Tuple, BinaryIO
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
    """Deterministic in-memory storage adapter for concurrent hydration testing."""

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


def _populate_remote_snapshot(
    adapter: InMemoryStorageAdapter,
    collection_id: str,
    snapshot_id: str,
) -> Tuple[ActiveSnapshotPointer, SnapshotManifest]:
    """Helper to seed remote storage with a valid snapshot bundle and active pointer."""
    bundle_prefix = f"indexes/{collection_id}/snapshots/{snapshot_id}"
    artifacts = {
        "chunks.json": json.dumps([{"chunk_id": "c1", "document_id": "d1", "text": "hello"}]).encode("utf-8"),
        "documents.json": json.dumps({"d1": {"document_id": "d1", "title": "Doc 1"}}).encode("utf-8"),
        "dense_index.npy": b"\x93NUMPY\x01\x00v\x00{'descr': '<f4', 'fortran_order': False, 'shape': (1, 4), }  \n" + b"\x00" * 16,
        "dense_index_meta.json": json.dumps({"model_name": "all-MiniLM-L6-v2", "num_chunks": 1}).encode("utf-8"),
    }
    checksums = {}
    for name, data in artifacts.items():
        adapter.put_bytes(f"{bundle_prefix}/{name}", data)
        checksums[name] = hashlib.sha256(data).hexdigest()

    manifest = SnapshotManifest(
        snapshot_id=snapshot_id,
        collection_id=collection_id,
        created_at=datetime.now(timezone.utc),
        format_version="1.0.0",
        embedding_model="all-MiniLM-L6-v2",
        embedding_dim=4,
        total_documents=1,
        total_chunks=1,
        document_ids=["d1"],
        document_versions={"d1": "sha_1"},
        chunk_ids=["c1"],
        corpus_fingerprint="fp1",
        artifact_checksums=checksums,
    )
    manifest_bytes = manifest.model_dump_json(indent=2).encode("utf-8")
    adapter.put_bytes(f"{bundle_prefix}/manifest.json", manifest_bytes)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()

    pointer = ActiveSnapshotPointer(
        snapshot_id=snapshot_id,
        collection_id=collection_id,
        activated_at=datetime.now(timezone.utc),
        manifest_checksum=manifest_sha,
        total_documents=1,
        total_chunks=1,
    )
    pointer_key = f"indexes/{collection_id}/active_snapshot.json"
    adapter.put_bytes(pointer_key, pointer.model_dump_json(indent=2).encode("utf-8"))

    return pointer, manifest


# =========================================================================
# 1. Existing Valid Cache Is Reused Without Deletion
# =========================================================================
def test_01_existing_valid_cache_is_reused_without_redownload_or_deletion(tmp_path):
    """Verifies that an already-hydrated valid snapshot cache is reused without deleting or overwriting it."""
    adapter = InMemoryStorageAdapter()
    col_id = "test_col_reuse"
    snap_id = "snap_reuse_1"
    _populate_remote_snapshot(adapter, col_id, snap_id)

    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path / "indexes")

    # Initial hydration
    cached_path = store.hydrate_snapshot(col_id)
    assert cached_path is not None
    assert cached_path.exists()

    # Place a canary file in the cached directory
    canary_path = cached_path / "canary.txt"
    canary_path.write_text("precious_reader_artifact", encoding="utf-8")

    # Clear recorded download counts
    adapter.download_call_counts.clear()

    # Second hydration call
    second_path = store.hydrate_snapshot(col_id)
    assert second_path == cached_path

    # Canary file must NOT be deleted! Proves shutil.rmtree was NOT called
    assert canary_path.is_file(), "Canary file was deleted! Valid cache was improperly replaced or deleted."
    assert canary_path.read_text(encoding="utf-8") == "precious_reader_artifact"

    # Redundant artifact downloads must be 0
    bundle_prefix = f"indexes/{col_id}/snapshots/{snap_id}"
    for artifact in REQUIRED_BUNDLE_ARTIFACTS:
        assert adapter.download_call_counts.get(f"{bundle_prefix}/{artifact}", 0) == 0


# =========================================================================
# 2. Concurrent Hydrators Deterministic Barrier Reuse
# =========================================================================
def test_02_concurrent_hydrators_deterministic_barrier_reuse_validated_result(tmp_path):
    """
    Two workers start hydration concurrently.
    Worker 1 completes promotion first and places a canary.
    Worker 2 finishes download and detects Worker 1's complete directory.
    Worker 2 reuses the directory without deleting Worker 1's directory or canary.
    """
    adapter = InMemoryStorageAdapter()
    col_id = "test_col_barrier"
    snap_id = "snap_barrier_1"
    _populate_remote_snapshot(adapter, col_id, snap_id)

    indexes_dir = tmp_path / "indexes"
    store1 = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=indexes_dir)
    store2 = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=indexes_dir)

    barrier = threading.Barrier(2)
    worker1_promoted = threading.Event()
    results = {}

    original_is_cache_valid = store2.is_cache_valid

    def worker_1():
        # Worker 1 proceeds normally and signals when done
        path1 = store1.hydrate_snapshot(col_id)
        # Place canary in the promoted cache
        (path1 / "worker1_canary.txt").write_text("worker1_was_here", encoding="utf-8")
        worker1_promoted.set()
        results["worker1"] = path1

    def worker_2():
        # Worker 2 waits for Worker 1 to finish promotion before Worker 2's pre-promotion check
        def delayed_check(cache_dir, expected_manifest_chk=None):
            worker1_promoted.wait(timeout=5.0)
            return original_is_cache_valid(cache_dir, expected_manifest_chk)

        store2.is_cache_valid = delayed_check
        path2 = store2.hydrate_snapshot(col_id)
        results["worker2"] = path2

    t1 = threading.Thread(target=worker_1)
    t2 = threading.Thread(target=worker_2)
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert results["worker1"] == results["worker2"]
    # Worker 1's canary must still exist!
    canary = results["worker2"] / "worker1_canary.txt"
    assert canary.is_file(), "Worker 2 deleted Worker 1's completed cache directory!"


# =========================================================================
# 3. Simultaneous Rename OS Collision (FileExistsError) Handled Gracefully
# =========================================================================
def test_03_simultaneous_rename_collision_handled_gracefully(tmp_path):
    """
    Simulates the exact OS-level race where two workers call rename at the same instant.
    The loser catches FileExistsError, verifies the winner's promoted cache, and reuses it safely.
    """
    adapter = InMemoryStorageAdapter()
    col_id = "test_col_collision"
    snap_id = "snap_collision_1"
    _populate_remote_snapshot(adapter, col_id, snap_id)

    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path / "indexes")

    # Hydrate normally once so clean cache exists
    valid_cache = store.hydrate_snapshot(col_id)
    assert valid_cache.is_dir()

    # Now create a second store instance simulating competing worker
    store_competing = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path / "indexes")

    # Monkeypatch rename on Path so that when staging_dir.rename(cache_dir) is called, it raises FileExistsError
    original_rename = Path.rename

    def mock_rename(self, target):
        if "_staging_hydrate_" in str(self):
            # Target already exists because competing worker created it
            raise FileExistsError(f"[WinError 183] Cannot create a file when that file already exists: {target}")
        return original_rename(self, target)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "rename", mock_rename)
        # Calling hydrate with force_refresh=True forces promotion path to trigger
        result = store_competing.hydrate_snapshot(col_id, force_refresh=True)

    assert result == valid_cache
    assert result.is_dir()
    assert (result / "manifest.json").is_file()


# =========================================================================
# 4. Incomplete/Failed Staging Download Never Corrupts Existing Valid Cache
# =========================================================================
def test_04_incomplete_staging_download_never_corrupts_existing_valid_cache(tmp_path):
    """Verifies that an aborted or failed staging download leaves any existing valid cache 100% intact."""
    adapter = InMemoryStorageAdapter()
    col_id = "test_col_abort"
    snap_id = "snap_abort_1"
    _populate_remote_snapshot(adapter, col_id, snap_id)

    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path / "indexes")
    valid_cache = store.hydrate_snapshot(col_id)

    # Place a canary in the valid cache
    canary = valid_cache / "valid_canary.txt"
    canary.write_text("pristine", encoding="utf-8")

    # Now tamper with remote storage so future downloads fail with checksum mismatch
    bundle_prefix = f"indexes/{col_id}/snapshots/{snap_id}"
    adapter.objects[f"{bundle_prefix}/dense_index.npy"] = b"corrupted_bytes"

    # Attempt second hydration with force_refresh=True
    with pytest.raises(SnapshotIntegrityError) as exc_info:
        store.hydrate_snapshot(col_id, force_refresh=True)

    assert "Checksum mismatch" in str(exc_info.value)

    # Valid cache must be completely uncorrupted!
    assert canary.is_file()
    assert canary.read_text(encoding="utf-8") == "pristine"
    for artifact in REQUIRED_BUNDLE_ARTIFACTS:
        assert (valid_cache / artifact).is_file()

    # Staging directories must be cleaned up
    snapshots_dir = tmp_path / "indexes" / col_id / "snapshots"
    staging_dirs = [d for d in snapshots_dir.iterdir() if d.name.startswith("_staging_hydrate_")]
    assert len(staging_dirs) == 0, f"Orphaned staging dirs left behind: {staging_dirs}"


# =========================================================================
# 5. Invalid Existing Cache Replaced When Idle
# =========================================================================
def test_05_invalid_existing_cache_replaced_when_idle(tmp_path):
    """If the target cache directory exists but is corrupt/incomplete, it is safely quarantined and replaced."""
    adapter = InMemoryStorageAdapter()
    col_id = "test_col_quarantine"
    snap_id = "snap_quarantine_1"
    _populate_remote_snapshot(adapter, col_id, snap_id)

    indexes_dir = tmp_path / "indexes"
    target_cache = indexes_dir / col_id / "snapshots" / snap_id
    target_cache.mkdir(parents=True, exist_ok=True)
    # Create corrupted manifest and missing files
    (target_cache / "manifest.json").write_text('{"bad": "data"}', encoding="utf-8")

    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=indexes_dir)

    # Hydration detects invalid cache, safely quarantines it, and promotes clean download
    result = store.hydrate_snapshot(col_id)
    assert result == target_cache
    assert store.is_cache_valid(target_cache)
    for artifact in REQUIRED_BUNDLE_ARTIFACTS:
        assert (target_cache / artifact).is_file()


# =========================================================================
# 6. Locked Existing Cache Fails Safely Closed Without Deletion
# =========================================================================
def test_06_locked_existing_cache_fails_safely_without_deletion(tmp_path):
    """If the target cache directory is invalid but locked/in-use, it fails closed safely without uncoordinated deletion."""
    adapter = InMemoryStorageAdapter()
    col_id = "test_col_locked"
    snap_id = "snap_locked_1"
    _populate_remote_snapshot(adapter, col_id, snap_id)

    indexes_dir = tmp_path / "indexes"
    target_cache = indexes_dir / col_id / "snapshots" / snap_id
    target_cache.mkdir(parents=True, exist_ok=True)
    # Invalid manifest
    corrupt_file = target_cache / "corrupt_data.bin"
    corrupt_file.write_bytes(b"bad")

    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=indexes_dir)

    # Monkeypatch os.rename on target_cache to raise PermissionError (simulating Windows file lock)
    real_os_rename = os.rename

    def mock_rename(src, dst):
        if str(src) == str(target_cache):
            raise PermissionError("[WinError 32] The process cannot access the file because it is being used by another process")
        return real_os_rename(src, dst)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "rename", mock_rename)
        with pytest.raises(SnapshotIntegrityError) as exc_info:
            store.hydrate_snapshot(col_id)

    assert "locked or in use" in str(exc_info.value)
    # Corrupt file was NOT deleted!
    assert corrupt_file.is_file(), "Locked directory was deleted despite being in use!"


# =========================================================================
# 7. Staging Cleanup Only Purges Current Operation's Own Staging Directory
# =========================================================================
def test_07_staging_cleanup_only_removes_own_staging_directory(tmp_path):
    """Verifies that an operation's finally block never touches staging directories belonging to other workers."""
    adapter = InMemoryStorageAdapter()
    col_id = "test_col_staging_scope"
    snap_id = "snap_staging_scope_1"
    _populate_remote_snapshot(adapter, col_id, snap_id)

    indexes_dir = tmp_path / "indexes"
    snapshots_dir = indexes_dir / col_id / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    # Create an orphaned staging directory belonging to another process
    other_staging = snapshots_dir / f"_staging_hydrate_{snap_id}_foreign99"
    other_staging.mkdir(parents=True, exist_ok=True)
    foreign_file = other_staging / "foreign.bin"
    foreign_file.write_bytes(b"do_not_touch")

    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=indexes_dir)
    res = store.hydrate_snapshot(col_id)
    assert res is not None

    # Foreign staging directory must still exist!
    assert other_staging.is_dir()
    assert foreign_file.is_file()
    assert foreign_file.read_bytes() == b"do_not_touch"


# =========================================================================
# 8. Multithreaded Concurrent Hydration Stress Test
# =========================================================================
def test_08_multithreaded_concurrent_hydration_stress_test(tmp_path):
    """Stress tests 8 threads across 2 collections simultaneously on a clean store."""
    adapter = InMemoryStorageAdapter()
    collections = ["stress_col_1", "stress_col_2"]
    for cid in collections:
        _populate_remote_snapshot(adapter, cid, f"snap_{cid}_001")

    indexes_dir = tmp_path / "indexes"

    def thread_hydrate(cid: str, tid: int):
        s = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=indexes_dir)
        path = s.hydrate_snapshot(cid)
        assert path is not None
        assert s.is_cache_valid(path)
        return (cid, tid, path.name)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = []
        for i in range(8):
            cid = collections[i % len(collections)]
            futures.append(executor.submit(thread_hydrate, cid, i))
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    assert len(results) == 8
    # No temporary staging directories remaining
    for cid in collections:
        snap_dir = indexes_dir / cid / "snapshots"
        staging_dirs = [d for d in snap_dir.iterdir() if d.name.startswith("_staging_hydrate_")]
        assert len(staging_dirs) == 0


# =========================================================================
# 9. Hydrator B Does Not Quarantine Valid Cache Promoted by Hydrator A
# =========================================================================
def test_09_hydrator_b_does_not_quarantine_valid_cache_promoted_by_hydrator_a(tmp_path):
    """
    Deterministic test of the concrete audit interleaving:
    1. Initially, an invalid cache exists on disk.
    2. Hydrator B starts and observes the target as invalid, downloads to staging, then pauses right before promotion lock.
    3. Hydrator A starts, quarantines the invalid cache, promotes its valid staging cache, and places a canary.
    4. Hydrator B resumes into the promotion section.
    5. Assert Hydrator B acquires lock, detects Hydrator A's valid cache, and reuses it WITHOUT quarantining or deleting it.
    6. Assert canary remains intact and each hydrator cleans only its own staging directory.
    """
    adapter = InMemoryStorageAdapter()
    col_id = "test_col_interleaving"
    snap_id = "snap_interleave_001"
    _populate_remote_snapshot(adapter, col_id, snap_id)

    indexes_dir = tmp_path / "indexes"
    snapshots_dir = indexes_dir / col_id / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    # 1. Create an invalid cache directory on disk
    target_cache = snapshots_dir / snap_id
    target_cache.mkdir(parents=True, exist_ok=True)
    (target_cache / "manifest.json").write_text('{"corrupted": true}', encoding="utf-8")

    store_a = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=indexes_dir)
    store_b = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=indexes_dir)

    b_ready_to_promote = threading.Event()
    a_promoted = threading.Event()
    results = {}

    original_acquire_lock_b = store_b._acquire_process_lock

    @contextmanager
    def intercepted_lock_b(lock_path, timeout=30.0):
        # Signal that B has downloaded and is about to acquire the promotion lock
        b_ready_to_promote.set()
        # Wait until A has completely finished promotion under the lock
        assert a_promoted.wait(timeout=5.0), "Timed out waiting for Hydrator A to finish promotion"
        with original_acquire_lock_b(lock_path, timeout=timeout):
            yield

    store_b._acquire_process_lock = intercepted_lock_b

    def run_hydrator_b():
        results["b"] = store_b.hydrate_snapshot(col_id)

    def run_hydrator_a():
        # Wait until B has observed the cache as invalid and is ready to enter promotion
        assert b_ready_to_promote.wait(timeout=5.0), "Timed out waiting for Hydrator B to get ready"
        # Now Hydrator A hydrates: should quarantine the corrupt cache, promote valid cache, and finish
        res_a = store_a.hydrate_snapshot(col_id)
        # Place a canary file in A's promoted directory
        canary_a = res_a / "canary_from_a.txt"
        canary_a.write_text("hydrator_a_promoted_this", encoding="utf-8")
        results["a"] = res_a
        # Signal B that A is completely done
        a_promoted.set()

    t_b = threading.Thread(target=run_hydrator_b)
    t_a = threading.Thread(target=run_hydrator_a)

    t_b.start()
    t_a.start()

    t_a.join(timeout=10.0)
    t_b.join(timeout=10.0)

    assert not t_a.is_alive(), "Hydrator A thread hung"
    assert not t_b.is_alive(), "Hydrator B thread hung"

    assert results["a"] == target_cache
    assert results["b"] == target_cache

    # CRITICAL ASSERTION: Hydrator A's canary must STILL exist!
    # Proves Hydrator B did NOT quarantine or delete Hydrator A's promoted directory
    canary = target_cache / "canary_from_a.txt"
    assert canary.is_file(), "Defect reproduced! Hydrator B quarantined/deleted Hydrator A's valid cache!"
    assert canary.read_text(encoding="utf-8") == "hydrator_a_promoted_this"

    # Integrity verification
    assert store_a.is_cache_valid(target_cache)

    # Assert no staging directories left behind by either worker
    remaining_staging = [d.name for d in snapshots_dir.iterdir() if d.name.startswith("_staging_hydrate_")]
    assert len(remaining_staging) == 0, f"Staging directories leaked: {remaining_staging}"

    # Assert no quarantine directories remain
    remaining_quarantine = [d.name for d in snapshots_dir.iterdir() if d.name.startswith("_corrupt_")]
    assert len(remaining_quarantine) == 0, f"Quarantine directories leaked: {remaining_quarantine}"


# =========================================================================
# 10. Concurrent Invalid Cache Handling (Both Detect Corrupt Simultaneously)
# =========================================================================
def test_10_concurrent_invalid_cache_handling_both_detect_corrupt(tmp_path):
    """
    Two workers start simultaneously when an invalid cache exists on disk.
    Exactly one quarantines the invalid cache; the other waits on the lock,
    detects the now-valid cache, and reuses it without re-quarantining.
    """
    adapter = InMemoryStorageAdapter()
    col_id = "test_col_concurrent_corrupt"
    snap_id = "snap_corrupt_race_001"
    _populate_remote_snapshot(adapter, col_id, snap_id)

    indexes_dir = tmp_path / "indexes"
    snapshots_dir = indexes_dir / col_id / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    # Place invalid cache
    target_cache = snapshots_dir / snap_id
    target_cache.mkdir(parents=True, exist_ok=True)
    (target_cache / "manifest.json").write_text("invalid json string", encoding="utf-8")

    results = []
    errors = []

    def run_worker(wid: int):
        try:
            s = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=indexes_dir)
            p = s.hydrate_snapshot(col_id)
            results.append(p)
        except Exception as e:
            errors.append(e)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futs = [executor.submit(run_worker, i) for i in range(2)]
        for f in concurrent.futures.as_completed(futs):
            f.result()

    assert len(errors) == 0, f"Errors during concurrent invalid cache handling: {errors}"
    assert len(results) == 2
    assert results[0] == target_cache
    assert results[1] == target_cache

    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=indexes_dir)
    assert store.is_cache_valid(target_cache)

    # Zero staging or quarantine directories remaining
    staging_dirs = [d.name for d in snapshots_dir.iterdir() if d.name.startswith("_staging_hydrate_")]
    assert len(staging_dirs) == 0
    quarantine_dirs = [d.name for d in snapshots_dir.iterdir() if d.name.startswith("_corrupt_")]
    assert len(quarantine_dirs) == 0


# =========================================================================
# 11. Competing Pointer Mirror Rejected If Manifest Checksum Mismatches
# =========================================================================
def test_11_competing_pointer_mirror_rejected_if_manifest_checksum_mismatches(tmp_path):
    """
    Verifies that _update_local_pointer_cache does NOT accept a competing writer's pointer
    if the snapshot_id matches but manifest_checksum mismatches.
    """
    col_id = "test_col_ptr_reject"
    indexes_dir = tmp_path / "indexes"
    store = DurableSnapshotStore(storage_adapter=InMemoryStorageAdapter(), indexes_dir=indexes_dir)

    cid_dir = indexes_dir / col_id
    cid_dir.mkdir(parents=True, exist_ok=True)
    local_ptr_path = cid_dir / "active_snapshot.json"

    # Pre-seed pointer with snapshot_id='snap_001' and checksum='checksum_A'
    competing_pointer = {
        "snapshot_id": "snap_001",
        "collection_id": col_id,
        "activated_at": datetime.now(timezone.utc).isoformat(),
        "manifest_checksum": "checksum_A_which_is_different",
        "total_documents": 1,
        "total_chunks": 1,
    }
    local_ptr_path.write_text(json.dumps(competing_pointer), encoding="utf-8")

    # Target pointer has same snapshot_id='snap_001' but checksum='checksum_B'
    target_pointer = ActiveSnapshotPointer(
        snapshot_id="snap_001",
        collection_id=col_id,
        activated_at=datetime.now(timezone.utc),
        manifest_checksum="checksum_B_expected",
        total_documents=1,
        total_chunks=1,
    )

    # Monkeypatch os.replace so that all replacement attempts raise PermissionError (simulating continuous lock collision)
    def mock_replace(src, dst):
        raise PermissionError("[WinError 5] Access is denied")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "replace", mock_replace)
        with pytest.raises(PermissionError):
            # Because checksum_A != checksum_B, the competing pointer is NOT accepted and replace_err is raised!
            store._update_local_pointer_cache(col_id, target_pointer)

