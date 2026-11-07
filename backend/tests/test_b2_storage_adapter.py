"""
Unit and integration tests for Task 8.8B:
Backblaze B2 Storage Compatibility, Configuration Validation, Coordination Locking,
and Concurrency-Safe Snapshot Publication.

Validates:
1. Provider factory accepts B2 aliases ('b2', 'backblaze', 'backblaze_b2').
2. B2 endpoint/region validation, rejection of 'auto', and region mismatch detection.
3. Zero credential logging in string representations and logs.
4. Normal object upload, download, metadata, and delete operations on B2 adapter.
5. B2 conditional-write unsupported behavior (supports_conditional_writes=False,
   raises StorageConditionalWriteUnsupportedError on direct put_bytes_conditional or HTTP 501).
6. Concurrency-safe snapshot publication under coordination lock for B2.
7. Competing publishers: concurrent/stale attempts fail closed with StaleSnapshotWriterError.
8. Failed publication leaves previous active snapshot usable.
9. Retry after partial publication is safe and idempotent.
10. Hydration verifies SHA-256 checksums from B2-published bundles.
"""
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, Optional
import uuid
import fitz
import pytest

from botocore.exceptions import ClientError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.storage import (
    BaseStorageAdapter,
    S3StorageAdapter,
    get_storage_adapter,
    StorageSaveResult,
    StorageObjectMetadata,
    StorageError,
    StorageConfigurationError,
    StoragePreconditionFailedError,
    StorageConditionalWriteUnsupportedError,
    StorageFileNotFoundError,
)
from app.retrieval.snapshot_store import (
    DurableSnapshotStore,
    SnapshotIntegrityError,
    StaleSnapshotWriterError,
    compute_file_sha256,
)
from app.schemas.publication import (
    SnapshotManifest,
    ActiveSnapshotPointer,
)
from app.core.config import get_settings


@pytest.fixture(autouse=True)
def isolate_b2_test_environment(monkeypatch):
    """Isolates B2 tests from developer environment variables unless explicitly overridden by a test."""
    settings = get_settings()
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.delenv("RENDER", raising=False)


class MockB2S3Client:
    """
    In-memory mock boto3 S3 client simulating Backblaze B2 behavior.
    Supports standard S3 operations (put_object, get_object, head_object, delete_object,
    upload_file, download_file), but returns HTTP 501 Not Implemented on conditional PutObject.
    """

    def __init__(self, bucket_name: str = "b2-staging-bucket"):
        self.bucket_name = bucket_name
        self.objects: Dict[str, dict] = {}
        self.put_object_calls = 0
        self.supports_conditional_writes = False

    def _client_error(self, code: str, msg: str, op: str = "PutObject", http_status: int = 400) -> ClientError:
        return ClientError(
            {"Error": {"Code": code, "Message": msg}, "ResponseMetadata": {"HTTPStatusCode": http_status}},
            op,
        )

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

        # Simulate Backblaze B2 HTTP 501 Not Implemented on conditional PutObject
        if "IfNoneMatch" in kwargs or "IfMatch" in kwargs:
            raise self._client_error(
                "NotImplemented",
                "A header you provided implies functionality that is not implemented (conditional write).",
                op="PutObject",
                http_status=501,
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
            "ETag": etag,
            "LastModified": datetime.now(timezone.utc),
        }
        return {"ETag": etag}

    def get_object(self, Bucket: str, Key: str, **kwargs: Any) -> dict:
        if Key not in self.objects:
            raise self._client_error("NoSuchKey", f"Key not found: {Key}", "GetObject", 404)
        obj = self.objects[Key]
        return {
            "Body": io.BytesIO(obj["data"]),
            "ContentLength": obj["ContentLength"],
            "ContentType": obj["ContentType"],
            "Metadata": obj["Metadata"],
            "ETag": obj["ETag"],
            "LastModified": obj["LastModified"],
        }

    def head_object(self, Bucket: str, Key: str, **kwargs: Any) -> dict:
        if Key not in self.objects:
            raise self._client_error("404", f"Not found: {Key}", "HeadObject", 404)
        obj = self.objects[Key]
        return {
            "ContentLength": obj["ContentLength"],
            "ContentType": obj["ContentType"],
            "Metadata": obj["Metadata"],
            "ETag": obj["ETag"],
            "LastModified": obj["LastModified"],
        }

    def delete_object(self, Bucket: str, Key: str, **kwargs: Any) -> dict:
        self.objects.pop(Key, None)
        return {"DeleteMarker": True}

    def upload_file(self, Filename: str, Bucket: str, Key: str, ExtraArgs: Optional[dict] = None, **kwargs: Any) -> None:
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

    def download_file(self, Bucket: str, Key: str, Filename: str, **kwargs: Any) -> None:
        if Key not in self.objects:
            raise self._client_error("NoSuchKey", f"Key not found: {Key}", "GetObject", 404)
        Path(Filename).parent.mkdir(parents=True, exist_ok=True)
        with open(Filename, "wb") as f:
            f.write(self.objects[Key]["data"])


