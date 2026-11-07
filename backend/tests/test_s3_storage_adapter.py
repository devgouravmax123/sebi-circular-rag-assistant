"""
Unit tests for Milestone 11 Wave A:
S3StorageAdapter contract conformance, security, streaming, and error handling.
"""
from datetime import datetime, timezone
import hashlib
import io
import os
from pathlib import Path
import tempfile
from typing import Any
import uuid
import fitz
import pytest

from botocore.exceptions import ClientError

from app.storage import (
    BaseStorageAdapter,
    S3StorageAdapter,
    StorageSaveResult,
    StorageObjectMetadata,
    StorageError,
    StorageConfigurationError,
    PathTraversalError,
    StorageLimitExceededError,
    InvalidFileFormatError,
    StorageFileNotFoundError,
)


class MockS3Client:
    """In-memory mock boto3 S3 client implementing object storage operations."""

    def __init__(self, bucket_name: str = "test-bucket"):
        self.bucket_name = bucket_name
        self.objects: dict[str, dict] = {}

    def _client_error(self, code: str, msg: str, op: str) -> ClientError:
        return ClientError(
            {"Error": {"Code": code, "Message": msg}},
            op,
        )

    def put_object(self, Bucket: str, Key: str, Body: Any, ContentType: str = "application/octet-stream", Metadata: dict = None, **kwargs) -> dict:
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
        if Key not in self.objects:
            raise self._client_error("NoSuchKey", f"The specified key does not exist: {Key}", "GetObject")
        obj = self.objects[Key]
        return {
            "Body": io.BytesIO(obj["data"]),
            "ContentLength": obj["ContentLength"],
            "ContentType": obj["ContentType"],
            "Metadata": obj["Metadata"],
            "LastModified": obj["LastModified"],
        }

    def head_object(self, Bucket: str, Key: str, **kwargs) -> dict:
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
        if Key not in self.objects:
            raise self._client_error("NoSuchKey", f"Key not found: {Key}", "DownloadFile")
        p = Path(Filename)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            f.write(self.objects[Key]["data"])


def create_minimal_pdf_bytes(text_lines: list[str]) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    y = 50
    for line in text_lines:
        page.insert_text((50, y), line)
        y += 20
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def test_s3_adapter_configuration_validation():
    """Verifies that S3StorageAdapter rejects missing credentials with StorageConfigurationError."""
    # Missing bucket
    with pytest.raises(StorageConfigurationError) as exc:
        S3StorageAdapter(bucket_name=None, access_key_id="acc", secret_access_key="sec")
    assert "S3_BUCKET_NAME" in str(exc.value)

    # Missing access key
    with pytest.raises(StorageConfigurationError) as exc:
        S3StorageAdapter(bucket_name="my-bucket", access_key_id="", secret_access_key="sec")
    assert "S3_ACCESS_KEY_ID" in str(exc.value)

    # Missing secret key
    with pytest.raises(StorageConfigurationError) as exc:
        S3StorageAdapter(bucket_name="my-bucket", access_key_id="acc", secret_access_key=" ")
    assert "S3_SECRET_ACCESS_KEY" in str(exc.value)


def test_s3_adapter_upload_stream_valid_pdf_and_metadata():
    """Verifies streaming upload to S3, SHA-256 calculation, and metadata storage."""
    mock_client = MockS3Client("corp-bucket")
    adapter = S3StorageAdapter(bucket_name="corp-bucket", s3_client=mock_client)

    ws_id = uuid.uuid4()
    col_id = "col_s3_test"
    doc_id = "doc_s3_001"

    pdf_bytes = create_minimal_pdf_bytes(["SEBI Compliance Section 4: Cloud Storage."])
    expected_sha = hashlib.sha256(pdf_bytes).hexdigest()

    res = adapter.save_upload_stream(
        workspace_id=ws_id,
        collection_id=col_id,
        document_id=doc_id,
        stream=io.BytesIO(pdf_bytes),
        original_filename="sebi_compliance.pdf",
    )

    assert isinstance(res, StorageSaveResult)
    expected_key = f"uploads/{ws_id}/{col_id}/{doc_id}.pdf"
    assert res.storage_path == expected_key
    assert res.sha256 == expected_sha
    assert res.byte_count == len(pdf_bytes)
    assert res.source_uri == f"s3://corp-bucket/{expected_key}"

    # Verify exists
    assert adapter.exists(res.source_uri) is True
    assert adapter.exists(f"upload://{ws_id}/{col_id}/{doc_id}.pdf") is True

    # Verify metadata
    meta = adapter.get_metadata(res.source_uri)
    assert meta is not None
    assert meta.byte_count == len(pdf_bytes)
    assert meta.sha256 == expected_sha
    assert meta.content_type == "application/pdf"

    # Verify open_stream
    with adapter.open_stream(res.source_uri) as stream:
        assert stream.read() == pdf_bytes


def test_s3_adapter_upload_size_limit_and_temp_cleanup():
    """Verifies that exceeding max_upload_bytes raises StorageLimitExceededError and does not write to S3."""
    mock_client = MockS3Client("corp-bucket")
    adapter = S3StorageAdapter(bucket_name="corp-bucket", s3_client=mock_client, max_upload_bytes=100)

    oversized_bytes = b"%PDF-1.4\n" + b"Y" * 500

    with pytest.raises(StorageLimitExceededError):
        adapter.save_upload_stream(
            workspace_id=uuid.uuid4(),
            collection_id="col1",
            document_id="doc_big",
            stream=io.BytesIO(oversized_bytes),
        )

    # Verify S3 was not written
    assert len(mock_client.objects) == 0


