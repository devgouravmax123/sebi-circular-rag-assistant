"""
Unit and integration tests for Milestone 11 - Phase 1:
Provider-neutral Storage Interface and LocalStorageAdapter conformance.

Validates:
1. BaseStorageAdapter interface inheritance and contract fulfillment.
2. Bounded streaming upload, size limits, and magic byte validation.
3. Streaming SHA-256 calculation accuracy.
4. Tenant/collection key partitioning and isolation.
5. Strict path/key traversal prevention.
6. Safe deletion without masked errors.
7. Missing-object handling (StorageFileNotFoundError, None metadata).
8. Temporary-file cleanup on abort/failure.
9. Factory provider selection and rejection of unsupported providers without silent fallback.
"""
from datetime import datetime, timezone
import hashlib
import io
from pathlib import Path
import uuid
import fitz
import pytest

from app.core.config import Settings
from app.storage import (
    BaseStorageAdapter,
    LocalStorageAdapter,
    get_storage_adapter,
    StorageSaveResult,
    StorageObjectMetadata,
    StorageError,
    StorageConfigurationError,
    PathTraversalError,
    StorageLimitExceededError,
    InvalidFileFormatError,
    StorageFileNotFoundError,
)


def create_minimal_pdf_bytes(text_lines: list[str]) -> bytes:
    """Helper to generate valid in-memory PDF bytes."""
    doc = fitz.open()
    page = doc.new_page()
    y = 50
    for line in text_lines:
        page.insert_text((50, y), line)
        y += 20
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def test_1_local_storage_adapter_conforms_to_interface(tmp_path):
    """Verifies that LocalStorageAdapter inherits from BaseStorageAdapter and implements all abstract methods."""
    adapter = LocalStorageAdapter(root_dir=tmp_path)
    assert isinstance(adapter, BaseStorageAdapter)

    required_methods = [
        "save_upload_stream",
        "open_stream",
        "materialize_to_local",
        "delete_file",
        "exists",
        "get_metadata",
    ]
    for method_name in required_methods:
        assert hasattr(adapter, method_name)
        assert callable(getattr(adapter, method_name))


def test_2_upload_stream_valid_pdf_and_metadata(tmp_path):
    """Tests successful streaming upload, URI generation, existence check, metadata, and stream reading."""
    adapter = LocalStorageAdapter(root_dir=tmp_path)
    ws_id = uuid.uuid4()
    col_id = f"col_{uuid.uuid4().hex[:8]}"
    doc_id = f"doc_{uuid.uuid4().hex[:8]}"

    pdf_bytes = create_minimal_pdf_bytes(["Regulatory Policy Section 1: Data Encryption Standards."])
    expected_sha = hashlib.sha256(pdf_bytes).hexdigest()

    save_res = adapter.save_upload_stream(
        workspace_id=ws_id,
        collection_id=col_id,
        document_id=doc_id,
        stream=io.BytesIO(pdf_bytes),
        original_filename="policy.pdf",
    )

    assert isinstance(save_res, StorageSaveResult)
    assert save_res.byte_count == len(pdf_bytes)
    assert save_res.sha256 == expected_sha
    assert save_res.source_uri == f"upload://{ws_id}/{col_id}/{doc_id}.pdf"

    # Check existence
    assert adapter.exists(save_res.source_uri) is True

    # Check metadata
    meta = adapter.get_metadata(save_res.source_uri)
    assert isinstance(meta, StorageObjectMetadata)
    assert meta.source_uri == save_res.source_uri
    assert meta.byte_count == len(pdf_bytes)
    assert meta.content_type == "application/pdf"
    assert meta.last_modified is not None

    # Check open_stream
    with adapter.open_stream(save_res.source_uri) as stream:
        read_bytes = stream.read()
        assert read_bytes == pdf_bytes

    # Check materialize_to_local
    with adapter.materialize_to_local(save_res.source_uri) as local_path:
        assert isinstance(local_path, Path)
        assert local_path.is_file()
        assert local_path.read_bytes() == pdf_bytes