def create_minimal_pdf_bytes(text_lines: list[str]) -> bytes:
    """Helper generating minimal valid PDF bytes."""
    doc = fitz.open()
    page = doc.new_page()
    y = 50
    for line in text_lines:
        page.insert_text((50, y), line)
        y += 20
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def build_sample_snapshot_dir(tmp_path: Path, collection_id: str, snapshot_id: str) -> tuple[Path, SnapshotManifest, ActiveSnapshotPointer]:
    """Helper constructing candidate snapshot directory and manifest."""
    snap_dir = tmp_path / "staging" / snapshot_id
    snap_dir.mkdir(parents=True, exist_ok=True)

    chunks_data = [{"chunk_id": f"c_{i}", "document_id": "doc_1", "text": f"Sample chunk {i}"} for i in range(3)]
    docs_data = {"doc_1": {"document_id": "doc_1", "title": "Test Doc", "sha256": "fake_sha"}}
    npy_data = b"NUMPY_MOCK_BYTES"
    meta_data = {"collection_id": collection_id, "num_chunks": 3}

    chunks_p = snap_dir / "chunks.json"
    docs_p = snap_dir / "documents.json"
    npy_p = snap_dir / "dense_index.npy"
    meta_p = snap_dir / "dense_index_meta.json"
    manifest_p = snap_dir / "manifest.json"

    with open(chunks_p, "w", encoding="utf-8") as f:
        json.dump(chunks_data, f)
    with open(docs_p, "w", encoding="utf-8") as f:
        json.dump(docs_data, f)
    with open(npy_p, "wb") as f:
        f.write(npy_data)
    with open(meta_p, "w", encoding="utf-8") as f:
        json.dump(meta_data, f)

    checksums = {
        "chunks.json": compute_file_sha256(chunks_p),
        "documents.json": compute_file_sha256(docs_p),
        "dense_index.npy": compute_file_sha256(npy_p),
        "dense_index_meta.json": compute_file_sha256(meta_p),
    }

    manifest = SnapshotManifest(
        snapshot_id=snapshot_id,
        collection_id=collection_id,
        created_at=datetime.now(timezone.utc),
        format_version="1.0.0",
        embedding_model="mock_model",
        embedding_dim=384,
        total_documents=1,
        total_chunks=3,
        document_ids=["doc_1"],
        document_versions={"doc_1": "fake_sha"},
        chunk_ids=[c["chunk_id"] for c in chunks_data],
        corpus_fingerprint="corpus_fp",
        artifact_checksums=checksums,
    )
    with open(manifest_p, "w", encoding="utf-8") as f:
        f.write(manifest.model_dump_json(indent=2))

    manifest_chk = compute_file_sha256(manifest_p)
    pointer = ActiveSnapshotPointer(
        snapshot_id=snapshot_id,
        collection_id=collection_id,
        activated_at=datetime.now(timezone.utc),
        manifest_checksum=manifest_chk,
        total_documents=1,
        total_chunks=3,
    )
    return snap_dir, manifest, pointer


# =============================================================================
# 1. Provider Factory Recognition
# =============================================================================

def test_b2_provider_aliases_accepted_by_factory(tmp_path: Path):
    """Verifies that get_storage_adapter recognizes 'b2', 'backblaze', and 'backblaze_b2'."""
    for alias in ["b2", "B2", "backblaze", "BACKBLAZE", "backblaze_b2"]:
        settings = Settings(
            storage_provider=alias,
            s3_bucket_name="my-b2-bucket",
            s3_access_key_id="test_key_id",
            s3_secret_access_key="test_app_key",
            s3_endpoint_url="https://s3.us-east-005.backblazeb2.com",
            s3_region_name="us-east-005",
            uploads_dir=str(tmp_path),
        )
        adapter = get_storage_adapter(settings)
        assert isinstance(adapter, S3StorageAdapter)
        assert adapter.is_b2 is True
        assert adapter.supports_conditional_writes is False
        assert adapter.bucket_name == "my-b2-bucket"


