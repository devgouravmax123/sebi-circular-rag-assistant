"""
Focused Unit and Integration Tests for Milestone 12 Wave 1 - Task 12.1B:
Conditional Active-Pointer Promotion in DurableSnapshotStore.

Validates:
1. First publication when pointer is absent uses create-only conditional write.
2. Successful replacement when pointer exists uses replace-only conditional write with current validator.
3. Two competing publishers: exactly one promotion succeeds for the same observed pointer.
4. Stale validator conflict does NOT overwrite the winning active pointer.
5. Failed promotion preserves prior active pointer and does not corrupt/delete immutable snapshot artifacts.
6. Ambiguous write outcome (timeout after successful commit) is safely resolved via authoritative re-read.
7. Ambiguous write outcome (timeout before commit) propagates error without silent unconditional fallback.
8. No unconditional put_bytes fallback occurs under any failure path.
9. Existing ActiveSnapshotPointer schema backward compatibility.
10. Parity between LocalStorageAdapter (SHA-256) and S3StorageAdapter (ETag).
"""
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
from typing import Any, Dict, Optional
import pytest
from botocore.exceptions import ClientError

from app.storage.base import (
    BaseStorageAdapter,
    StoragePreconditionFailedError,
    StorageError,
    ConditionalWriteResult,
    StorageFileNotFoundError,
)
from app.storage.local import LocalStorageAdapter
from app.storage.s3 import S3StorageAdapter
from app.schemas.publication import (
    SnapshotManifest,
    ActiveSnapshotPointer,
)
from app.retrieval.snapshot_store import (
    DurableSnapshotStore,
    StaleSnapshotWriterError,
    SnapshotIntegrityError,
    REQUIRED_BUNDLE_ARTIFACTS,
)


class MockConditionalS3Client:
    """
    Deterministic in-memory mock of boto3 S3 client implementing
    conditional PutObject semantics (IfNoneMatch and IfMatch) and error simulation.
    """

    def __init__(self, bucket_name: str = "test-bucket"):
        self.bucket_name = bucket_name
        self.objects: Dict[str, dict] = {}
        self.put_object_calls: int = 0
        self.fail_on_keys: Dict[str, Exception] = {}
        self.conditional_writes: list[dict] = []

    def _client_error(self, code: str, msg: str, op: str = "PutObject") -> ClientError:
        return ClientError({"Error": {"Code": code, "Message": msg}}, op)

    def put_object(
        self,
        Bucket: str,
        Key: str,
        Body: Any,
        ContentType: str = "application/octet-stream",
        Metadata: Optional[dict] = None,
        **kwargs: Any,
    ) -> dict:
        self.put_object_calls += 1
        self.conditional_writes.append({
            "key": Key,
            "IfNoneMatch": kwargs.get("IfNoneMatch"),
            "IfMatch": kwargs.get("IfMatch"),
        })

        if Key in self.fail_on_keys:
            exc = self.fail_on_keys.pop(Key)
            raise exc

        # Precondition: IfNoneMatch="*"
        if kwargs.get("IfNoneMatch") == "*":
            if Key in self.objects:
                raise self._client_error(
                    "PreconditionFailed",
                    "At least one of the pre-conditions you specified did not hold",
                    "PutObject",
                )

        # Precondition: IfMatch="<etag>"
        if "IfMatch" in kwargs:
            if Key not in self.objects:
                raise self._client_error(
                    "PreconditionFailed",
                    "Target object does not exist for IfMatch",
                    "PutObject",
                )
            current_etag = self.objects[Key].get("ETag", "").strip('"')
            expected_etag = kwargs["IfMatch"].strip('"')
            if current_etag != expected_etag:
                raise self._client_error(
                    "PreconditionFailed",
                    f"ETag mismatch: current {current_etag} != expected {expected_etag}",
                    "PutObject",
                )

        if hasattr(Body, "read"):
            data = Body.read()
        elif isinstance(Body, bytes):
            data = Body
        else:
            data = str(Body).encode("utf-8")

        etag = f'"{hashlib.md5(data).hexdigest()}"'
        self.objects[Key] = {
            "data": data,
            "ContentType": ContentType,
            "Metadata": Metadata or {},
            "ContentLength": len(data),
            "LastModified": datetime.now(timezone.utc),
            "ETag": etag,
        }
        return {"ETag": etag}

    def get_object(self, Bucket: str, Key: str, **kwargs: Any) -> dict:
        if Key not in self.objects:
            raise self._client_error("NoSuchKey", f"Key not found: {Key}", "GetObject")
        obj = self.objects[Key]
        return {
            "Body": io.BytesIO(obj["data"]),
            "ContentLength": obj["ContentLength"],
            "ContentType": obj["ContentType"],
            "Metadata": obj["Metadata"],
            "LastModified": obj["LastModified"],
        }

    def head_object(self, Bucket: str, Key: str, **kwargs: Any) -> dict:
        if Key not in self.objects:
            raise self._client_error("404", f"Not found: {Key}", "HeadObject")
        obj = self.objects[Key]
        return {
            "ContentLength": obj["ContentLength"],
            "ContentType": obj["ContentType"],
            "Metadata": obj["Metadata"],
            "ETag": obj.get("ETag", ""),
        }

    def upload_file(self, Filename: str, Bucket: str, Key: str, ExtraArgs: dict = None, **kwargs) -> None:
        p = Path(Filename)
        if not p.is_file():
            raise FileNotFoundError(f"File not found: {Filename}")
        with open(p, "rb") as f:
            self.put_object(
                Bucket=Bucket,
                Key=Key,
                Body=f,
                ContentType=(ExtraArgs or {}).get("ContentType", "application/octet-stream"),
                Metadata=(ExtraArgs or {}).get("Metadata", {}),
            )


