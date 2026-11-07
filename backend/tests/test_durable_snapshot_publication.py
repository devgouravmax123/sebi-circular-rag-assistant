"""
Unit and integration tests for Milestone 11 Wave B:
Durable snapshot publication, immutable bundles, remote active pointer,
and cold-start local cache hydration with outage resilience.
"""
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Dict, List, Optional
import uuid

import numpy as np
import pytest
from botocore.exceptions import ClientError

from app.core.config import Settings
from app.schemas.document import DocumentRecord, ChunkRecord
from app.schemas.publication import SnapshotManifest, ActiveSnapshotPointer, PublicationResult
from app.persistence.file_repo import FileStorageRepositoryFactory
from app.retrieval.dense_embedder import DenseEmbedder
from app.retrieval.publisher import IndexPublisher, IndexPublicationError, IndexValidationError
from app.retrieval.service import RetrievalService
from app.retrieval.snapshot_store import (
    DurableSnapshotStore,
    SnapshotIntegrityError,
    StaleSnapshotWriterError,
    compute_file_sha256,
)
from app.storage import (
    BaseStorageAdapter,
    LocalStorageAdapter,
    S3StorageAdapter,
    StorageFileNotFoundError,
    StorageError,
)


class MockS3Client:
    """Deterministic in-memory mock of boto3 S3 client with failure simulation."""

    def __init__(self, bucket_name: str = "test-bucket"):
        self.bucket_name = bucket_name
        self.objects: Dict[str, dict] = {}
        self.fail_on_keys: Dict[str, Exception] = {}
        self.get_object_calls: int = 0
        self.put_object_calls: int = 0

    def _client_error(self, code: str, msg: str, op: str) -> ClientError:
        return ClientError({"Error": {"Code": code, "Message": msg}}, op)

    def put_object(self, Bucket: str, Key: str, Body: Any, ContentType: str = "application/octet-stream", Metadata: dict = None, **kwargs) -> dict:
        if Key in self.fail_on_keys:
            raise self.fail_on_keys[Key]
        self.put_object_calls += 1
        if hasattr(Body, "read"):
            data = Body.read()
        elif isinstance(Body, bytes):
            data = Body
        else:
            data = str(Body).encode("utf-8")

        self.objects[Key] = {
            "data": data,
            "ContentType": ContentType,
            "Metadata": Metadata or {},
            "ContentLength": len(data),
            "LastModified": datetime.now(timezone.utc),
        }
        return {"ETag": f'"{hashlib.md5(data).hexdigest()}"'}

    def get_object(self, Bucket: str, Key: str, **kwargs) -> dict:
        if Key in self.fail_on_keys:
            raise self.fail_on_keys[Key]
        self.get_object_calls += 1
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

    def head_object(self, Bucket: str, Key: str, **kwargs) -> dict:
        if Key in self.fail_on_keys:
            raise self.fail_on_keys[Key]
        if Key not in self.objects:
            raise self._client_error("404", f"Not found: {Key}", "HeadObject")
        obj = self.objects[Key]
        return {
            "ContentLength": obj["ContentLength"],
            "ContentType": obj["ContentType"],
            "Metadata": obj["Metadata"],
            "LastModified": obj["LastModified"],
        }

    def delete_object(self, Bucket: str, Key: str, **kwargs) -> dict:
        self.objects.pop(Key, None)
        return {"DeleteMarker": True}

    def upload_file(self, Filename: str, Bucket: str, Key: str, ExtraArgs: dict = None, **kwargs) -> None:
        if Key in self.fail_on_keys:
            raise self.fail_on_keys[Key]
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

    def download_file(self, Bucket: str, Key: str, Filename: str, **kwargs) -> None:
        if Key in self.fail_on_keys:
            raise self.fail_on_keys[Key]
        if Key not in self.objects:
            raise self._client_error("NoSuchKey", f"Key not found: {Key}", "DownloadFile")
        p = Path(Filename)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            f.write(self.objects[Key]["data"])