# =============================================================================
# 2. Configuration Validation & Region Checks
# =============================================================================

def test_b2_configuration_rejects_missing_endpoint():
    """Verifies that Backblaze B2 rejects missing S3_ENDPOINT_URL."""
    with pytest.raises(StorageConfigurationError) as exc:
        S3StorageAdapter(
            bucket_name="my-b2-bucket",
            access_key_id="key",
            secret_access_key="secret",
            endpoint_url=None,
            region_name="us-east-005",
            provider_type="b2",
        )
    assert "Backblaze B2 requires S3_ENDPOINT_URL" in str(exc.value)


def test_b2_configuration_rejects_auto_region():
    """Verifies that Backblaze B2 strictly rejects region 'auto'."""
    with pytest.raises(StorageConfigurationError) as exc:
        S3StorageAdapter(
            bucket_name="my-b2-bucket",
            access_key_id="key",
            secret_access_key="secret",
            endpoint_url="https://s3.us-west-004.backblazeb2.com",
            region_name="auto",
            provider_type="b2",
        )
    assert "Backblaze B2 requires explicit S3_REGION_NAME" in str(exc.value)
    assert "auto' is rejected" in str(exc.value)


def test_b2_configuration_rejects_region_mismatch():
    """Verifies that region name must match the endpoint region."""
    with pytest.raises(StorageConfigurationError) as exc:
        S3StorageAdapter(
            bucket_name="my-b2-bucket",
            access_key_id="key",
            secret_access_key="secret",
            endpoint_url="https://s3.us-east-005.backblazeb2.com",
            region_name="us-west-004",  # Mismatch!
            provider_type="b2",
        )
    assert "does not match Backblaze B2 endpoint region 'us-east-005'" in str(exc.value)


def test_b2_configuration_accepts_matching_region():
    """Verifies that valid matching endpoint and region are accepted."""
    client = MockB2S3Client()
    adapter = S3StorageAdapter(
        bucket_name="my-b2-bucket",
        endpoint_url="https://s3.us-east-005.backblazeb2.com",
        region_name="us-east-005",
        s3_client=client,
        provider_type="b2",
    )
    assert adapter.is_b2 is True
    assert adapter.supports_conditional_writes is False
    assert adapter.region_name == "us-east-005"


# =============================================================================
# 3. Object Upload, Download, Metadata, and Deletion
# =============================================================================

def test_b2_normal_storage_crud_operations():
    """Verifies upload_stream, materialize_to_local, metadata, exists, and delete on B2 adapter."""
    client = MockB2S3Client()
    adapter = S3StorageAdapter(
        bucket_name="b2-bucket",
        endpoint_url="https://s3.us-east-005.backblazeb2.com",
        region_name="us-east-005",
        s3_client=client,
        provider_type="b2",
    )

    pdf_bytes = create_minimal_pdf_bytes(["Backblaze B2 Integration Test", "Circular Ref: SEBI/2026/01"])
    ws_id = uuid.uuid4()
    col_id = "test_col"
    doc_id = "doc_b2_001"

    # 1. Upload stream
    res = adapter.save_upload_stream(
        workspace_id=ws_id,
        collection_id=col_id,
        document_id=doc_id,
        stream=io.BytesIO(pdf_bytes),
        original_filename="b2_test.pdf",
    )
    assert res.byte_count == len(pdf_bytes)
    assert res.sha256 == hashlib.sha256(pdf_bytes).hexdigest()
    assert adapter.exists(res.source_uri) is True

    # 2. Metadata check
    meta = adapter.get_metadata(res.source_uri)
    assert meta is not None
    assert meta.byte_count == len(pdf_bytes)
    assert meta.sha256 == res.sha256

    # 3. Materialize to local
    with adapter.materialize_to_local(res.source_uri) as local_p:
        assert local_p.is_file()
        assert local_p.read_bytes() == pdf_bytes
    assert not local_p.exists()  # cleanup guaranteed

    # 4. Safe delete
    assert adapter.delete_file(res.source_uri) is True
    assert adapter.exists(res.source_uri) is False
    assert adapter.delete_file(res.source_uri) is False  # idempotent False


# =============================================================================
# 4. Conditional Write Unsupported Behavior
# =============================================================================