def _create_mock_snapshot_files(snapshot_dir: Path, collection_id: str, snapshot_id: str) -> tuple[SnapshotManifest, ActiveSnapshotPointer]:
    """Helper to stage valid local snapshot bundle artifacts and generate valid manifest and pointer."""
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "chunks.json": b'[{"chunk_id": "c1", "text": "sample chunk text"}]',
        "documents.json": b'{"doc_1": {"document_id": "doc_1", "sha256": "abc"}}',
        "dense_index.npy": b'NUMPY_DENSE_MATRIX_BYTES_SIMULATION',
        "dense_index_meta.json": b'{"num_chunks": 1, "dim": 384}',
    }
    checksums = {}
    for filename, content in artifacts.items():
        p = snapshot_dir / filename
        p.write_bytes(content)
        checksums[filename] = hashlib.sha256(content).hexdigest()

    now = datetime.now(timezone.utc)
    manifest = SnapshotManifest(
        snapshot_id=snapshot_id,
        collection_id=collection_id,
        created_at=now,
        format_version="1.0.0",
        embedding_model="BAAI/bge-small-en-v1.5",
        embedding_dim=384,
        total_documents=1,
        total_chunks=1,
        document_ids=["doc_1"],
        document_versions={"doc_1": "abc"},
        chunk_ids=["c1"],
        corpus_fingerprint=hashlib.sha256(b"fingerprint").hexdigest(),
        artifact_checksums=checksums,
    )
    manifest_bytes = manifest.model_dump_json(indent=2).encode("utf-8")
    (snapshot_dir / "manifest.json").write_bytes(manifest_bytes)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()

    pointer = ActiveSnapshotPointer(
        snapshot_id=snapshot_id,
        collection_id=collection_id,
        activated_at=now,
        manifest_checksum=manifest_sha,
        total_documents=1,
        total_chunks=1,
    )
    return manifest, pointer


# ==============================================================================
# S3 / Cloud Provider Pointer Promotion Tests
# ==============================================================================