def test_3_upload_size_limit_enforced_and_temp_file_cleaned(tmp_path):
    """Verifies that exceeding max_upload_bytes raises StorageLimitExceededError and removes temp files."""
    max_bytes = 200
    adapter = LocalStorageAdapter(root_dir=tmp_path, max_upload_bytes=max_bytes)
    ws_id = uuid.uuid4()
    col_id = "col_limit_test"
    doc_id = "doc_oversized"

    # Create oversized PDF payload (> 200 bytes)
    oversized_bytes = b"%PDF-1.4\n" + b"X" * 1000

    with pytest.raises(StorageLimitExceededError) as exc_info:
        adapter.save_upload_stream(
            workspace_id=ws_id,
            collection_id=col_id,
            document_id=doc_id,
            stream=io.BytesIO(oversized_bytes),
        )
    assert "exceeds maximum allowed limit" in str(exc_info.value)

    # Verify no final file or temp file was retained
    col_dir = tmp_path / str(ws_id) / col_id
    if col_dir.exists():
        retained_files = list(col_dir.iterdir())
        assert len(retained_files) == 0, f"Leaked files found: {retained_files}"


def test_4_invalid_format_and_empty_file_rejected(tmp_path):
    """Verifies rejection of empty files and non-PDF files missing %PDF- header."""
    adapter = LocalStorageAdapter(root_dir=tmp_path)
    ws_id = uuid.uuid4()
    col_id = "col_format_test"

    # 1. Empty file
    with pytest.raises(InvalidFileFormatError) as exc_info:
        adapter.save_upload_stream(
            workspace_id=ws_id,
            collection_id=col_id,
            document_id="doc_empty",
            stream=io.BytesIO(b""),
        )
    assert "empty" in str(exc_info.value).lower()

    # 2. Non-PDF magic bytes
    with pytest.raises(InvalidFileFormatError) as exc_info:
        adapter.save_upload_stream(
            workspace_id=ws_id,
            collection_id=col_id,
            document_id="doc_text",
            stream=io.BytesIO(b"This is a plain text file, not a PDF document."),
        )
    assert "file does not begin with '%PDF-'" in str(exc_info.value)

    # Verify clean state
    col_dir = tmp_path / str(ws_id) / col_id
    if col_dir.exists():
        assert len(list(col_dir.iterdir())) == 0


def test_5_sha256_cryptographic_integrity(tmp_path):
    """Verifies that the computed SHA-256 matches an exact independent cryptographic digest."""
    adapter = LocalStorageAdapter(root_dir=tmp_path)
    ws_id = uuid.uuid4()
    col_id = "col_sha_test"
    doc_id = "doc_sha_1"

    content = create_minimal_pdf_bytes(["Cryptographic Verification Clause: 256-bit hash validation."])
    expected_digest = hashlib.sha256(content).hexdigest()

    save_res = adapter.save_upload_stream(
        workspace_id=ws_id,
        collection_id=col_id,
        document_id=doc_id,
        stream=io.BytesIO(content),
    )
    assert save_res.sha256 == expected_digest


def test_6_tenant_and_collection_isolation(tmp_path):
    """Verifies that storage keys and directories are strictly partitioned by workspace and collection."""
    adapter = LocalStorageAdapter(root_dir=tmp_path)
    ws1 = uuid.uuid4()
    ws2 = uuid.uuid4()
    col_a = "col_alpha"
    col_b = "col_beta"
    doc_id = "shared_doc_name"

    pdf_bytes = create_minimal_pdf_bytes(["Isolation test content."])

    res1 = adapter.save_upload_stream(ws1, col_a, doc_id, io.BytesIO(pdf_bytes))
    res2 = adapter.save_upload_stream(ws1, col_b, doc_id, io.BytesIO(pdf_bytes))
    res3 = adapter.save_upload_stream(ws2, col_a, doc_id, io.BytesIO(pdf_bytes))

    # All three must have different URIs and storage paths
    assert res1.source_uri != res2.source_uri
    assert res1.source_uri != res3.source_uri
    assert res2.source_uri != res3.source_uri

    # Deleting one must not affect the others
    assert adapter.delete_file(res1.source_uri) is True
    assert adapter.exists(res1.source_uri) is False
    assert adapter.exists(res2.source_uri) is True
    assert adapter.exists(res3.source_uri) is True