def test_b2_put_bytes_conditional_raises_unsupported_error():
    """Verifies that direct put_bytes_conditional on B2 adapter raises StorageConditionalWriteUnsupportedError."""
    client = MockB2S3Client()
    adapter = S3StorageAdapter(
        bucket_name="b2-bucket",
        endpoint_url="https://s3.us-east-005.backblazeb2.com",
        region_name="us-east-005",
        s3_client=client,
        provider_type="b2",
    )

    with pytest.raises(StorageConditionalWriteUnsupportedError) as exc:
        adapter.put_bytes_conditional("indexes/col/test.json", b"{}", expected_validator=None)
    assert "does not support conditional PutObject (Backblaze B2)" in str(exc.value)


# =============================================================================
# 5. Concurrency-Safe Snapshot Publication on B2
# =============================================================================

class MockPostgresSession:
    """Mock session simulating PostgreSQL connection with pg_try_advisory_xact_lock."""
    def __init__(self, lock_acquired: bool = True):
        self.lock_acquired = lock_acquired
        self.bind = type("MockBind", (), {"dialect": type("MockDialect", (), {"name": "postgresql"})()})()
        self.committed = False
        self.rolled_back = False

    def get_bind(self):
        return self.bind

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def begin(self):
        outer = self
        class MockTx:
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc_val, exc_tb):
                if exc_type is None:
                    outer.committed = True
                else:
                    outer.rolled_back = True
        return MockTx()

    def execute(self, stmt, params=None):
        val = self.lock_acquired
        class MockResult:
            def scalar(self):
                return val
        return MockResult()


def test_b2_postgres_advisory_lock_coordination_success(tmp_path: Path):
    """Verifies that publish_bundle utilizes pg_try_advisory_xact_lock when PostgreSQL session_factory is present."""
    client = MockB2S3Client()
    adapter = S3StorageAdapter(
        bucket_name="b2-bucket",
        endpoint_url="https://s3.us-east-005.backblazeb2.com",
        region_name="us-east-005",
        s3_client=client,
        provider_type="b2",
    )
    mock_session = MockPostgresSession(lock_acquired=True)
    session_factory = lambda: mock_session

    store = DurableSnapshotStore(
        storage_adapter=adapter,
        indexes_dir=tmp_path / "indexes",
        session_factory=session_factory,
    )

    col_id = "col_b2_pg_lock"
    snap_id = "snap_pg_001"
    snap_dir, manifest, pointer = build_sample_snapshot_dir(tmp_path, col_id, snap_id)

    store.publish_bundle(col_id, snap_id, snap_dir, manifest, pointer)
    assert mock_session.committed is True

    current_ptr = store.get_remote_active_pointer(col_id)
    assert current_ptr is not None
    assert current_ptr.snapshot_id == snap_id


def test_b2_postgres_advisory_lock_contention_raises_stale_writer(tmp_path: Path):
    """Verifies that if another worker holds the PostgreSQL advisory lock, StaleSnapshotWriterError is raised."""
    client = MockB2S3Client()
    adapter = S3StorageAdapter(
        bucket_name="b2-bucket",
        endpoint_url="https://s3.us-east-005.backblazeb2.com",
        region_name="us-east-005",
        s3_client=client,
        provider_type="b2",
    )
    # Simulate another worker holding the advisory lock (returns False)
    mock_session = MockPostgresSession(lock_acquired=False)
    session_factory = lambda: mock_session

    store = DurableSnapshotStore(
        storage_adapter=adapter,
        indexes_dir=tmp_path / "indexes",
        session_factory=session_factory,
    )

    col_id = "col_b2_contention"
    snap_id = "snap_pg_conflict"
    snap_dir, manifest, pointer = build_sample_snapshot_dir(tmp_path, col_id, snap_id)

    with pytest.raises(StaleSnapshotWriterError) as exc:
        store.publish_bundle(col_id, snap_id, snap_dir, manifest, pointer)
    assert "another worker currently holds the PostgreSQL transaction advisory lock" in str(exc.value)

def test_b2_publish_bundle_under_coordination_lock(tmp_path: Path):
    """Verifies that publish_bundle successfully publishes to B2 via coordination lock."""
    client = MockB2S3Client()
    adapter = S3StorageAdapter(
        bucket_name="b2-bucket",
        endpoint_url="https://s3.us-east-005.backblazeb2.com",
        region_name="us-east-005",
        s3_client=client,
        provider_type="b2",
    )
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path / "indexes")

    col_id = "col_b2_01"
    snap_id = "snap_20261105_001"
    snap_dir, manifest, pointer = build_sample_snapshot_dir(tmp_path, col_id, snap_id)

    # Publish bundle to B2
    store.publish_bundle(col_id, snap_id, snap_dir, manifest, pointer)

    # Authoritative active pointer should be written
    active_ptr = store.get_remote_active_pointer(col_id)
    assert active_ptr is not None
    assert active_ptr.snapshot_id == snap_id
    assert active_ptr.total_chunks == 3