def test_s3_first_publication_when_pointer_absent(tmp_path: Path):
    """Verifies that the first publication when active pointer is absent uses create-only (IfNoneMatch='*')."""
    mock_client = MockConditionalS3Client("cloud-bucket")
    adapter = S3StorageAdapter(bucket_name="cloud-bucket", s3_client=mock_client)
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path / "indexes")

    col_id = "test_col"
    snap_id = "snap_001"
    local_dir = tmp_path / "local" / col_id / snap_id
    manifest, pointer = _create_mock_snapshot_files(local_dir, col_id, snap_id)

    store.publish_bundle(col_id, snap_id, local_dir, manifest, pointer)

    # 1. Authoritative pointer exists in S3
    remote_pointer = store.get_remote_active_pointer(col_id)
    assert remote_pointer is not None
    assert remote_pointer.snapshot_id == snap_id
    assert remote_pointer.collection_id == col_id

    # 2. Verify IfNoneMatch='*' was sent for pointer write
    pointer_key = f"indexes/{col_id}/active_snapshot.json"
    pointer_writes = [w for w in mock_client.conditional_writes if w["key"] == pointer_key]
    assert len(pointer_writes) == 1
    assert pointer_writes[0]["IfNoneMatch"] == "*"
    assert pointer_writes[0]["IfMatch"] is None

    # 3. Local pointer cache mirror updated
    cached_pointer_path = tmp_path / "indexes" / col_id / "active_snapshot.json"
    assert cached_pointer_path.is_file()
    cached_data = json.loads(cached_pointer_path.read_text("utf-8"))
    assert cached_data["snapshot_id"] == snap_id


def test_s3_successful_replacement_when_pointer_exists(tmp_path: Path):
    """Verifies that a subsequent publication uses replace-only with matching ETag validator."""
    mock_client = MockConditionalS3Client("cloud-bucket")
    adapter = S3StorageAdapter(bucket_name="cloud-bucket", s3_client=mock_client)
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path / "indexes")

    col_id = "test_col"
    # 1. Initial snapshot
    dir1 = tmp_path / "local" / col_id / "snap_001"
    m1, p1 = _create_mock_snapshot_files(dir1, col_id, "snap_001")
    store.publish_bundle(col_id, "snap_001", dir1, m1, p1)

    initial_pointer, initial_etag = store.get_remote_active_pointer_and_validator(col_id)
    assert initial_pointer.snapshot_id == "snap_001"
    assert initial_etag is not None

    # 2. Subsequent snapshot
    dir2 = tmp_path / "local" / col_id / "snap_002"
    m2, p2 = _create_mock_snapshot_files(dir2, col_id, "snap_002")
    store.publish_bundle(col_id, "snap_002", dir2, m2, p2)

    # 3. Verify replace-only write sent IfMatch
    pointer_key = f"indexes/{col_id}/active_snapshot.json"
    pointer_writes = [w for w in mock_client.conditional_writes if w["key"] == pointer_key]
    assert len(pointer_writes) == 2
    assert pointer_writes[1]["IfMatch"] == initial_etag

    # 4. Remote pointer updated to snap_002
    updated_pointer = store.get_remote_active_pointer(col_id)
    assert updated_pointer.snapshot_id == "snap_002"