def test_7_path_traversal_rejection(tmp_path):
    """Verifies rejection of path traversal sequences in identifiers and paths."""
    adapter = LocalStorageAdapter(root_dir=tmp_path)
    ws_id = uuid.uuid4()
    pdf_bytes = create_minimal_pdf_bytes(["Test."])

    # Traversal in collection_id
    with pytest.raises(StorageError):
        adapter.save_upload_stream(ws_id, "../evil_col", "doc1", io.BytesIO(pdf_bytes))

    # Traversal in document_id
    with pytest.raises(StorageError):
        adapter.save_upload_stream(ws_id, "col_valid", "../evil_doc", io.BytesIO(pdf_bytes))

    # Path traversal in resolve_source_file
    assert adapter.resolve_source_file(f"upload://{ws_id}/../../etc/passwd") is None
    assert adapter.resolve_source_file(str(tmp_path.parent / "escape.txt")) is None


def test_8_safe_deletion_and_missing_object_handling(tmp_path):
    """Verifies safe deletion, non-existent object deletion returning False, and file-not-found exceptions."""
    adapter = LocalStorageAdapter(root_dir=tmp_path)
    ws_id = uuid.uuid4()
    col_id = "col_del_test"
    doc_id = "doc_del_1"

    pdf_bytes = create_minimal_pdf_bytes(["Temporary document for deletion test."])
    save_res = adapter.save_upload_stream(ws_id, col_id, doc_id, io.BytesIO(pdf_bytes))

    assert adapter.exists(save_res.source_uri) is True
    assert adapter.delete_file(save_res.source_uri) is True
    assert adapter.exists(save_res.source_uri) is False

    # Second delete on already-removed file returns False safely without error
    assert adapter.delete_file(save_res.source_uri) is False

    # Non-existent URI operations
    missing_uri = f"upload://{ws_id}/{col_id}/non_existent.pdf"
    assert adapter.exists(missing_uri) is False
    assert adapter.get_metadata(missing_uri) is None

    with pytest.raises(StorageFileNotFoundError):
        with adapter.open_stream(missing_uri):
            pass

    with pytest.raises(StorageFileNotFoundError):
        with adapter.materialize_to_local(missing_uri):
            pass


def test_9_factory_provider_selection_and_no_silent_fallback(tmp_path):
    """Verifies that get_storage_adapter returns LocalStorageAdapter for 'local' and fails on unsupported providers."""
    # 1. Default / local selection
    settings_local = Settings(storage_provider="local", uploads_dir=str(tmp_path))
    adapter = get_storage_adapter(settings_local)
    assert isinstance(adapter, LocalStorageAdapter)

    # 2. Case-insensitive local
    settings_caps = Settings(storage_provider="LOCAL", uploads_dir=str(tmp_path))
    adapter_caps = get_storage_adapter(settings_caps)
    assert isinstance(adapter_caps, LocalStorageAdapter)

    # 3. Invalid / unsupported provider fails explicitly without silent fallback
    settings_invalid = Settings(storage_provider="unsupported_cloud_provider", uploads_dir=str(tmp_path))
    with pytest.raises(StorageConfigurationError) as exc_info:
        get_storage_adapter(settings_invalid)
    assert "Unsupported storage provider 'unsupported_cloud_provider'" in str(exc_info.value)

    # 4. S3 provider fails without required credentials
    settings_s3_missing = Settings(storage_provider="s3", uploads_dir=str(tmp_path))
    with pytest.raises(StorageConfigurationError) as exc_s3:
        get_storage_adapter(settings_s3_missing)
    assert "S3_BUCKET_NAME" in str(exc_s3.value)

    # 5. S3 provider succeeds when configured
    from app.storage import S3StorageAdapter
    settings_s3_valid = Settings(
        storage_provider="s3",
        s3_bucket_name="my-bucket",
        s3_access_key_id="test_key",
        s3_secret_access_key="test_secret",
        uploads_dir=str(tmp_path),
    )
    adapter_s3 = get_storage_adapter(settings_s3_valid)
    assert isinstance(adapter_s3, S3StorageAdapter)
    assert adapter_s3.bucket_name == "my-bucket"