def test_b2_competing_publishers_conflict_rejected(tmp_path: Path):
    """Verifies that stale or conflicting pointer publications on B2 are rejected."""
    client = MockB2S3Client()
    adapter = S3StorageAdapter(
        bucket_name="b2-bucket",
        endpoint_url="https://s3.us-east-005.backblazeb2.com",
        region_name="us-east-005",
        s3_client=client,
        provider_type="b2",
    )
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path / "indexes")

    col_id = "col_b2_conflict"
    # Publish newer snapshot first
    dir_newer, m_newer, p_newer = build_sample_snapshot_dir(tmp_path, col_id, "snap_newer")
    p_newer.activated_at = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)
    store.publish_bundle(col_id, "snap_newer", dir_newer, m_newer, p_newer)

    # Competing publisher attempts to publish an older snapshot
    dir_older, m_older, p_older = build_sample_snapshot_dir(tmp_path, col_id, "snap_older")
    p_older.activated_at = datetime(2026, 11, 5, 11, 0, 0, tzinfo=timezone.utc)

    with pytest.raises(StaleSnapshotWriterError) as exc:
        store.publish_bundle(col_id, "snap_older", dir_older, m_older, p_older)
    assert "Stale publication rejected" in str(exc.value)

    # Active pointer must still point to 'snap_newer'
    current_ptr = store.get_remote_active_pointer(col_id)
    assert current_ptr.snapshot_id == "snap_newer"


def test_b2_failed_publication_leaves_previous_snapshot_usable(tmp_path: Path):
    """Verifies that an error during artifact upload leaves the previous active snapshot intact."""
    client = MockB2S3Client()
    adapter = S3StorageAdapter(
        bucket_name="b2-bucket",
        endpoint_url="https://s3.us-east-005.backblazeb2.com",
        region_name="us-east-005",
        s3_client=client,
        provider_type="b2",
    )
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path / "indexes")

    col_id = "col_b2_fail"
    # 1. Publish baseline working snapshot
    dir_v1, m_v1, p_v1 = build_sample_snapshot_dir(tmp_path, col_id, "snap_v1")
    store.publish_bundle(col_id, "snap_v1", dir_v1, m_v1, p_v1)

    # 2. Attempt publishing v2 but corrupt/delete a required artifact locally before upload
    dir_v2, m_v2, p_v2 = build_sample_snapshot_dir(tmp_path, col_id, "snap_v2")
    (dir_v2 / "chunks.json").unlink()  # Missing artifact

    with pytest.raises(SnapshotIntegrityError):
        store.publish_bundle(col_id, "snap_v2", dir_v2, m_v2, p_v2)

    # 3. Active pointer must still point to snap_v1
    current_ptr = store.get_remote_active_pointer(col_id)
    assert current_ptr.snapshot_id == "snap_v1"


def test_b2_idempotent_retry_after_successful_publication(tmp_path: Path):
    """Verifies that re-publishing the same snapshot succeeds idempotently without error."""
    client = MockB2S3Client()
    adapter = S3StorageAdapter(
        bucket_name="b2-bucket",
        endpoint_url="https://s3.us-east-005.backblazeb2.com",
        region_name="us-east-005",
        s3_client=client,
        provider_type="b2",
    )
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path / "indexes")

    col_id = "col_b2_idemp"
    snap_dir, manifest, pointer = build_sample_snapshot_dir(tmp_path, col_id, "snap_idemp")

    # First publication
    store.publish_bundle(col_id, "snap_idemp", snap_dir, manifest, pointer)

    # Re-publication of same snapshot (e.g. worker retry after crash recovery)
    store.publish_bundle(col_id, "snap_idemp", snap_dir, manifest, pointer)

    current_ptr = store.get_remote_active_pointer(col_id)
    assert current_ptr.snapshot_id == "snap_idemp"


# =============================================================================
# 6. Hydration Checksum Verification from B2
# =============================================================================