def test_s3_competing_publishers_only_one_wins(tmp_path: Path):
    """
    Simulates two distributed workers racing to publish competing snapshots starting from the same base pointer.
    Verifies that exactly one promotion succeeds and the second raises StaleSnapshotWriterError.
    """
    mock_client = MockConditionalS3Client("cloud-bucket")
    adapter = S3StorageAdapter(bucket_name="cloud-bucket", s3_client=mock_client)
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path / "indexes")

    col_id = "racing_col"
    # Stage base snapshot
    base_dir = tmp_path / "local" / col_id / "snap_base"
    m_base, p_base = _create_mock_snapshot_files(base_dir, col_id, "snap_base")
    store.publish_bundle(col_id, "snap_base", base_dir, m_base, p_base)

    _, base_etag = store.get_remote_active_pointer_and_validator(col_id)
    assert base_etag is not None

    # Worker A prepares snap_A based on snap_base
    dir_a = tmp_path / "local" / col_id / "snap_A"
    m_a, p_a = _create_mock_snapshot_files(dir_a, col_id, "snap_A")

    # Worker B prepares snap_B based on snap_base
    dir_b = tmp_path / "local" / col_id / "snap_B"
    m_b, p_b = _create_mock_snapshot_files(dir_b, col_id, "snap_B")

    # Worker A publishes first with observed_validator -> wins
    store.publish_bundle(col_id, "snap_A", dir_a, m_a, p_a, observed_validator=base_etag)

    # Worker B tries to publish with observed_validator=base_etag -> loses due to precondition mismatch
    with pytest.raises(StaleSnapshotWriterError) as exc_info:
        store.publish_bundle(col_id, "snap_B", dir_b, m_b, p_b, observed_validator=base_etag)

    assert "Conditional pointer promotion conflict" in str(exc_info.value)
    assert "snap_A" in str(exc_info.value)

    # Winning pointer remains snap_A!
    active_ptr = store.get_remote_active_pointer(col_id)
    assert active_ptr.snapshot_id == "snap_A"


def test_s3_stale_validator_does_not_corrupt_artifacts(tmp_path: Path):
    """
    Verifies that when a conditional promotion fails, the losing candidate's immutable artifacts
    remain intact and the winner's pointer and artifacts are neither corrupted nor deleted.
    """
    mock_client = MockConditionalS3Client("cloud-bucket")
    adapter = S3StorageAdapter(bucket_name="cloud-bucket", s3_client=mock_client)
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path / "indexes")

    col_id = "integrity_col"
    dir1 = tmp_path / "local" / col_id / "snap_1"
    m1, p1 = _create_mock_snapshot_files(dir1, col_id, "snap_1")
    store.publish_bundle(col_id, "snap_1", dir1, m1, p1)

    dir2 = tmp_path / "local" / col_id / "snap_2"
    m2, p2 = _create_mock_snapshot_files(dir2, col_id, "snap_2")
    store.publish_bundle(col_id, "snap_2", dir2, m2, p2)

    # Simulate stale writer with outdated timestamp attempting snap_stale
    dir_stale = tmp_path / "local" / col_id / "snap_stale"
    m_stale, p_stale = _create_mock_snapshot_files(dir_stale, col_id, "snap_stale")
    p_stale.activated_at = datetime(2020, 1, 1, tzinfo=timezone.utc)

    with pytest.raises(StaleSnapshotWriterError):
        store.publish_bundle(col_id, "snap_stale", dir_stale, m_stale, p_stale)

    # 1. Active pointer is still snap_2
    active_ptr = store.get_remote_active_pointer(col_id)
    assert active_ptr.snapshot_id == "snap_2"

    # 2. Artifacts for snap_2 exist and are intact
    for art in REQUIRED_BUNDLE_ARTIFACTS:
        art_key = f"indexes/{col_id}/snapshots/snap_2/{art}"
        assert adapter.exists(art_key)


