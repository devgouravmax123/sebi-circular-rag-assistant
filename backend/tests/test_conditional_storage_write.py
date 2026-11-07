"""
Unit and integration tests for Milestone 12 Wave 1 - Task 12.1A:
Conditional Storage Write Primitive (BaseStorageAdapter, LocalStorageAdapter, S3StorageAdapter).

Validates:
1. Create-only write succeeds when object is absent.
2. Create-only write fails with StoragePreconditionFailedError when object exists.
3. Replace-only write succeeds when target exists and current validator matches.
4. Replace-only write fails with StoragePreconditionFailedError when validator mismatches.
5. Replace-only write fails with StoragePreconditionFailedError when target is missing.
6. Concurrent competing writers: exactly one conditional operation succeeds.
7. No silent unconditional fallback occurs after a precondition or provider error.
8. Non-precondition provider errors propagate as StorageError without retrying unconditionally.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import io
from pathlib import Path
from typing import Any, Dict, Optional
import pytest
from botocore.exceptions import ClientError

from app.storage.base import (
    BaseStorageAdapter,
    StoragePreconditionFailedError,
    StorageError,
    ConditionalWriteResult,
)
from app.storage.local import LocalStorageAdapter
from app.storage.s3 import S3StorageAdapter


class ConditionalMockS3Client:
    """
    Deterministic in-memory mock of boto3 S3 client implementing
    RFC 7232 / S3 conditional-write semantics (IfNoneMatch and IfMatch).
    """

    def __init__(self, bucket_name: str = "test-bucket"):
        self.bucket_name = bucket_name
        self.objects: Dict[str, dict] = {}
        self.put_object_calls: int = 0
        self.fail_on_keys: Dict[str, Exception] = {}

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

        if Key in self.fail_on_keys:
            raise self.fail_on_keys[Key]

        # 1. Evaluate IfNoneMatch precondition (create-only)
        if kwargs.get("IfNoneMatch") == "*":
            if Key in self.objects:
                raise self._client_error(
                    "PreconditionFailed",
                    "At least one of the pre-conditions you specified did not hold",
                    "PutObject",
                )

        # 2. Evaluate IfMatch precondition (replace-only)
        if "IfMatch" in kwargs:
            if Key not in self.objects:
                raise self._client_error(
                    "PreconditionFailed",
                    "At least one of the pre-conditions you specified did not hold",
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


# ==============================================================================
# LocalStorageAdapter Conditional Write Tests
# ==============================================================================

def test_local_create_only_succeeds_when_absent(tmp_path: Path):
    """Verifies Local create-only write succeeds when target does not exist."""
    adapter = LocalStorageAdapter(root_dir=tmp_path)
    content = b'{"status": "active", "version": 1}'

    res = adapter.put_bytes_conditional("metadata/pointer.json", content, expected_validator=None)
    assert isinstance(res, ConditionalWriteResult)
    assert res.source_uri.startswith("upload://metadata/pointer.json")
    assert res.byte_count == len(content)

    expected_sha = hashlib.sha256(content).hexdigest()
    assert res.validator == expected_sha

    # Verify file content
    read_bytes = adapter.get_bytes("metadata/pointer.json")
    assert read_bytes == content


def test_local_create_only_fails_when_present(tmp_path: Path):
    """Verifies Local create-only write raises StoragePreconditionFailedError when target exists."""
    adapter = LocalStorageAdapter(root_dir=tmp_path)
    initial_content = b'{"status": "initial"}'
    adapter.put_bytes_conditional("metadata/pointer.json", initial_content, expected_validator=None)

    # Attempt create-only on existing object
    new_content = b'{"status": "attempted_overwrite"}'
    with pytest.raises(StoragePreconditionFailedError) as exc_info:
        adapter.put_bytes_conditional("metadata/pointer.json", new_content, expected_validator=None)

    assert "already exists" in str(exc_info.value)
    # Ensure original content was untouched
    assert adapter.get_bytes("metadata/pointer.json") == initial_content


def test_local_replace_only_succeeds_with_matching_validator(tmp_path: Path):
    """Verifies Local replace-only write succeeds when current SHA-256 matches expected validator."""
    adapter = LocalStorageAdapter(root_dir=tmp_path)
    v1_content = b'{"version": 1}'
    res1 = adapter.put_bytes_conditional("state.json", v1_content, expected_validator=None)

    v2_content = b'{"version": 2}'
    res2 = adapter.put_bytes_conditional("state.json", v2_content, expected_validator=res1.validator)
    assert isinstance(res2, ConditionalWriteResult)
    assert res2.byte_count == len(v2_content)
    assert res2.validator == hashlib.sha256(v2_content).hexdigest()

    assert adapter.get_bytes("state.json") == v2_content


def test_local_replace_only_fails_with_mismatched_validator(tmp_path: Path):
    """Verifies Local replace-only write raises StoragePreconditionFailedError on validator mismatch."""
    adapter = LocalStorageAdapter(root_dir=tmp_path)
    v1_content = b'{"version": 1}'
    adapter.put_bytes_conditional("state.json", v1_content, expected_validator=None)

    wrong_validator = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    with pytest.raises(StoragePreconditionFailedError) as exc_info:
        adapter.put_bytes_conditional("state.json", b'{"version": 99}', expected_validator=wrong_validator)

    assert "validator mismatch" in str(exc_info.value)
    # Ensure original content was untouched
    assert adapter.get_bytes("state.json") == v1_content


def test_local_replace_only_fails_when_target_missing(tmp_path: Path):
    """Verifies Local replace-only write raises StoragePreconditionFailedError when target does not exist."""
    adapter = LocalStorageAdapter(root_dir=tmp_path)
    validator = hashlib.sha256(b"dummy").hexdigest()

    with pytest.raises(StoragePreconditionFailedError) as exc_info:
        adapter.put_bytes_conditional("nonexistent.json", b'{"val": 1}', expected_validator=validator)

    assert "does not exist" in str(exc_info.value)


def test_local_concurrent_competing_create_writers(tmp_path: Path):
    """
    Verifies that when multiple threads race to perform a create-only conditional write
    on the exact same key, exactly ONE succeeds and all others fail with StoragePreconditionFailedError.
    """
    adapter = LocalStorageAdapter(root_dir=tmp_path)
    key = "concurrent_create.json"
    results = []
    errors = []

    def writer_task(idx: int):
        data = f'{{"winner": {idx}}}'.encode("utf-8")
        try:
            res = adapter.put_bytes_conditional(key, data, expected_validator=None)
            results.append((idx, res))
        except StoragePreconditionFailedError as e:
            errors.append((idx, e))

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(writer_task, i) for i in range(8)]
        for f in futures:
            f.result()

    assert len(results) == 1, f"Expected exactly 1 winner, got {len(results)}"
    assert len(errors) == 7, f"Expected 7 precondition errors, got {len(errors)}"


def test_local_concurrent_competing_replace_writers(tmp_path: Path):
    """
    Verifies that when multiple threads attempt to replace an object using the same base validator,
    exactly ONE succeeds in updating the object, and all subsequent writers fail with StoragePreconditionFailedError.
    """
    adapter = LocalStorageAdapter(root_dir=tmp_path)
    key = "concurrent_replace.json"
    init_res = adapter.put_bytes_conditional(key, b'{"base": 0}', expected_validator=None)
    base_validator = init_res.validator

    results = []
    errors = []

    def replace_task(idx: int):
        data = f'{{"updated_by": {idx}}}'.encode("utf-8")
        try:
            res = adapter.put_bytes_conditional(key, data, expected_validator=base_validator)
            results.append((idx, res))
        except StoragePreconditionFailedError as e:
            errors.append((idx, e))

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(replace_task, i) for i in range(8)]
        for f in futures:
            f.result()

    assert len(results) == 1, f"Expected exactly 1 replace winner, got {len(results)}"
    assert len(errors) == 7, f"Expected 7 replace errors, got {len(errors)}"


# ==============================================================================
# S3StorageAdapter Conditional Write Tests (Using Deterministic Mock)
# ==============================================================================

def test_s3_create_only_succeeds_when_absent():
    """Verifies S3 create-only write passes IfNoneMatch='*' and succeeds when absent."""
    mock_client = ConditionalMockS3Client("cloud-bucket")
    adapter = S3StorageAdapter(bucket_name="cloud-bucket", s3_client=mock_client)
    content = b'{"active_snapshot": "snap_001"}'

    res = adapter.put_bytes_conditional("indexes/col_1/active.json", content, expected_validator=None)
    assert isinstance(res, ConditionalWriteResult)
    assert res.source_uri == "s3://cloud-bucket/indexes/col_1/active.json"
    assert res.byte_count == len(content)
    assert res.validator.strip('"') == hashlib.md5(content).hexdigest()


def test_s3_create_only_fails_when_present():
    """Verifies S3 create-only write maps 412 PreconditionFailed to StoragePreconditionFailedError."""
    mock_client = ConditionalMockS3Client("cloud-bucket")
    adapter = S3StorageAdapter(bucket_name="cloud-bucket", s3_client=mock_client)
    initial_content = b'{"active_snapshot": "snap_001"}'
    adapter.put_bytes_conditional("indexes/col_1/active.json", initial_content, expected_validator=None)

    # Competing create-only
    with pytest.raises(StoragePreconditionFailedError) as exc_info:
        adapter.put_bytes_conditional("indexes/col_1/active.json", b'{"other": 2}', expected_validator=None)

    assert "precondition failed" in str(exc_info.value).lower()


def test_s3_replace_only_succeeds_with_matching_validator():
    """Verifies S3 replace-only write passes IfMatch and updates target when ETag matches."""
    mock_client = ConditionalMockS3Client("cloud-bucket")
    adapter = S3StorageAdapter(bucket_name="cloud-bucket", s3_client=mock_client)
    v1_content = b'{"active_snapshot": "snap_001"}'
    res1 = adapter.put_bytes_conditional("active.json", v1_content, expected_validator=None)

    v2_content = b'{"active_snapshot": "snap_002"}'
    res2 = adapter.put_bytes_conditional("active.json", v2_content, expected_validator=res1.validator)
    assert isinstance(res2, ConditionalWriteResult)
    assert res2.byte_count == len(v2_content)
    assert res2.validator.strip('"') == hashlib.md5(v2_content).hexdigest()


def test_s3_replace_only_fails_with_mismatched_validator():
    """Verifies S3 replace-only write raises StoragePreconditionFailedError on ETag mismatch."""
    mock_client = ConditionalMockS3Client("cloud-bucket")
    adapter = S3StorageAdapter(bucket_name="cloud-bucket", s3_client=mock_client)
    v1_content = b'{"active_snapshot": "snap_001"}'
    adapter.put_bytes_conditional("active.json", v1_content, expected_validator=None)

    wrong_etag = '"0123456789abcdef0123456789abcdef"'
    with pytest.raises(StoragePreconditionFailedError) as exc_info:
        adapter.put_bytes_conditional("active.json", b'{"active_snapshot": "snap_999"}', expected_validator=wrong_etag)

    assert "precondition failed" in str(exc_info.value).lower()


def test_s3_replace_only_fails_when_target_missing():
    """Verifies S3 replace-only write raises StoragePreconditionFailedError when key does not exist."""
    mock_client = ConditionalMockS3Client("cloud-bucket")
    adapter = S3StorageAdapter(bucket_name="cloud-bucket", s3_client=mock_client)

    with pytest.raises(StoragePreconditionFailedError) as exc_info:
        adapter.put_bytes_conditional("nonexistent.json", b'{"data": 1}', expected_validator='"abc"')

    assert "precondition failed" in str(exc_info.value).lower()


def test_s3_no_unconditional_fallback_after_precondition_error():
    """
    Verifies that when a conditional write fails with PreconditionFailed,
    the adapter NEVER retries or falls back to an unconditional put_object.
    """
    mock_client = ConditionalMockS3Client("cloud-bucket")
    adapter = S3StorageAdapter(bucket_name="cloud-bucket", s3_client=mock_client)
    adapter.put_bytes_conditional("active.json", b"v1", expected_validator=None)
    assert mock_client.put_object_calls == 1

    with pytest.raises(StoragePreconditionFailedError):
        adapter.put_bytes_conditional("active.json", b"v2", expected_validator='"wrong-etag"')

    # Total put_object calls must be exactly 2 (the 1st success + the 1 failed attempt; NO unconditional retry)
    assert mock_client.put_object_calls == 2


def test_s3_provider_error_propagation_without_retry():
    """
    Verifies that non-precondition provider errors (e.g. 500 InternalError) are
    propagated as StorageError and never retried unconditionally.
    """
    mock_client = ConditionalMockS3Client("cloud-bucket")
    mock_client.fail_on_keys["error_key.json"] = ClientError(
        {"Error": {"Code": "InternalError", "Message": "S3 backend failure"}},
        "PutObject",
    )
    adapter = S3StorageAdapter(bucket_name="cloud-bucket", s3_client=mock_client)

    with pytest.raises(StorageError) as exc_info:
        adapter.put_bytes_conditional("error_key.json", b"data", expected_validator=None)

    assert not isinstance(exc_info.value, StoragePreconditionFailedError)
    assert "S3 backend failure" in str(exc_info.value)
    assert mock_client.put_object_calls == 1


def test_empty_string_validator_rejected_for_local_and_s3(tmp_path: Path):
    """
    Verifies that passing an empty string or whitespace string for expected_validator
    is rejected with StoragePreconditionFailedError on both Local and S3 adapters.
    """
    local_adapter = LocalStorageAdapter(root_dir=tmp_path)
    with pytest.raises(StoragePreconditionFailedError) as exc_local:
        local_adapter.put_bytes_conditional("file.txt", b"data", expected_validator="   ")
    assert "Invalid expected_validator" in str(exc_local.value)

    mock_client = ConditionalMockS3Client("cloud-bucket")
    s3_adapter = S3StorageAdapter(bucket_name="cloud-bucket", s3_client=mock_client)
    with pytest.raises(StoragePreconditionFailedError) as exc_s3:
        s3_adapter.put_bytes_conditional("file.txt", b"data", expected_validator="")
    assert "Invalid expected_validator" in str(exc_s3.value)
    assert mock_client.put_object_calls == 0