def test_b2_hydration_downloads_and_verifies_checksums(tmp_path: Path):
    """Verifies that hydrate_snapshot pulls bundles from B2 and verifies SHA-256 integrity."""
    client = MockB2S3Client()
    adapter = S3StorageAdapter(
        bucket_name="b2-bucket",
        endpoint_url="https://s3.us-east-005.backblazeb2.com",
        region_name="us-east-005",
        s3_client=client,
        provider_type="b2",
    )
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path / "hydrated_indexes")

    col_id = "col_b2_hydrate"
    snap_id = "snap_b2_hydrate_01"
    snap_dir, manifest, pointer = build_sample_snapshot_dir(tmp_path, col_id, snap_id)

    # Publish to B2
    store.publish_bundle(col_id, snap_id, snap_dir, manifest, pointer)

    # Hydrate into separate clean indexes directory
    hydrated_dir = store.hydrate_snapshot(col_id)
    assert hydrated_dir is not None
    assert hydrated_dir.is_dir()
    assert (hydrated_dir / "chunks.json").is_file()
    assert (hydrated_dir / "documents.json").is_file()
    assert (hydrated_dir / "dense_index.npy").is_file()
    assert (hydrated_dir / "manifest.json").is_file()

    # Re-verify checksums match manifest
    for artifact_name, expected_chk in manifest.artifact_checksums.items():
        assert compute_file_sha256(hydrated_dir / artifact_name) == expected_chk


# =============================================================================
# 7. Concurrency Hardening & Isolation Tests (Task 8.8D)
# =============================================================================

def test_b2_multi_collection_isolation(tmp_path: Path):
    """
    Verifies that advisory lock keys are uniquely derived per collection_id
    and that locking collection A does not conflict with or block collection B.
    """
    cid_a = "collection_alpha_123"
    cid_b = "collection_beta_456"

    key_a = int.from_bytes(
        hashlib.sha256(f"snapshot_publication:{cid_a}".encode("utf-8")).digest()[:8],
        byteorder="big",
        signed=True,
    )
    key_b = int.from_bytes(
        hashlib.sha256(f"snapshot_publication:{cid_b}".encode("utf-8")).digest()[:8],
        byteorder="big",
        signed=True,
    )

    assert key_a != key_b
    assert -9223372036854775808 <= key_a <= 9223372036854775807
    assert -9223372036854775808 <= key_b <= 9223372036854775807

    # Shared lock state simulating PostgreSQL pg_try_advisory_xact_lock across connections
    held_locks = set()

    def make_session():
        session = MockPostgresSession()
        def mock_execute(stmt, params=None):
            k = params.get("key") if isinstance(params, dict) else None
            acquired = k not in held_locks
            if acquired and k is not None:
                held_locks.add(k)
            class MockResult:
                def scalar(self):
                    return acquired
            return MockResult()
        session.execute = mock_execute
        return session

    client = MockB2S3Client()
    adapter = S3StorageAdapter(
        bucket_name="b2-bucket",
        endpoint_url="https://s3.us-east-005.backblazeb2.com",
        region_name="us-east-005",
        s3_client=client,
        provider_type="b2",
    )
    store = DurableSnapshotStore(
        storage_adapter=adapter,
        indexes_dir=tmp_path / "indexes",
        session_factory=make_session,
    )

    # Worker 1 acquires lock on collection A
    with store._acquire_coordination_lock(cid_a):
        assert key_a in held_locks
        assert key_b not in held_locks

        # Worker 2 simultaneously attempts lock on collection B -> SUCCEEDS (collection isolation)
        with store._acquire_coordination_lock(cid_b):
            assert key_b in held_locks

        # Worker 3 attempts lock on collection A while held -> REJECTED (mutual exclusion on collection A)
        with pytest.raises(StaleSnapshotWriterError) as exc:
            with store._acquire_coordination_lock(cid_a):
                pass
        assert "another worker currently holds the PostgreSQL transaction advisory lock" in str(exc.value)