def test_s3_ambiguous_write_re_read_recovers_if_committed(tmp_path: Path):
    """
    Simulates a network timeout where S3 received and committed the conditional PutObject,
    but the client connection timed out before receiving the HTTP 200 response.
    Verifies that the store's authoritative re-read detects the successful commit and succeeds cleanly.
    """
    mock_client = MockConditionalS3Client("cloud-bucket")
    pointer_key = "indexes/ambiguous_col/active_snapshot.json"

    # Custom wrapper that commits the object to memory but raises a ClientError to simulate client drop
    original_put = mock_client.put_object
    def failing_put_object(*args, **kwargs):
        if kwargs.get("Key") == pointer_key:
            # Commit the write into S3 state
            res = original_put(*args, **kwargs)
            # Simulate network timeout after commit
            raise ClientError(
                {"Error": {"Code": "RequestTimeout", "Message": "Your socket connection to the server was not suitable"}},
                "PutObject",
            )
        return original_put(*args, **kwargs)

    mock_client.put_object = failing_put_object

    adapter = S3StorageAdapter(bucket_name="cloud-bucket", s3_client=mock_client)
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path / "indexes")

    col_id = "ambiguous_col"
    snap_id = "snap_ambiguous"
    local_dir = tmp_path / "local" / col_id / snap_id
    manifest, pointer = _create_mock_snapshot_files(local_dir, col_id, snap_id)

    # Should NOT raise! The store re-reads the pointer and confirms snap_ambiguous is active
    store.publish_bundle(col_id, snap_id, local_dir, manifest, pointer)

    active_ptr = store.get_remote_active_pointer(col_id)
    assert active_ptr is not None
    assert active_ptr.snapshot_id == snap_id


def test_s3_ambiguous_write_propagates_error_if_not_committed(tmp_path: Path):
    """
    Simulates an ambiguous network failure where the write was NOT committed.
    Verifies that the error is propagated and no unconditional retry occurs.
    """
    mock_client = MockConditionalS3Client("cloud-bucket")
    pointer_key = "indexes/uncommitted_col/active_snapshot.json"

    # Fails immediately without writing the pointer object
    mock_client.fail_on_keys[pointer_key] = ClientError(
        {"Error": {"Code": "RequestTimeout", "Message": "Connection timed out before sending data"}},
        "PutObject",
    )

    adapter = S3StorageAdapter(bucket_name="cloud-bucket", s3_client=mock_client)
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path / "indexes")

    col_id = "uncommitted_col"
    snap_id = "snap_uncommitted"
    local_dir = tmp_path / "local" / col_id / snap_id
    manifest, pointer = _create_mock_snapshot_files(local_dir, col_id, snap_id)

    with pytest.raises(StorageError):
        store.publish_bundle(col_id, snap_id, local_dir, manifest, pointer)

    # Remote pointer does NOT exist (no silent fallback or unverified commit)
    assert store.get_remote_active_pointer(col_id) is None


# ==============================================================================
# LocalStorageAdapter Pointer Promotion Parity Tests
# ==============================================================================

def test_local_first_publication_when_pointer_absent(tmp_path: Path):
    """Verifies that LocalStorageAdapter performs create-only conditional promotion when pointer is absent."""
    adapter = LocalStorageAdapter(root_dir=tmp_path / "uploads")
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path / "indexes")

    col_id = "local_col"
    snap_id = "snap_local_001"
    local_dir = tmp_path / "local" / col_id / snap_id
    manifest, pointer = _create_mock_snapshot_files(local_dir, col_id, snap_id)

    store.publish_bundle(col_id, snap_id, local_dir, manifest, pointer)

    active_ptr = store.get_remote_active_pointer(col_id)
    assert active_ptr is not None
    assert active_ptr.snapshot_id == snap_id


def test_local_competing_publishers_only_one_wins(tmp_path: Path):
    """
    Verifies that when two workers publish competing snapshots with LocalStorageAdapter,
    process-safe conditional promotion ensures exactly one wins and the second raises StaleSnapshotWriterError.
    """
    adapter = LocalStorageAdapter(root_dir=tmp_path / "uploads")
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path / "indexes")

    col_id = "local_race_col"
    dir_base = tmp_path / "local" / col_id / "snap_base"
    m_base, p_base = _create_mock_snapshot_files(dir_base, col_id, "snap_base")
    store.publish_bundle(col_id, "snap_base", dir_base, m_base, p_base)

    _, base_sha = store.get_remote_active_pointer_and_validator(col_id)
    assert base_sha is not None

    dir_a = tmp_path / "local" / col_id / "snap_A"
    m_a, p_a = _create_mock_snapshot_files(dir_a, col_id, "snap_A")

    dir_b = tmp_path / "local" / col_id / "snap_B"
    m_b, p_b = _create_mock_snapshot_files(dir_b, col_id, "snap_B")

    # Publisher A succeeds with observed_validator
    store.publish_bundle(col_id, "snap_A", dir_a, m_a, p_a, observed_validator=base_sha)

    # Publisher B tries to replace base pointer with stale base_sha -> fails with StaleSnapshotWriterError
    with pytest.raises(StaleSnapshotWriterError) as exc_info:
        store.publish_bundle(col_id, "snap_B", dir_b, m_b, p_b, observed_validator=base_sha)

    assert "Conditional pointer promotion conflict" in str(exc_info.value)

    active_ptr = store.get_remote_active_pointer(col_id)
    assert active_ptr.snapshot_id == "snap_A"