def test_s3_adapter_invalid_format_and_empty_file():
    """Verifies that empty files and non-PDF headers are rejected before uploading."""
    mock_client = MockS3Client("corp-bucket")
    adapter = S3StorageAdapter(bucket_name="corp-bucket", s3_client=mock_client)

    # Empty file
    with pytest.raises(InvalidFileFormatError):
        adapter.save_upload_stream(
            workspace_id=uuid.uuid4(),
            collection_id="col1",
            document_id="doc_empty",
            stream=io.BytesIO(b""),
        )

    # Non-PDF
    with pytest.raises(InvalidFileFormatError):
        adapter.save_upload_stream(
            workspace_id=uuid.uuid4(),
            collection_id="col1",
            document_id="doc_text",
            stream=io.BytesIO(b"Hello world, not a PDF!"),
        )

    assert len(mock_client.objects) == 0


def test_s3_adapter_materialize_to_local_guarantees_cleanup():
    """Verifies materialize_to_local downloads to temp file and deletes it on context exit."""
    mock_client = MockS3Client("corp-bucket")
    adapter = S3StorageAdapter(bucket_name="corp-bucket", s3_client=mock_client)

    pdf_bytes = create_minimal_pdf_bytes(["Test materialization to local."])
    res = adapter.save_upload_stream(uuid.uuid4(), "col_mat", "doc_mat", io.BytesIO(pdf_bytes))

    materialized_path = None
    with adapter.materialize_to_local(res.source_uri) as local_path:
        materialized_path = local_path
        assert local_path.is_file()
        assert local_path.read_bytes() == pdf_bytes

    # Assert temporary file was removed upon context exit
    assert not materialized_path.exists()

    # Also test cleanup when exception is raised inside context
    materialized_err_path = None
    try:
        with adapter.materialize_to_local(res.source_uri) as local_path:
            materialized_err_path = local_path
            raise RuntimeError("Boom inside context")
    except RuntimeError:
        pass
    assert not materialized_err_path.exists()


def test_s3_adapter_safe_deletion_and_missing_objects():
    """Verifies safe deletion, non-existent object handling, and missing-object exceptions."""
    mock_client = MockS3Client("corp-bucket")
    adapter = S3StorageAdapter(bucket_name="corp-bucket", s3_client=mock_client)

    pdf_bytes = create_minimal_pdf_bytes(["Doc to be deleted."])
    res = adapter.save_upload_stream(uuid.uuid4(), "col_del", "doc_del", io.BytesIO(pdf_bytes))

    assert adapter.exists(res.source_uri) is True
    assert adapter.delete_file(res.source_uri) is True
    assert adapter.exists(res.source_uri) is False

    # Second delete returns False safely
    assert adapter.delete_file(res.source_uri) is False

    # Missing object behavior
    missing_uri = "s3://corp-bucket/uploads/ws/col/missing.pdf"
    assert adapter.exists(missing_uri) is False
    assert adapter.get_metadata(missing_uri) is None

    with pytest.raises(StorageFileNotFoundError):
        with adapter.open_stream(missing_uri):
            pass

    with pytest.raises(StorageFileNotFoundError):
        with adapter.materialize_to_local(missing_uri):
            pass


def test_s3_adapter_path_traversal_rejection():
    """Verifies path traversal in key resolution is strictly rejected."""
    mock_client = MockS3Client("corp-bucket")
    adapter = S3StorageAdapter(bucket_name="corp-bucket", s3_client=mock_client)

    with pytest.raises(PathTraversalError):
        adapter.resolve_key("uploads/../../etc/passwd")

    with pytest.raises(PathTraversalError):
        adapter.resolve_key("upload://ws/../../evil.pdf")

    with pytest.raises(StorageError):
        adapter.save_upload_stream(uuid.uuid4(), "../evil", "doc", io.BytesIO(b"%PDF-1.4"))


def test_s3_adapter_generic_bytes_and_file_transfer(tmp_path):
    """Verifies put_bytes, get_bytes, upload_file, and download_file for snapshot preparation."""
    mock_client = MockS3Client("corp-bucket")
    adapter = S3StorageAdapter(bucket_name="corp-bucket", s3_client=mock_client)

    # 1. put_bytes / get_bytes
    data = b'{"snapshot_id": "snap_001", "chunk_count": 42}'
    key = "indexes/col_1/snapshots/snap_001/manifest.json"
    uri = adapter.put_bytes(key, data, content_type="application/json")
    assert uri == f"s3://corp-bucket/{key}"
    assert adapter.get_bytes(uri) == data

    # 2. upload_file / download_file
    local_src = tmp_path / "chunks.json"
    local_src.write_text('{"chunks": []}', encoding="utf-8")

    file_uri = adapter.upload_file(local_src, "indexes/col_1/snapshots/snap_001/chunks.json")
    assert adapter.exists(file_uri) is True

    local_dst = tmp_path / "downloaded_chunks.json"
    adapter.download_file(file_uri, local_dst)
    assert local_dst.is_file()
    assert local_dst.read_text(encoding="utf-8") == '{"chunks": []}'