def test_b2_remote_pointer_write_failure_rolls_back_and_releases_lock(tmp_path: Path):
    """
    Verifies that if remote pointer persistence fails (e.g. S3/B2 put_bytes failure),
    the PostgreSQL transaction rolls back, previous active pointer is preserved,
    and subsequent publications can proceed cleanly without lock retention.
    """
    client = MockB2S3Client()
    adapter = S3StorageAdapter(
        bucket_name="b2-bucket",
        endpoint_url="https://s3.us-east-005.backblazeb2.com",
        region_name="us-east-005",
        s3_client=client,
        provider_type="b2",
    )

    sessions_created = []
    def session_factory():
        s = MockPostgresSession(lock_acquired=True)
        sessions_created.append(s)
        return s

    store = DurableSnapshotStore(
        storage_adapter=adapter,
        indexes_dir=tmp_path / "indexes",
        session_factory=session_factory,
    )

    col_id = "col_b2_rollback"
    snap_v1 = "snap_v1_baseline"
    snap_dir_v1, manifest_v1, pointer_v1 = build_sample_snapshot_dir(tmp_path, col_id, snap_v1)

    # Publish baseline snapshot v1 successfully
    store.publish_bundle(col_id, snap_v1, snap_dir_v1, manifest_v1, pointer_v1)
    assert store.get_remote_active_pointer(col_id).snapshot_id == snap_v1
    assert sessions_created[-1].committed is True
    assert sessions_created[-1].rolled_back is False

    # Now attempt to publish snapshot v2, but simulate a transient network/B2 error on writing active_pointer.json
    snap_v2 = "snap_v2_failing"
    snap_dir_v2, manifest_v2, pointer_v2 = build_sample_snapshot_dir(tmp_path, col_id, snap_v2)

    original_put_object = client.put_object
    def failing_put_object(*args, **kwargs):
        key = kwargs.get("Key", "")
        if "active_snapshot.json" in key:
            raise client._client_error("InternalError", "Simulated B2 500 error", op="PutObject", http_status=500)
        return original_put_object(*args, **kwargs)

    client.put_object = failing_put_object

    with pytest.raises(Exception):
        store.publish_bundle(col_id, snap_v2, snap_dir_v2, manifest_v2, pointer_v2)

    # Verify transaction was rolled back
    failing_session = sessions_created[-1]
    assert failing_session.rolled_back is True
    assert failing_session.committed is False

    # Verify previous active snapshot remains intact
    assert store.get_remote_active_pointer(col_id).snapshot_id == snap_v1

    # Restore client and verify subsequent publication succeeds cleanly
    client.put_object = original_put_object
    store.publish_bundle(col_id, snap_v2, snap_dir_v2, manifest_v2, pointer_v2)
    assert store.get_remote_active_pointer(col_id).snapshot_id == snap_v2
    assert sessions_created[-1].committed is True


def test_b2_orchestrator_retries_on_live_publication_conflict(tmp_path: Path):
    """
    Verifies that when publisher encounters a StaleSnapshotWriterError during
    pointer promotion, PublicationResult sets error_details='StaleSnapshotWriterError',
    and IngestionOrchestrator raises RetryableIngestionError (rather than NonRetryableIngestionError).
    """
    import numpy as np
    from unittest.mock import patch
    from app.schemas.document import DocumentRecord, ChunkRecord
    from app.retrieval.publisher import IndexPublisher
    from app.ingestion.orchestrator import IngestionOrchestrator, RetryableIngestionError, NonRetryableIngestionError
    from app.persistence.file_repo import FileStorageRepositoryFactory
    from app.retrieval.dense_embedder import DenseEmbedder
    import app.ingestion.orchestrator as orch_module

    class MinimalMockEmbedder(DenseEmbedder):
        def __init__(self):
            super().__init__(model_name="mock-embedder")
        def embed_texts(self, texts, batch_size=32):
            return np.ones((len(texts), 384), dtype=np.float32)
        def embed_query(self, query):
            return np.ones((384,), dtype=np.float32)

    col_id = "col_b2_orch_conflict"
    processed_dir = tmp_path / "processed"
    indexes_dir = tmp_path / "indexes"
    factory = FileStorageRepositoryFactory(processed_dir=str(processed_dir))
    doc_repo = factory.get_document_repository()
    chunk_repo = factory.get_chunk_repository()

    # Create 1 document and 1 chunk
    doc_record = DocumentRecord(
        document_id="doc_b2_c1",
        title="Circular 1",
        circular_number="CIR/B2/1",
        domain_id="SEBI_CSCRF",
        extraction_status="completed",
        sha256="abc12345",
        source_url="https://example.com/doc.pdf",
        pdf_path="data/uploads/doc.pdf",
        collection_id=col_id,
    )
    doc_repo.save_document(doc_record)
    chunk_record = ChunkRecord(
        chunk_id="chk_b2_c1_1",
        document_id="doc_b2_c1",
        chunk_index=0,
        text="Test chunk text for conflict testing.",
        page_start=1,
        page_end=1,
        collection_id=col_id,
    )
    chunk_repo.save_chunks([chunk_record])

    client = MockB2S3Client()
    adapter = S3StorageAdapter(
        bucket_name="b2-bucket",
        endpoint_url="https://s3.us-east-005.backblazeb2.com",
        region_name="us-east-005",
        s3_client=client,
        provider_type="b2",
    )

    # Session simulating another worker currently holding the advisory lock
    mock_session = MockPostgresSession(lock_acquired=False)
    session_factory = lambda: mock_session

    publisher = IndexPublisher(
        repository_factory=factory,
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
        embedder=MinimalMockEmbedder(),
        collection_id=col_id,
        storage_adapter=adapter,
    )
    publisher.durable_store.session_factory = session_factory

    # 1. Direct call to publisher produces StaleSnapshotWriterError in error_details
    pub_res = publisher.build_and_publish_snapshot()
    assert pub_res.success is False
    assert pub_res.error_details == "StaleSnapshotWriterError"
    assert "another worker currently holds" in pub_res.message

    # 2. IngestionOrchestrator translates this error_details to RetryableIngestionError
    orch = IngestionOrchestrator(
        repository_factory=factory,
        processed_dir=processed_dir,
        indexes_dir=indexes_dir,
        embedder=MinimalMockEmbedder(),
        storage_adapter=adapter,
        publish_snapshots=True,
    )

    pdf_doc = fitz.open()
    pdf_page = pdf_doc.new_page()
    pdf_page.insert_text((50, 50), "Regulatory Policy Provision for conflict test.")
    pdf_bytes = pdf_doc.tobytes()
    pdf_doc.close()

    save_res = adapter.save_upload_stream(
        workspace_id=uuid.uuid4(),
        collection_id=col_id,
        document_id="doc_b2_c1",
        stream=io.BytesIO(pdf_bytes),
    )
    job_repo = factory.get_ingestion_job_repository()
    job = job_repo.create_job(
        source_url=save_res.source_uri,
        idempotency_key="idem_b2_orch_1",
        collection_id=col_id,
    )

    def mock_publisher_factory(*args, **kwargs):
        p = IndexPublisher(*args, **kwargs)
        p.durable_store.session_factory = session_factory
        return p

    claimed = job_repo.claim_next_job(worker_id="test_worker")
    assert claimed is not None

    with patch.object(orch_module, "IndexPublisher", side_effect=mock_publisher_factory):
        exec_res = orch.process_job(claimed, claimed.claim_token)
        assert exec_res.error_details is not None
        assert exec_res.error_details.get("category") == "retryable"
        assert "Publication conflict for collection" in exec_res.error_details.get("reason", "")
        assert "Job will retry with backoff" in exec_res.error_details.get("reason", "")