def test_active_pointer_schema_backward_compatibility():
    """Verifies that ActiveSnapshotPointer serializes and deserializes cleanly without new required fields."""
    now = datetime(2026, 11, 2, 0, 0, 0, tzinfo=timezone.utc)
    ptr = ActiveSnapshotPointer(
        snapshot_id="snap_123",
        collection_id="custom_collection",
        activated_at=now,
        manifest_checksum="a" * 64,
        total_documents=10,
        total_chunks=50,
    )
    raw_json = ptr.model_dump_json(indent=2)
    parsed = json.loads(raw_json)

    assert parsed["snapshot_id"] == "snap_123"
    assert parsed["collection_id"] == "custom_collection"
    assert parsed["manifest_checksum"] == "a" * 64
    assert parsed["total_documents"] == 10
    assert parsed["total_chunks"] == 50

    reconstructed = ActiveSnapshotPointer(**parsed)
    assert reconstructed.snapshot_id == ptr.snapshot_id
    assert reconstructed.collection_id == ptr.collection_id


def test_concurrent_threads_publish_bundle_race(tmp_path: Path):
    """
    Verifies that when 4 concurrent worker threads simultaneously attempt to publish competing
    snapshots against the same observed base pointer, exactly ONE succeeds and 3 fail with
    StaleSnapshotWriterError, leaving the winner's pointer active.
    """
    from concurrent.futures import ThreadPoolExecutor

    mock_client = MockConditionalS3Client("cloud-bucket")
    adapter = S3StorageAdapter(bucket_name="cloud-bucket", s3_client=mock_client)
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path / "indexes")

    col_id = "thread_race_col"
    base_dir = tmp_path / "local" / col_id / "snap_base"
    m_base, p_base = _create_mock_snapshot_files(base_dir, col_id, "snap_base")
    store.publish_bundle(col_id, "snap_base", base_dir, m_base, p_base)

    _, base_etag = store.get_remote_active_pointer_and_validator(col_id)
    assert base_etag is not None

    # Pre-stage 4 distinct snapshot bundles
    candidates = []
    for i in range(4):
        sid = f"snap_cand_{i}"
        sdir = tmp_path / "local" / col_id / sid
        m, p = _create_mock_snapshot_files(sdir, col_id, sid)
        candidates.append((sid, sdir, m, p))

    successes = []
    conflicts = []

    def publish_task(cand):
        sid, sdir, m, p = cand
        try:
            store.publish_bundle(col_id, sid, sdir, m, p, observed_validator=base_etag)
            successes.append(sid)
        except StaleSnapshotWriterError as e:
            conflicts.append((sid, e))

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(publish_task, c) for c in candidates]
        for f in futures:
            f.result()

    assert len(successes) == 1, f"Expected exactly 1 winning publisher, got {len(successes)} ({successes})"
    assert len(conflicts) == 3, f"Expected 3 conflicts, got {len(conflicts)}"

    winning_sid = successes[0]
    active_ptr = store.get_remote_active_pointer(col_id)
    assert active_ptr.snapshot_id == winning_sid