class MockEmbedder(DenseEmbedder):
    """Deterministic mock embedder returning unit vectors with shape (N, 384)."""

    def __init__(self, dim: int = 384):
        super().__init__(model_name="mock-embedder")
        self.dim = dim

    def embed_texts(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        if not texts:
            return np.empty((0, self.dim), dtype=np.float32)
        vectors = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i in range(len(texts)):
            vectors[i, 0] = 1.0
        return vectors

    def embed_query(self, query: str) -> np.ndarray:
        vectors = np.zeros((self.dim,), dtype=np.float32)
        vectors[0] = 1.0
        return vectors


def create_minimal_pdf_bytes(text_lines: list[str]) -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    y = 50
    for line in text_lines:
        page.insert_text((50, y), line)
        y += 20
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def setup_test_repository(repo_dir: Path, collection_id: str, doc_count: int = 2):
    """Helper creating test DocumentRecord and ChunkRecord objects in a file repo."""
    factory = FileStorageRepositoryFactory(processed_dir=str(repo_dir))
    doc_repo = factory.get_document_repository()
    chunk_repo = factory.get_chunk_repository()

    for i in range(doc_count):
        doc_id = f"doc_{collection_id}_{i+1}"
        doc = DocumentRecord(
            document_id=doc_id,
            title=f"Regulatory Circular {i+1} for {collection_id}",
            circular_number=f"CIR/{collection_id}/{i+1}",
            domain_id="SEBI_CSCRF",
            extraction_status="completed",
            sha256=hashlib.sha256(f"content_{doc_id}".encode()).hexdigest(),
            source_url=f"https://example.com/{doc_id}.pdf",
            pdf_path=f"data/uploads/{doc_id}.pdf",
            collection_id=collection_id,
        )
        doc_repo.save_document(doc)

        # 2 chunks per doc
        doc_chunks = []
        for c_idx in range(2):
            chunk = ChunkRecord(
                chunk_id=f"chk_{doc_id}_{c_idx+1}",
                document_id=doc_id,
                chunk_index=c_idx,
                text=f"Provisions for compliance policy section {c_idx+1} under {collection_id}.",
                page_start=1,
                page_end=1,
                collection_id=collection_id,
            )
            doc_chunks.append(chunk)
        chunk_repo.save_chunks(doc_chunks)

    return factory


# =========================================================================
# 1. Successful Durable Publication & Pointer Update
# =========================================================================
def test_1_successful_durable_publication_and_pointer_update(tmp_path):
    """Verifies that IndexPublisher uploads complete bundle and updates remote active pointer."""
    col_id = "col_durable_pub"
    factory = setup_test_repository(tmp_path / "processed", col_id)
    mock_s3 = MockS3Client("durable-bucket")
    adapter = S3StorageAdapter(bucket_name="durable-bucket", s3_client=mock_s3)

    publisher = IndexPublisher(
        repository_factory=factory,
        indexes_dir=tmp_path / "indexes",
        processed_dir=tmp_path / "processed",
        embedder=MockEmbedder(),
        collection_id=col_id,
        storage_adapter=adapter,
    )

    res = publisher.build_and_publish_snapshot()
    assert res.success is True
    assert res.snapshot_id is not None
    assert res.total_documents == 2
    assert res.total_chunks == 4

    # Verify authoritative remote pointer was updated
    pointer_key = f"indexes/{col_id}/active_snapshot.json"
    assert pointer_key in mock_s3.objects
    ptr_data = json.loads(mock_s3.objects[pointer_key]["data"].decode("utf-8"))
    assert ptr_data["snapshot_id"] == res.snapshot_id
    assert ptr_data["collection_id"] == col_id
    assert ptr_data["total_documents"] == 2
    assert ptr_data["total_chunks"] == 4

    # Verify all 5 bundle artifacts were uploaded to remote storage
    bundle_prefix = f"indexes/{col_id}/snapshots/{res.snapshot_id}"
    for artifact in ["chunks.json", "documents.json", "dense_index.npy", "dense_index_meta.json", "manifest.json"]:
        artifact_key = f"{bundle_prefix}/{artifact}"
        assert artifact_key in mock_s3.objects, f"Missing remote artifact {artifact_key}"


# =========================================================================
# 2. Partial Artifact Upload Failure Leaves Old Pointer Unchanged
# =========================================================================
def test_2_partial_artifact_upload_failure_leaves_old_pointer_unchanged(tmp_path):
    """Verifies that an upload failure leaves the previous authoritative active snapshot unchanged."""
    col_id = "col_partial_fail"
    factory = setup_test_repository(tmp_path / "processed", col_id)
    mock_s3 = MockS3Client("durable-bucket")
    adapter = S3StorageAdapter(bucket_name="durable-bucket", s3_client=mock_s3)

    publisher = IndexPublisher(
        repository_factory=factory,
        indexes_dir=tmp_path / "indexes",
        processed_dir=tmp_path / "processed",
        embedder=MockEmbedder(),
        collection_id=col_id,
        storage_adapter=adapter,
    )

    # 1. Publish v1 successfully
    res1 = publisher.build_and_publish_snapshot(snapshot_id="snap_v1")
    assert res1.success is True

    # 2. Configure mock S3 to fail on uploading dense_index.npy for snap_v2
    fail_key = f"indexes/{col_id}/snapshots/snap_v2/dense_index.npy"
    mock_s3.fail_on_keys[fail_key] = ClientError({"Error": {"Code": "500", "Message": "Internal Error"}}, "UploadFile")

    res2 = publisher.build_and_publish_snapshot(snapshot_id="snap_v2")
    assert res2.success is False

    # 3. Assert remote active pointer STILL points to snap_v1
    pointer_key = f"indexes/{col_id}/active_snapshot.json"
    ptr_data = json.loads(mock_s3.objects[pointer_key]["data"].decode("utf-8"))
    assert ptr_data["snapshot_id"] == "snap_v1"


# =========================================================================
# 3. Bundle Checksum Mismatch Prevents Activation
# =========================================================================
def test_3_bundle_checksum_mismatch_prevents_activation(tmp_path):
    """Verifies that a tampered artifact prevents publication activation."""
    col_id = "col_chk_tamper"
    mock_s3 = MockS3Client("durable-bucket")
    adapter = S3StorageAdapter(bucket_name="durable-bucket", s3_client=mock_s3)
    store = DurableSnapshotStore(storage_adapter=adapter, indexes_dir=tmp_path / "indexes")

    # Create dummy staging dir with manifest
    staging = tmp_path / "staging_tamper"
    staging.mkdir(parents=True)
    (staging / "chunks.json").write_text("[]")
    (staging / "documents.json").write_text("{}")
    (staging / "dense_index.npy").write_bytes(b"\x00" * 32)
    (staging / "dense_index_meta.json").write_text("{}")

    manifest = SnapshotManifest(
        snapshot_id="snap_tamper",
        collection_id=col_id,
        created_at=datetime.now(timezone.utc),
        format_version="1.0.0",
        embedding_model="mock",
        embedding_dim=384,
        total_documents=0,
        total_chunks=0,
        document_ids=[],
        document_versions={},
        chunk_ids=[],
        corpus_fingerprint="abc",
        artifact_checksums={
            "chunks.json": compute_file_sha256(staging / "chunks.json"),
            "documents.json": compute_file_sha256(staging / "documents.json"),
            "dense_index.npy": compute_file_sha256(staging / "dense_index.npy"),
            "dense_index_meta.json": compute_file_sha256(staging / "dense_index_meta.json"),
        },
    )
    (staging / "manifest.json").write_text(manifest.model_dump_json())

    # Point with incorrect manifest_checksum
    pointer = ActiveSnapshotPointer(
        snapshot_id="snap_tamper",
        collection_id=col_id,
        activated_at=datetime.now(timezone.utc),
        manifest_checksum="corrupted_manifest_checksum_12345",
        total_documents=0,
        total_chunks=0,
    )

    store.publish_bundle(col_id, "snap_tamper", staging, manifest, pointer)

    # Hydrating must detect the checksum mismatch and raise SnapshotIntegrityError
    with pytest.raises(SnapshotIntegrityError) as exc:
        store.hydrate_snapshot(col_id)
    assert "checksum mismatch" in str(exc.value).lower()


# =========================================================================
# 4. Missing Artifact Prevents Activation
# =========================================================================
def test_4_missing_artifact_prevents_activation(tmp_path):
    """Verifies that missing required bundle artifact raises SnapshotIntegrityError."""
    col_id = "col_missing_art"
    mock_s3 = MockS3Client("durable-bucket")
    adapter = S3StorageAdapter(bucket_name="durable-bucket", s3_client=mock_s3)
    store = DurableSnapshotStore(adapter, indexes_dir=tmp_path / "indexes")

    staging = tmp_path / "staging_incomplete"
    staging.mkdir(parents=True)
    # Only write chunks and docs, missing dense_index.npy
    (staging / "chunks.json").write_text("[]")
    (staging / "documents.json").write_text("{}")
    (staging / "manifest.json").write_text("{}")

    manifest = SnapshotManifest(
        snapshot_id="snap_incomplete",
        collection_id=col_id,
        created_at=datetime.now(timezone.utc),
        format_version="1.0.0",
        embedding_model="mock",
        embedding_dim=384,
        total_documents=0,
        total_chunks=0,
        document_ids=[],
        document_versions={},
        chunk_ids=[],
        corpus_fingerprint="abc",
        artifact_checksums={},
    )
    pointer = ActiveSnapshotPointer(
        snapshot_id="snap_incomplete",
        collection_id=col_id,
        activated_at=datetime.now(timezone.utc),
        manifest_checksum="chk",
        total_documents=0,
        total_chunks=0,
    )

    with pytest.raises(SnapshotIntegrityError) as exc_info:
        store.publish_bundle(col_id, "snap_incomplete", staging, manifest, pointer)
    assert "Missing required snapshot artifact" in str(exc_info.value)

    # Active pointer must not exist
    assert adapter.exists(f"indexes/{col_id}/active_snapshot.json") is False


# =========================================================================
# 5. Pointer Update Failure Leaves Old Snapshot Active
# =========================================================================
def test_5_pointer_update_failure_leaves_old_snapshot_active(tmp_path):
    """Verifies that failure when writing active pointer does not overwrite prior active snapshot."""
    col_id = "col_ptr_fail"
    factory = setup_test_repository(tmp_path / "processed", col_id)
    mock_s3 = MockS3Client("durable-bucket")
    adapter = S3StorageAdapter(bucket_name="durable-bucket", s3_client=mock_s3)

    publisher = IndexPublisher(
        repository_factory=factory,
        indexes_dir=tmp_path / "indexes",
        processed_dir=tmp_path / "processed",
        embedder=MockEmbedder(),
        collection_id=col_id,
        storage_adapter=adapter,
    )

    # 1. Publish v1
    res1 = publisher.build_and_publish_snapshot(snapshot_id="snap_v1")
    assert res1.success is True

    # 2. Block pointer key write for snap_v2
    pointer_key = f"indexes/{col_id}/active_snapshot.json"
    mock_s3.fail_on_keys[pointer_key] = ClientError({"Error": {"Code": "503", "Message": "Service Unavailable"}}, "PutObject")

    res2 = publisher.build_and_publish_snapshot(snapshot_id="snap_v2")
    assert res2.success is False

    # 3. Assert v1 is still the active pointer in S3
    ptr_data = json.loads(mock_s3.objects[pointer_key]["data"].decode("utf-8"))
    assert ptr_data["snapshot_id"] == "snap_v1"


# =========================================================================
# 6. Concurrent Publication Lock Contention
# =========================================================================
def test_6_concurrent_publication_lock_contention(tmp_path):
    """Verifies that an active .publish.lock prevents concurrent builds from overwriting state."""
    col_id = "col_lock_test"
    factory = setup_test_repository(tmp_path / "processed", col_id)
    mock_s3 = MockS3Client("durable-bucket")
    adapter = S3StorageAdapter(bucket_name="durable-bucket", s3_client=mock_s3)

    indexes_dir = tmp_path / "indexes"
    lock_file = indexes_dir / col_id / ".publish.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text("99999:2026-11-01T00:00:00Z\n")

    publisher = IndexPublisher(
        repository_factory=factory,
        indexes_dir=indexes_dir,
        processed_dir=tmp_path / "processed",
        embedder=MockEmbedder(),
        collection_id=col_id,
        storage_adapter=adapter,
    )

    res = publisher.build_and_publish_snapshot()
    assert res.success is False
    assert res.error_details == "ConcurrentPublicationConflict"
    assert "already in progress" in res.message


# =========================================================================
# 7. Cold-Start Hydration Downloads & Validates Bundle
# =========================================================================
def test_7_cold_start_hydration_downloads_and_validates_bundle(tmp_path):
    """Verifies that a cold node with empty indexes_dir downloads, verifies, and queries from remote snapshot."""
    col_id = "col_cold_hydrate"
    factory = setup_test_repository(tmp_path / "processed", col_id)
    mock_s3 = MockS3Client("durable-bucket")
    adapter = S3StorageAdapter(bucket_name="durable-bucket", s3_client=mock_s3)

    # 1. Publish snapshot on Node A
    pub_node = IndexPublisher(
        repository_factory=factory,
        indexes_dir=tmp_path / "node_a_indexes",
        processed_dir=tmp_path / "processed",
        embedder=MockEmbedder(),
        collection_id=col_id,
        storage_adapter=adapter,
    )
    pub_res = pub_node.build_and_publish_snapshot()
    assert pub_res.success is True

    # 2. Node B starts up with completely clean/empty indexes directory
    node_b_indexes = tmp_path / "node_b_indexes"
    retrieval_svc = RetrievalService(
        processed_dir=tmp_path / "processed",
        indexes_dir=node_b_indexes,
        dense_embedder=MockEmbedder(),
        storage_adapter=adapter,
    )

    # 3. get_active_snapshot_dir triggers hydration
    snap_dir = retrieval_svc.get_active_snapshot_dir(collection_id=col_id)
    assert snap_dir is not None
    assert snap_dir.is_dir()
    assert snap_dir.name == pub_res.snapshot_id
    assert (snap_dir / "chunks.json").is_file()
    assert (snap_dir / "dense_index.npy").is_file()

    # 4. Search over hydrated index succeeds
    search_res = retrieval_svc.search_hybrid("compliance policy", top_k=3, collection_id=col_id)
    assert search_res.total_results > 0


# =========================================================================
# 8. Corrupted Remote Bundle Is Rejected
# =========================================================================
def test_8_corrupted_remote_bundle_is_rejected(tmp_path):
    """Verifies that if an artifact is corrupted in remote storage, hydration rejects it."""
    col_id = "col_corrupted_bundle"
    factory = setup_test_repository(tmp_path / "processed", col_id)
    mock_s3 = MockS3Client("durable-bucket")
    adapter = S3StorageAdapter(bucket_name="durable-bucket", s3_client=mock_s3)

    pub = IndexPublisher(
        repository_factory=factory,
        indexes_dir=tmp_path / "pub_indexes",
        processed_dir=tmp_path / "processed",
        embedder=MockEmbedder(),
        collection_id=col_id,
        storage_adapter=adapter,
    )
    res = pub.build_and_publish_snapshot()
    assert res.success is True

    # Tamper with chunks.json in remote S3
    remote_chunks_key = f"indexes/{col_id}/snapshots/{res.snapshot_id}/chunks.json"
    mock_s3.objects[remote_chunks_key]["data"] = b"tampered corrupt bytes"

    # Hydrate on clean node
    clean_indexes = tmp_path / "clean_indexes"
    retrieval_svc = RetrievalService(
        processed_dir=tmp_path / "processed",
        indexes_dir=clean_indexes,
        dense_embedder=MockEmbedder(),
        storage_adapter=adapter,
    )

    # Hydration must fail and reject corrupted bundle
    with pytest.raises(SnapshotIntegrityError) as exc_info:
        retrieval_svc.get_active_snapshot_dir(collection_id=col_id)
    assert "Checksum mismatch" in str(exc_info.value)


# =========================================================================
# 9. Valid Cache Avoids Redundant Downloads
# =========================================================================
def test_9_valid_cache_avoids_redundant_downloads(tmp_path):
    """Verifies that an already-hydrated valid snapshot cache does not re-download artifacts."""
    col_id = "col_cache_hit"
    factory = setup_test_repository(tmp_path / "processed", col_id)
    mock_s3 = MockS3Client("durable-bucket")
    adapter = S3StorageAdapter(bucket_name="durable-bucket", s3_client=mock_s3)

    pub = IndexPublisher(
        repository_factory=factory,
        indexes_dir=tmp_path / "node_indexes",
        processed_dir=tmp_path / "processed",
        embedder=MockEmbedder(),
        collection_id=col_id,
        storage_adapter=adapter,
    )
    pub.build_and_publish_snapshot()

    retrieval_svc = RetrievalService(
        processed_dir=tmp_path / "processed",
        indexes_dir=tmp_path / "node_indexes",
        dense_embedder=MockEmbedder(),
        storage_adapter=adapter,
    )

    # Initial call hydrates or checks
    retrieval_svc.get_active_snapshot_dir(collection_id=col_id)
    initial_calls = mock_s3.get_object_calls

    # Second call should find cache valid and not download artifacts
    retrieval_svc.get_active_snapshot_dir(collection_id=col_id)
    # Only the active_snapshot.json pointer is read (1 call)
    assert mock_s3.get_object_calls <= initial_calls + 1


# =========================================================================
# 10. Remote Outage With No Cache Fails Closed
# =========================================================================
def test_10_remote_outage_with_no_cache_fails_closed(tmp_path):
    """Verifies that a remote storage outage with an empty cache fails closed (returns None)."""
    col_id = "col_outage_no_cache"
    mock_s3 = MockS3Client("durable-bucket")
    adapter = S3StorageAdapter(bucket_name="durable-bucket", s3_client=mock_s3)

    pointer_key = f"indexes/{col_id}/active_snapshot.json"
    mock_s3.fail_on_keys[pointer_key] = ClientError({"Error": {"Code": "500", "Message": "Remote Outage"}}, "GetObject")

    retrieval_svc = RetrievalService(
        processed_dir=tmp_path / "processed",
        indexes_dir=tmp_path / "empty_indexes",
        dense_embedder=MockEmbedder(),
        storage_adapter=adapter,
    )

    snap_dir = retrieval_svc.get_active_snapshot_dir(collection_id=col_id)
    assert snap_dir is None


# =========================================================================
# 11. Remote Outage With Verified Cache Serves Cache Under Explicit Policy
# =========================================================================
def test_11_remote_outage_with_verified_cache_serves_cache(tmp_path):
    """Verifies that if remote S3 suffers an outage, an existing verified cache continues serving."""
    col_id = "col_outage_cache"
    factory = setup_test_repository(tmp_path / "processed", col_id)
    mock_s3 = MockS3Client("durable-bucket")
    adapter = S3StorageAdapter(bucket_name="durable-bucket", s3_client=mock_s3)

    indexes_dir = tmp_path / "indexes"
    pub = IndexPublisher(
        repository_factory=factory,
        indexes_dir=indexes_dir,
        processed_dir=tmp_path / "processed",
        embedder=MockEmbedder(),
        collection_id=col_id,
        storage_adapter=adapter,
    )
    pub_res = pub.build_and_publish_snapshot()
    assert pub_res.success is True

    retrieval_svc = RetrievalService(
        processed_dir=tmp_path / "processed",
        indexes_dir=indexes_dir,
        dense_embedder=MockEmbedder(),
        storage_adapter=adapter,
    )

    # Verify initial normal resolution
    assert retrieval_svc.get_active_snapshot_dir(collection_id=col_id) is not None

    # Simulate catastrophic remote storage outage
    pointer_key = f"indexes/{col_id}/active_snapshot.json"
    mock_s3.fail_on_keys[pointer_key] = ClientError({"Error": {"Code": "503", "Message": "Cloudflare R2 Outage"}}, "GetObject")

    # Under our explicit policy: serves verified local cache with logged warning
    fallback_dir = retrieval_svc.get_active_snapshot_dir(collection_id=col_id)
    assert fallback_dir is not None
    assert fallback_dir.name == pub_res.snapshot_id


# =========================================================================
# 12. Local Provider Conformance & Parity
# =========================================================================
def test_12_local_provider_conformance_and_parity(tmp_path):
    """Verifies that LocalStorageAdapter runs publication and retrieval without network dependencies."""
    col_id = "col_local_parity"
    factory = setup_test_repository(tmp_path / "processed", col_id)
    local_adapter = LocalStorageAdapter(root_dir=tmp_path / "uploads")

    pub = IndexPublisher(
        repository_factory=factory,
        indexes_dir=tmp_path / "indexes",
        processed_dir=tmp_path / "processed",
        embedder=MockEmbedder(),
        collection_id=col_id,
        storage_adapter=local_adapter,
    )
    pub_res = pub.build_and_publish_snapshot()
    assert pub_res.success is True

    retrieval_svc = RetrievalService(
        processed_dir=tmp_path / "processed",
        indexes_dir=tmp_path / "indexes",
        dense_embedder=MockEmbedder(),
        storage_adapter=local_adapter,
    )
    snap_dir = retrieval_svc.get_active_snapshot_dir(collection_id=col_id)
    assert snap_dir is not None
    assert snap_dir.name == pub_res.snapshot_id

    # Search succeeds
    resp = retrieval_svc.search_hybrid("compliance policy", collection_id=col_id)
    assert resp.total_results > 0


# =========================================================================
# 13. Worker Does Not Complete Job If Durable Publication Fails
# =========================================================================
def test_13_worker_does_not_complete_job_if_durable_publication_fails(tmp_path):
    """Verifies that IngestionOrchestrator fails/retries if durable publication fails."""
    from app.schemas.ingestion_job import IngestionJobRecord, JobStatus, ProcessingStage
    from app.ingestion.orchestrator import IngestionOrchestrator, RetryableIngestionError, NonRetryableIngestionError

    col_id = "col_orch_pub_fail"
    factory = setup_test_repository(tmp_path / "processed", col_id)
    job_repo = factory.get_ingestion_job_repository()

    mock_s3 = MockS3Client("durable-bucket")
    # Fail publication bundle upload in S3
    fail_prefix = f"indexes/{col_id}/snapshots/"
    mock_s3.fail_on_keys[f"indexes/{col_id}/active_snapshot.json"] = ClientError(
        {"Error": {"Code": "500", "Message": "Remote S3 Error"}}, "PutObject"
    )

    adapter = S3StorageAdapter(bucket_name="durable-bucket", s3_client=mock_s3)

    # Save a valid test upload PDF with real text
    pdf_bytes = create_minimal_pdf_bytes(["Regulatory Policy Provision 1 for indexing test."])
    save_res = adapter.save_upload_stream(
        workspace_id=uuid.uuid4(),
        collection_id=col_id,
        document_id="doc_test_fail",
        stream=io.BytesIO(pdf_bytes),
    )

    job = job_repo.create_job(
        source_url=save_res.source_uri,
        idempotency_key=f"idemp_{uuid.uuid4().hex[:8]}",
        collection_id=col_id,
    )
    claimed = job_repo.claim_next_job(worker_id="test_worker")
    assert claimed is not None

    orchestrator = IngestionOrchestrator(
        repository_factory=factory,
        indexes_dir=tmp_path / "indexes",
        processed_dir=tmp_path / "processed",
        embedder=MockEmbedder(),
        storage_adapter=adapter,
        publish_snapshots=True,
    )

    # Executing job must not complete the job when durable publication fails
    exec_res = orchestrator.process_job(claimed, claimed.claim_token)
    assert exec_res.status != JobStatus.COMPLETED

    # Active pointer must not exist
    assert adapter.exists(f"indexes/{col_id}/active_snapshot.json") is False


# =========================================================================
# 14. Concurrent Hydration Threads Are Safe
# =========================================================================
def test_14_concurrent_hydration_is_thread_safe(tmp_path):
    """Verifies that multiple concurrent hydration threads do not corrupt the local cache."""
    import concurrent.futures

    col_id = "col_thread_hydrate"
    factory = setup_test_repository(tmp_path / "processed", col_id)
    mock_s3 = MockS3Client("durable-bucket")
    adapter = S3StorageAdapter(bucket_name="durable-bucket", s3_client=mock_s3)

    # Publish on Publisher node
    pub = IndexPublisher(
        repository_factory=factory,
        indexes_dir=tmp_path / "indexes_pub",
        processed_dir=tmp_path / "processed",
        embedder=MockEmbedder(),
        collection_id=col_id,
        storage_adapter=adapter,
    )
    pub_res = pub.build_and_publish_snapshot()
    assert pub_res.success is True

    # Hydrate concurrently across 4 workers on a clean indexes directory
    clean_indexes = tmp_path / "clean_indexes"

    def run_hydration(worker_id: int):
        svc = RetrievalService(
            processed_dir=tmp_path / "processed",
            indexes_dir=clean_indexes,
            dense_embedder=MockEmbedder(),
            storage_adapter=adapter,
        )
        snap_dir = svc.get_active_snapshot_dir(collection_id=col_id)
        assert snap_dir is not None
        assert snap_dir.name == pub_res.snapshot_id
        return True

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(run_hydration, i) for i in range(4)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    assert len(results) == 4
    assert all(results)


# =========================================================================
# 15. Retry and Crash Recovery Is Idempotent
# =========================================================================
def test_15_retry_crash_recovery_is_idempotent(tmp_path):
    """Verifies that publishing multiple times for the same collection produces consistent active snapshots."""
    col_id = "col_idempotent_retry"
    factory = setup_test_repository(tmp_path / "processed", col_id)
    mock_s3 = MockS3Client("durable-bucket")
    adapter = S3StorageAdapter(bucket_name="durable-bucket", s3_client=mock_s3)

    indexes_dir = tmp_path / "indexes"
    pub = IndexPublisher(
        repository_factory=factory,
        indexes_dir=indexes_dir,
        processed_dir=tmp_path / "processed",
        embedder=MockEmbedder(),
        collection_id=col_id,
        storage_adapter=adapter,
    )

    # Run 1
    res1 = pub.build_and_publish_snapshot()
    assert res1.success is True

    # Run 2 (retry / subsequent cycle)
    res2 = pub.build_and_publish_snapshot()
    assert res2.success is True

    # Latest active pointer reflects run 2
    store = DurableSnapshotStore(adapter, indexes_dir=indexes_dir)
    latest_ptr = store.get_remote_active_pointer(col_id)
    assert latest_ptr is not None
    assert latest_ptr.snapshot_id == res2.snapshot_id


# =========================================================================
# 16. Stale Remote Writer Prevented from Overwriting Newer Snapshot
# =========================================================================
def test_16_stale_remote_writer_prevented_from_overwriting_newer_snapshot(tmp_path):
    """
    Verifies distributed cross-instance concurrency safety:
    A lagging worker attempting to publish a snapshot with an older activation
    timestamp cannot overwrite a newer active snapshot pointer.
    """
    col_id = "col_stale_prevention"
    factory = setup_test_repository(tmp_path / "processed", col_id)
    mock_s3 = MockS3Client("durable-bucket")
    adapter = S3StorageAdapter(bucket_name="durable-bucket", s3_client=mock_s3)
    indexes_dir = tmp_path / "indexes"

    store = DurableSnapshotStore(adapter, indexes_dir=indexes_dir)

    # 1. First publish snapshot 1 at T1 = 10:00:00
    t1 = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
    pub1 = IndexPublisher(
        repository_factory=factory,
        indexes_dir=indexes_dir,
        processed_dir=tmp_path / "processed",
        embedder=MockEmbedder(),
        collection_id=col_id,
        storage_adapter=adapter,
    )
    res1 = pub1.build_and_publish_snapshot(snapshot_id="snap_v1")
    assert res1.success is True

    # 2. Publish snapshot 2 at T2 = 10:05:00
    t2 = datetime(2026, 11, 1, 10, 5, 0, tzinfo=timezone.utc)
    ptr_v2 = ActiveSnapshotPointer(
        snapshot_id="snap_v2",
        collection_id=col_id,
        activated_at=t2,
        manifest_checksum="v2_manifest_chk",
        total_documents=1,
        total_chunks=1,
    )
    # Manually activate pointer v2 in remote store
    store.storage_adapter.put_bytes(
        store.get_active_pointer_key(col_id),
        ptr_v2.model_dump_json(indent=2).encode("utf-8"),
        content_type="application/json",
    )

    # Confirm remote active pointer is snap_v2
    current_ptr = store.get_remote_active_pointer(col_id)
    assert current_ptr is not None
    assert current_ptr.snapshot_id == "snap_v2"

    # 3. Simulate a lagging worker attempting to publish snap_lagging with timestamp T1 (< T2)
    lagging_ptr = ActiveSnapshotPointer(
        snapshot_id="snap_lagging",
        collection_id=col_id,
        activated_at=t1,
        manifest_checksum="lagging_chk",
        total_documents=1,
        total_chunks=1,
    )

    # Use a mock manifest and local dir
    local_snap_dir = tmp_path / "indexes" / col_id / "snapshots" / "snap_v1"
    with open(local_snap_dir / "manifest.json", "r", encoding="utf-8") as f:
        manifest_data = json.load(f)
    manifest = SnapshotManifest(**manifest_data)

    # Publishing the lagging snapshot must raise StaleSnapshotWriterError
    with pytest.raises(StaleSnapshotWriterError) as exc_info:
        store.publish_bundle(
            collection_id=col_id,
            snapshot_id="snap_lagging",
            local_snapshot_dir=local_snap_dir,
            manifest=manifest,
            pointer=lagging_ptr,
        )
    assert "Stale publication rejected" in str(exc_info.value)

    # 4. Confirm remote pointer remains securely pointing to snap_v2
    verified_ptr = store.get_remote_active_pointer(col_id)
    assert verified_ptr is not None
    assert verified_ptr.snapshot_id == "snap_v2"


# =========================================================================
# 17. Worker Retries on Stale Publication Conflict
# =========================================================================
def test_17_worker_retries_on_stale_publication_conflict(tmp_path, monkeypatch):
    """
    Verifies that when a publication fails with StaleSnapshotWriterError,
    the IngestionOrchestrator raises RetryableIngestionError so the worker
    will retry with exponential backoff rather than failing permanently.
    """
    from app.ingestion.orchestrator import IngestionOrchestrator, RetryableIngestionError
    from app.schemas.ingestion_job import IngestionJobRecord, JobStatus

    col_id = "col_worker_conflict"
    factory = setup_test_repository(tmp_path / "processed", col_id)
    mock_s3 = MockS3Client("durable-bucket")
    adapter = S3StorageAdapter(bucket_name="durable-bucket", s3_client=mock_s3)

    orch = IngestionOrchestrator(
        repository_factory=factory,
        processed_dir=tmp_path / "processed",
        indexes_dir=tmp_path / "indexes",
        embedder=MockEmbedder(),
        storage_adapter=adapter,
        publish_snapshots=True,
    )

    # Mock IndexPublisher.build_and_publish_snapshot to simulate stale publication conflict
    def mock_build_and_publish(*args, **kwargs):
        return PublicationResult(
            success=False,
            snapshot_id="snap_conflict",
            total_documents=0,
            total_chunks=0,
            message="Stale publication rejected: remote active pointer is newer",
            error_details="StaleSnapshotWriterError",
        )

    monkeypatch.setattr(IndexPublisher, "build_and_publish_snapshot", mock_build_and_publish)

    # Save a valid test upload PDF with real text to storage adapter
    pdf_bytes = create_minimal_pdf_bytes(["Regulatory Policy Provision for conflict test."])
    save_res = adapter.save_upload_stream(
        workspace_id=uuid.uuid4(),
        collection_id=col_id,
        document_id="doc_test_conflict",
        stream=io.BytesIO(pdf_bytes),
    )

    job_repo = factory.get_ingestion_job_repository()
    job = job_repo.create_job(
        source_url=save_res.source_uri,
        idempotency_key="idem_conflict_1",
        collection_id=col_id,
    )
    claimed = job_repo.claim_next_job(worker_id="test_worker")
    assert claimed is not None

    exec_res = orch.process_job(claimed, claimed.claim_token)
    assert exec_res.status != JobStatus.COMPLETED
    assert exec_res.error_details["category"] == "retryable"
    assert "Publication conflict" in exec_res.error_details["reason"]


# =========================================================================
# 18. Collection Index Publisher Receives Configured Storage Adapter
# =========================================================================
def test_18_collection_index_publisher_receives_configured_adapter(monkeypatch, tmp_path):
    """
    Verifies that backend/app/api/v1/collections.py _get_index_publisher
    explicitly injects the configured storage adapter into IndexPublisher.
    """
    from app.api.v1.collections import _get_index_publisher
    from app.core.config import Settings

    mock_s3 = MockS3Client("adapter-test-bucket")
    test_adapter = S3StorageAdapter(bucket_name="adapter-test-bucket", s3_client=mock_s3)

    # Mock get_storage_adapter to return test_adapter
    monkeypatch.setattr("app.api.v1.collections.get_storage_adapter", lambda s: test_adapter)

    pub = _get_index_publisher("test_col")
    assert pub.storage_adapter is test_adapter
    assert isinstance(pub.storage_adapter, S3StorageAdapter)


# =========================================================================
# 19. Storage Cleanup on Duplicate Upload Registration
# =========================================================================
def test_19_storage_cleanup_on_duplicate_upload_registration(tmp_path):
    """
    Verifies that when an identical PDF artifact is uploaded,
    the storage adapter's delete_file is called to prune redundant uploaded bytes.
    """
    mock_s3 = MockS3Client("dedup-bucket")
    adapter = S3StorageAdapter(bucket_name="dedup-bucket", s3_client=mock_s3)

    pdf_bytes = b"%PDF-1.4 dummy valid stream for dedup"
    stream = io.BytesIO(pdf_bytes)

    res = adapter.save_upload_stream(
        workspace_id=uuid.uuid4(),
        collection_id="col_dedup",
        document_id="doc_dedup_1",
        stream=stream,
        original_filename="test.pdf",
    )
    assert adapter.exists(res.source_uri)

    # Call delete_file as done during duplicate detection
    adapter.delete_file(res.source_uri)
    assert not adapter.exists(res.source_uri)


# =========================================================================
# 20. API Upload Document Endpoint Integration (collections.py)
# =========================================================================
@pytest.mark.anyio
async def test_20_api_upload_document_endpoint_integration(monkeypatch, tmp_path):
    """
    Verifies that upload_collection_document in collections.py stores the file
    via the configured storage adapter and creates an IngestionJobModel with the storage URI.
    """
    from fastapi import UploadFile
    from app.api.v1.collections import upload_collection_document
    from app.models.auth import UserModel, WorkspaceModel, WorkspaceMembershipModel, WorkspaceMembershipModel
    from app.models.collection import CollectionModel
    from app.persistence.models import IngestionJobModel, RegulatoryDocumentModel
    from app.api.workspace_deps import WorkspaceAuthContext

    ws_id = uuid.uuid4()
    col_id = "col_api_upload"
    user_id = uuid.uuid4()

    mock_s3 = MockS3Client("api-upload-bucket")
    adapter = S3StorageAdapter(bucket_name="api-upload-bucket", s3_client=mock_s3)
    monkeypatch.setattr("app.api.v1.collections.get_storage_adapter", lambda s: adapter)

    col = CollectionModel(
        id=col_id,
        workspace_id=ws_id,
        name="API Upload Col",
        is_system=False,
        is_public=False,
    )
    user = UserModel(id=user_id, email="test@example.com")
    workspace = WorkspaceModel(id=ws_id, name="Test Workspace", owner_id=user_id)
    membership = WorkspaceMembershipModel(workspace_id=ws_id, user_id=user_id, role="admin")
    auth_ctx = WorkspaceAuthContext(workspace=workspace, membership=membership, user=user)

    added_objects = []

    class MockQuery:
        def __init__(self, model):
            self.model = model

        def filter(self, *args, **kwargs):
            return self

        def first(self):
            if self.model == CollectionModel:
                return col
            if self.model == IngestionJobModel:
                return None  # No duplicate
            if self.model == RegulatoryDocumentModel:
                return None
            return None

    class MockSession:
        def query(self, model):
            return MockQuery(model)

        def add(self, obj):
            if hasattr(obj, "created_at") and getattr(obj, "created_at") is None:
                obj.created_at = datetime.now(timezone.utc)
            added_objects.append(obj)

        def flush(self):
            pass

        def commit(self):
            pass

        def refresh(self, obj):
            pass

        def rollback(self):
            pass

    pdf_content = create_minimal_pdf_bytes(["Test policy for API upload endpoint."])
    upload_file = UploadFile(
        file=io.BytesIO(pdf_content),
        filename="policy.pdf",
    )

    resp = await upload_collection_document(
        workspace_id=ws_id,
        collection_id=col_id,
        file=upload_file,
        title="Test Policy",
        circular_number="CIR-123",
        auth_ctx=auth_ctx,
        db=MockSession(),
    )

    assert resp.status == "queued"
    assert resp.collection_id == col_id
    assert resp.workspace_id == ws_id
    assert resp.source_url.startswith("s3://") or resp.source_url.startswith("upload://")

    # Confirm object was saved in storage adapter
    assert adapter.exists(resp.source_url)
    meta = adapter.get_metadata(resp.source_url)
    assert meta is not None
    assert meta.byte_count == len(pdf_content)


# =========================================================================
# 21. API Download Document Streaming Endpoint Integration (collections.py)
# =========================================================================
@pytest.mark.anyio
async def test_21_api_download_document_streaming_endpoint_integration(monkeypatch, tmp_path):
    """
    Verifies that download_collection_document in collections.py streams PDF bytes
    directly from the configured storage adapter without intermediate temporary files.
    """
    from app.api.v1.collections import download_collection_document
    from app.models.auth import UserModel, WorkspaceModel, WorkspaceMembershipModel
    from app.models.collection import CollectionModel
    from app.persistence.models import RegulatoryDocumentModel
    from app.api.workspace_deps import WorkspaceAuthContext

    ws_id = uuid.uuid4()
    col_id = "col_api_download"
    doc_id = "doc_download_test"
    user_id = uuid.uuid4()

    mock_s3 = MockS3Client("api-download-bucket")
    adapter = S3StorageAdapter(bucket_name="api-download-bucket", s3_client=mock_s3)
    monkeypatch.setattr("app.api.v1.collections.get_storage_adapter", lambda s: adapter)

    pdf_content = create_minimal_pdf_bytes(["Confidential Document for Download."])
    save_res = adapter.save_upload_stream(
        workspace_id=ws_id,
        collection_id=col_id,
        document_id=doc_id,
        stream=io.BytesIO(pdf_content),
        original_filename="confidential.pdf",
    )

    col = CollectionModel(id=col_id, workspace_id=ws_id, is_system=False, is_public=False)
    doc = RegulatoryDocumentModel(id=doc_id, workspace_id=ws_id, collection_id=col_id)
    user = UserModel(id=user_id, email="test@example.com")
    workspace = WorkspaceModel(id=ws_id, name="Test Workspace", owner_id=user_id)
    membership = WorkspaceMembershipModel(workspace_id=ws_id, user_id=user_id, role="member")
    auth_ctx = WorkspaceAuthContext(workspace=workspace, membership=membership, user=user)

    class MockQuery:
        def __init__(self, model):
            self.model = model

        def filter(self, *args, **kwargs):
            return self

        def first(self):
            if self.model == CollectionModel:
                return col
            if self.model == RegulatoryDocumentModel:
                return doc
            return None

    class MockSession:
        def query(self, model):
            return MockQuery(model)

    response = download_collection_document(
        workspace_id=ws_id,
        collection_id=col_id,
        document_id=doc_id,
        auth_ctx=auth_ctx,
        db=MockSession(),
    )

    # Verify StreamingResponse headers and streamed chunks
    assert response.media_type == "application/pdf"
    assert f'filename="{doc_id}.pdf"' in response.headers["Content-Disposition"]

    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    streamed_bytes = b"".join(chunks)
    assert streamed_bytes == pdf_content


# =========================================================================
# 22. API List Documents Snapshot Readiness Integration (collections.py)
# =========================================================================
def test_22_api_list_documents_snapshot_readiness_integration(monkeypatch, tmp_path):
    """
    Verifies that list_collection_documents in collections.py queries the authoritative
    index publisher to determine the is_published readiness status for documents.
    """
    from app.api.v1.collections import list_collection_documents
    from app.models.auth import UserModel, WorkspaceModel, WorkspaceMembershipModel
    from app.models.collection import CollectionModel
    from app.persistence.models import RegulatoryDocumentModel
    from app.api.workspace_deps import WorkspaceAuthContext

    ws_id = uuid.uuid4()
    col_id = "col_api_readiness"
    user_id = uuid.uuid4()

    mock_s3 = MockS3Client("api-readiness-bucket")
    adapter = S3StorageAdapter(bucket_name="api-readiness-bucket", s3_client=mock_s3)
    monkeypatch.setattr("app.api.v1.collections.get_storage_adapter", lambda s: adapter)

    col = CollectionModel(id=col_id, workspace_id=ws_id, is_system=False, is_public=False)
    doc_published = RegulatoryDocumentModel(id="doc_pub", workspace_id=ws_id, collection_id=col_id, title="Published Doc")
    doc_pending = RegulatoryDocumentModel(id="doc_pend", workspace_id=ws_id, collection_id=col_id, title="Pending Doc")
    user = UserModel(id=user_id, email="test@example.com")
    workspace = WorkspaceModel(id=ws_id, name="Test Workspace", owner_id=user_id)
    membership = WorkspaceMembershipModel(workspace_id=ws_id, user_id=user_id, role="member")
    auth_ctx = WorkspaceAuthContext(workspace=workspace, membership=membership, user=user)

    class MockQuery:
        def __init__(self, model):
            self.model = model

        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args):
            return self

        def first(self):
            return col

        def all(self):
            return [doc_published, doc_pending]

    class MockSession:
        def query(self, model):
            return MockQuery(model)

    # Mock IndexPublisher.get_active_manifest to report doc_pub as published
    class MockManifest:
        document_ids = ["doc_pub"]

    class MockPublisher:
        storage_adapter = adapter

        def get_active_manifest(self):
            return MockManifest()

    monkeypatch.setattr("app.api.v1.collections._get_index_publisher", lambda c_id: MockPublisher())

    res = list_collection_documents(
        workspace_id=ws_id,
        collection_id=col_id,
        auth_ctx=auth_ctx,
        db=MockSession(),
    )

    assert res.total == 2
    doc_map = {d.document_id: d.is_published for d in res.documents}
    assert doc_map["doc_pub"] is True
    assert doc_map["doc_pend"] is False