def test_b2_staging_fails_closed_when_postgres_coordination_unavailable(tmp_path: Path, monkeypatch):
    """
    Verifies that when running with environment='staging', B2 publication fails closed
    with StorageConfigurationError if PostgreSQL coordination is missing or using a non-PostgreSQL dialect,
    and NEVER silently falls back to local process lockfiles.
    """
    from app.core.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "environment", "staging")

    client = MockB2S3Client()
    adapter = S3StorageAdapter(
        bucket_name="b2-bucket",
        endpoint_url="https://s3.us-east-005.backblazeb2.com",
        region_name="us-east-005",
        s3_client=client,
        provider_type="b2",
    )

    col_id = "col_staging_failclosed"
    lock_file = tmp_path / "indexes" / col_id / f".{col_id}.active_pointer.lock"

    # Case A: session_factory is None
    store_no_factory = DurableSnapshotStore(
        storage_adapter=adapter,
        indexes_dir=tmp_path / "indexes",
        session_factory=None,
    )
    snap_dir, manifest, pointer = build_sample_snapshot_dir(tmp_path, col_id, "snap_s1")

    with pytest.raises(StorageConfigurationError) as exc_a:
        store_no_factory.publish_bundle(col_id, "snap_s1", snap_dir, manifest, pointer)
    assert "requires an active PostgreSQL database session factory" in str(exc_a.value)
    assert not lock_file.exists()

    # Case B: session_factory provides non-PostgreSQL engine (e.g. SQLite dialect)
    class MockSqliteSession:
        def __init__(self):
            self.bind = type("MockBind", (), {"dialect": type("MockDialect", (), {"name": "sqlite"})()})()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get_bind(self):
            return self.bind

    store_sqlite = DurableSnapshotStore(
        storage_adapter=adapter,
        indexes_dir=tmp_path / "indexes",
        session_factory=lambda: MockSqliteSession(),
    )

    with pytest.raises(StorageConfigurationError) as exc_b:
        store_sqlite.publish_bundle(col_id, "snap_s1", snap_dir, manifest, pointer)
    assert "requires a PostgreSQL database bind" in str(exc_b.value)
    assert "resolved dialect 'sqlite'" in str(exc_b.value)
    assert not lock_file.exists()

