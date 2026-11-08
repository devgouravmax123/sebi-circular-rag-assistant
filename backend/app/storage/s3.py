"""
S3 / Cloudflare R2 Durable Storage Adapter (Milestone 11 - Wave A).

Provides durable, tenant-scoped object storage implementing BaseStorageAdapter with:
- AWS S3 & Cloudflare R2 compatibility via boto3.
- Isolated object key partitioning: uploads/{workspace_id}/{collection_id}/{document_id}.pdf
- Single bounded streaming upload pass with magic bytes (%PDF-) validation and SHA-256 calculation.
- Stream reading and temporary-file local materialization with guaranteed cleanup.
- Safe deletion and existence / metadata checks without unhandled exceptions.
- Fail-fast configuration validation with zero silent fallback.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import logging
import os
from pathlib import Path
import tempfile
from typing import BinaryIO, Generator, Optional, Union, Any
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, BotoCoreError

from app.storage.base import (
    BaseStorageAdapter,
    PDF_MAGIC_BYTES,
    DEFAULT_MAX_UPLOAD_BYTES,
    StorageError,
    StorageConfigurationError,
    PathTraversalError,
    StorageLimitExceededError,
    InvalidFileFormatError,
    StorageFileNotFoundError,
    StoragePreconditionFailedError,
    StorageConditionalWriteUnsupportedError,
    StorageSaveResult,
    StorageObjectMetadata,
    ConditionalWriteResult,
)

logger = logging.getLogger(__name__)

__all__ = ["S3StorageAdapter"]


class S3StorageAdapter(BaseStorageAdapter):
    """
    Manages tenant-partitioned documents and snapshot artifacts in an S3-compatible bucket
    (AWS S3 or Cloudflare R2).
    """

    def __init__(
        self,
        bucket_name: Optional[str] = None,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        region_name: str = "auto",
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
        s3_client: Optional[Any] = None,
        provider_type: Optional[str] = None,
    ):
        self.max_upload_bytes = max_upload_bytes
        self.endpoint_url = endpoint_url.strip() if endpoint_url and endpoint_url.strip() else None
        self.region_name = region_name or "auto"
        self.provider_type = (provider_type or "").strip().lower()

        is_b2 = False
        if self.provider_type in ("b2", "backblaze", "backblaze_b2"):
            is_b2 = True
        elif self.endpoint_url and "backblazeb2.com" in self.endpoint_url.lower():
            is_b2 = True
        self.is_b2 = is_b2

        # Validate Backblaze B2 specific requirements
        if is_b2:
            if not self.endpoint_url:
                raise StorageConfigurationError(
                    "Backblaze B2 requires S3_ENDPOINT_URL pointing to the bucket's region S3 endpoint (e.g. 'https://s3.us-east-005.backblazeb2.com')."
                )
            if not self.endpoint_url.startswith(("http://", "https://")):
                self.endpoint_url = f"https://{self.endpoint_url}"

            import re
            b2_match = re.search(r"s3\.([a-zA-Z0-9-]+)\.backblazeb2\.com", self.endpoint_url.lower())
            if b2_match:
                endpoint_region = b2_match.group(1)
                if not self.region_name or self.region_name.strip().lower() == "auto":
                    raise StorageConfigurationError(
                        f"Backblaze B2 requires explicit S3_REGION_NAME matching bucket endpoint region '{endpoint_region}'; 'auto' is rejected."
                    )
                if self.region_name.strip().lower() != endpoint_region.lower():
                    raise StorageConfigurationError(
                        f"S3_REGION_NAME '{self.region_name}' does not match Backblaze B2 endpoint region '{endpoint_region}'."
                    )
            else:
                if not self.region_name or self.region_name.strip().lower() == "auto":
                    raise StorageConfigurationError(
                        "Backblaze B2 requires an explicit S3_REGION_NAME matching the bucket's region; 'auto' is rejected."
                    )

        if s3_client is not None:
            self.client = s3_client
            self.bucket_name = (bucket_name or "test-bucket").strip()
            if hasattr(s3_client, "supports_conditional_writes"):
                self._supports_conditional_writes = s3_client.supports_conditional_writes
            else:
                self._supports_conditional_writes = not is_b2
            return

        self._supports_conditional_writes = not is_b2

        # Validate required credentials
        if not bucket_name or not str(bucket_name).strip():
            raise StorageConfigurationError(
                "Missing required S3 configuration: S3_BUCKET_NAME must be set when STORAGE_PROVIDER=s3."
            )
        if not access_key_id or not str(access_key_id).strip():
            raise StorageConfigurationError(
                "Missing required S3 configuration: S3_ACCESS_KEY_ID must be set when STORAGE_PROVIDER=s3."
            )
        if not secret_access_key or not str(secret_access_key).strip():
            raise StorageConfigurationError(
                "Missing required S3 configuration: S3_SECRET_ACCESS_KEY must be set when STORAGE_PROVIDER=s3."
            )

        self.bucket_name = str(bucket_name).strip()
        client_config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "virtual" if not self.endpoint_url else "path"},
            retries={"max_attempts": 3, "mode": "standard"},
        )
        client_kwargs: dict[str, Any] = {
            "service_name": "s3",
            "aws_access_key_id": str(access_key_id).strip(),
            "aws_secret_access_key": str(secret_access_key).strip(),
            "region_name": self.region_name,
            "config": client_config,
        }
        if self.endpoint_url:
            client_kwargs["endpoint_url"] = self.endpoint_url

        try:
            self.client = boto3.client(**client_kwargs)
        except Exception as e:
            raise StorageConfigurationError(f"Failed to initialize S3 client: {e}") from e

    @property
    def supports_conditional_writes(self) -> bool:
        """Indicates whether this storage adapter natively supports conditional writes."""
        return self._supports_conditional_writes

    def resolve_key(self, source_uri: str) -> str:
        """
        Extracts and normalizes the S3 object key from an upload://, s3://, or relative path URI.
        Rejects path traversal attempts.
        """
        if not source_uri:
            raise StorageError("Empty source URI or storage key.")
        clean = str(source_uri).strip().replace("\\", "/")
        if clean.startswith("upload://"):
            key = f"uploads/{clean[len('upload://'):]}"
        elif clean.startswith("s3://"):
            rel = clean[len("s3://") :]
            parts = rel.split("/", 1)
            if parts[0] == self.bucket_name and len(parts) > 1:
                key = parts[1]
            else:
                key = rel
        else:
            key = clean.lstrip("/")

        # Check for path traversal elements
        parts = [p for p in key.split("/") if p]
        if any(p == ".." or p == "." for p in parts):
            raise PathTraversalError(f"Path traversal detected in key '{key}'.")
        return key

    def save_upload_stream(
        self,
        workspace_id: Union[uuid.UUID, str],
        collection_id: str,
        document_id: str,
        stream: BinaryIO,
        original_filename: Optional[str] = None,
    ) -> StorageSaveResult:
        """
        Incrementally streams data to an isolated local temp file, validating magic bytes
        and size limit, computes SHA-256, and uploads to S3 under the partitioned key.
        Guarantees temp file cleanup.
        """
        ws_str = self.sanitize_identifier(str(workspace_id), "workspace_id")
        col_str = self.sanitize_identifier(collection_id, "collection_id")
        doc_str = self.sanitize_identifier(document_id, "document_id")
        key = f"uploads/{ws_str}/{col_str}/{doc_str}.pdf"

        temp_file = tempfile.NamedTemporaryFile(prefix=f"s3_up_{doc_str}_", suffix=".pdf", delete=False)
        temp_path = Path(temp_file.name)
        temp_file.close()
        hasher = hashlib.sha256()
        bytes_written = 0
        header_checked = False

        try:
            with open(temp_path, "wb") as f_out:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    bytes_written += len(chunk)
                    if bytes_written > self.max_upload_bytes:
                        raise StorageLimitExceededError(
                            f"File size exceeds maximum allowed limit of {self.max_upload_bytes} bytes "
                            f"({self.max_upload_bytes // (1024 * 1024)} MiB)."
                        )
                    if not header_checked:
                        if len(chunk) < len(PDF_MAGIC_BYTES) or not chunk.startswith(PDF_MAGIC_BYTES):
                            raise InvalidFileFormatError(
                                "Invalid file signature: file does not begin with '%PDF-'. Only genuine PDF documents are supported."
                            )
                        header_checked = True
                    hasher.update(chunk)
                    f_out.write(chunk)

            if bytes_written == 0:
                raise InvalidFileFormatError("Uploaded file is empty (0 bytes).")

            sha256 = hasher.hexdigest()

            # Upload to S3 with metadata
            with open(temp_path, "rb") as f_in:
                self.client.put_object(
                    Bucket=self.bucket_name,
                    Key=key,
                    Body=f_in,
                    ContentType="application/pdf",
                    Metadata={
                        "sha256": sha256,
                        "original-filename": original_filename or f"{doc_str}.pdf",
                    },
                )

            canonical_uri = f"s3://{self.bucket_name}/{key}"
            logger.info(f"Persisted S3 document {canonical_uri} ({bytes_written} bytes, sha256={sha256[:12]}...)")
            return StorageSaveResult(
                storage_path=key,
                sha256=sha256,
                byte_count=bytes_written,
                source_uri=canonical_uri,
            )
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception as e:
                    logger.warning(f"Failed to cleanup temp upload file {temp_path}: {e}")

    @contextmanager
    def open_stream(self, source_uri: str) -> Generator[BinaryIO, None, None]:
        """
        Yields a readable binary stream from S3 for the specified URI.
        Guarantees that the stream is closed when the context manager exits.
        Raises StorageFileNotFoundError if the object is missing.
        """
        key = self.resolve_key(source_uri)
        try:
            res = self.client.get_object(Bucket=self.bucket_name, Key=key)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404", "NoSuchBucket"):
                raise StorageFileNotFoundError(f"Storage object not found: {source_uri}") from e
            raise StorageError(f"S3 error opening stream for {source_uri}: {e}") from e
        except Exception as e:
            raise StorageError(f"Error opening stream for {source_uri}: {e}") from e

        body = res["Body"]
        try:
            yield body
        finally:
            try:
                body.close()
            except Exception:
                pass

    @contextmanager
    def materialize_to_local(self, source_uri: str) -> Generator[Path, None, None]:
        """
        Downloads the remote object from S3 to an isolated local temporary file
        and yields its Path. Guarantees temporary file removal upon context exit.
        Raises StorageFileNotFoundError if the object is missing.
        """
        key = self.resolve_key(source_uri)
        temp_file = tempfile.NamedTemporaryFile(prefix="s3_mat_", suffix=".pdf", delete=False)
        temp_path = Path(temp_file.name)
        temp_file.close()

        try:
            try:
                self.client.download_file(self.bucket_name, key, str(temp_path))
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code in ("NoSuchKey", "404", "NoSuchBucket"):
                    raise StorageFileNotFoundError(f"Storage object not found: {source_uri}") from e
                raise StorageError(f"S3 error downloading {source_uri}: {e}") from e
            except Exception as e:
                raise StorageError(f"Error downloading {source_uri}: {e}") from e

            yield temp_path
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception as e:
                    logger.warning(f"Failed to cleanup materialized file {temp_path}: {e}")

    def delete_file(self, file_path_or_uri: Union[str, Path]) -> bool:
        """
        Safely deletes an object from S3.
        Returns True if deleted, False if object did not exist or error occurred.
        Never raises unhandled exceptions.
        """
        if not file_path_or_uri:
            return False
        try:
            key = self.resolve_key(str(file_path_or_uri))
            if not self.exists(key):
                return False
            self.client.delete_object(Bucket=self.bucket_name, Key=key)
            logger.info(f"Deleted S3 object '{key}'")
            return True
        except Exception as e:
            logger.warning(f"Failed to delete S3 object '{file_path_or_uri}': {e}")
            return False

    def delete_prefix(self, prefix: str) -> int:
        """
        Safely deletes all objects in the bucket whose key matches the given prefix.
        Handles pagination and batch deletes (up to 1000 per request).
        Returns total count of deleted objects.
        """
        if not prefix:
            return 0
        clean_prefix = str(prefix).strip().replace("\\", "/")
        if clean_prefix.startswith("upload://"):
            clean_prefix = f"uploads/{clean_prefix[len('upload://'):]}"
        elif clean_prefix.startswith("s3://"):
            parts = clean_prefix[len("s3://"):].split("/", 1)
            clean_prefix = parts[1] if len(parts) > 1 else ""
        clean_prefix = clean_prefix.lstrip("/")

        paginator = self.client.get_paginator("list_objects_v2")
        deleted_count = 0
        try:
            for page in paginator.paginate(Bucket=self.bucket_name, Prefix=clean_prefix):
                items = page.get("Contents", [])
                if not items:
                    continue
                delete_keys = [{"Key": item["Key"]} for item in items]
                for i in range(0, len(delete_keys), 1000):
                    batch = delete_keys[i : i + 1000]
                    self.client.delete_objects(
                        Bucket=self.bucket_name,
                        Delete={"Objects": batch, "Quiet": True},
                    )
                    deleted_count += len(batch)
            logger.info(f"Deleted {deleted_count} S3 object(s) with prefix '{clean_prefix}'")
            return deleted_count
        except Exception as e:
            logger.warning(f"Error deleting S3 objects with prefix '{clean_prefix}': {e}")
            raise

    def exists(self, source_uri: str) -> bool:
        """Returns True if object exists in S3, False otherwise."""
        try:
            key = self.resolve_key(source_uri)
            self.client.head_object(Bucket=self.bucket_name, Key=key)
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NoSuchBucket"):
                return False
            logger.warning(f"S3 head_object error for {source_uri}: {e}")
            return False
        except Exception:
            return False

    def get_metadata(self, source_uri: str) -> Optional[StorageObjectMetadata]:
        """Retrieves metadata from S3 object headers."""
        try:
            key = self.resolve_key(source_uri)
            res = self.client.head_object(Bucket=self.bucket_name, Key=key)
            meta = res.get("Metadata", {})
            return StorageObjectMetadata(
                source_uri=source_uri,
                byte_count=res.get("ContentLength", 0),
                sha256=meta.get("sha256"),
                etag=res.get("ETag"),
                content_type=res.get("ContentType", "application/pdf"),
                last_modified=res.get("LastModified"),
            )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NoSuchBucket"):
                return None
            logger.warning(f"S3 get_metadata error for {source_uri}: {e}")
            return None
        except Exception:
            return None

    def put_bytes(self, storage_key_or_uri: str, data: bytes, content_type: Optional[str] = None) -> str:
        """Persists raw bytes to S3 object."""
        key = self.resolve_key(storage_key_or_uri)
        extra: dict[str, Any] = {}
        if content_type:
            extra["ContentType"] = content_type
        self.client.put_object(Bucket=self.bucket_name, Key=key, Body=data, **extra)
        return f"s3://{self.bucket_name}/{key}"

    def put_bytes_conditional(
        self,
        storage_key_or_uri: str,
        data: bytes,
        expected_validator: Optional[str],
        content_type: Optional[str] = None,
    ) -> ConditionalWriteResult:
        """
        Conditionally persists raw bytes to S3-compatible object storage.

        Validator contract:
        - The validator is the S3 object ETag (entity tag) string.
        - Create-only write: When expected_validator is None, uses IfNoneMatch="*" to require
          that the target key does not exist. Raises StoragePreconditionFailedError if target exists.
        - Replace-only write: When expected_validator is a non-empty string, uses IfMatch='<etag>'
          to require that the target key exists and its ETag matches. Raises StoragePreconditionFailedError
          if target is missing or ETag mismatches.
        - Propagates provider errors; never silently falls back to an unconditional write.
        """
        if not self.supports_conditional_writes:
            raise StorageConditionalWriteUnsupportedError(
                f"S3 provider at endpoint '{self.endpoint_url}' does not support conditional PutObject (Backblaze B2)."
            )

        if expected_validator is not None and not str(expected_validator).strip():
            raise StoragePreconditionFailedError(
                f"Invalid expected_validator '{expected_validator}': must be None for create-only or a non-empty string for replace-only."
            )

        key = self.resolve_key(storage_key_or_uri)
        extra: dict[str, Any] = {}
        if content_type:
            extra["ContentType"] = content_type

        if expected_validator is None:
            extra["IfNoneMatch"] = "*"
        else:
            raw_etag = expected_validator.strip()
            quoted_etag = raw_etag if (raw_etag.startswith('"') and raw_etag.endswith('"')) else f'"{raw_etag}"'
            extra["IfMatch"] = quoted_etag

        try:
            res = self.client.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=data,
                **extra,
            )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            status_code = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in ("NotImplemented", "501") or status_code == 501:
                raise StorageConditionalWriteUnsupportedError(
                    f"S3 provider at endpoint '{self.endpoint_url}' returned 501 Not Implemented for conditional PutObject: {e}"
                ) from e
            if code in ("PreconditionFailed", "412") or status_code == 412:
                mode_desc = "create-only (target exists)" if expected_validator is None else f"replace-only (expected ETag {expected_validator})"
                raise StoragePreconditionFailedError(
                    f"S3 conditional write precondition failed for key '{key}' ({mode_desc}): {e}"
                ) from e
            raise StorageError(f"S3 error during conditional write for key '{key}': {e}") from e
        except Exception as e:
            raise StorageError(f"Unexpected error during S3 conditional write for key '{key}': {e}") from e

        new_etag = res.get("ETag", "")
        return ConditionalWriteResult(
            source_uri=f"s3://{self.bucket_name}/{key}",
            validator=new_etag,
            byte_count=len(data),
        )

    def upload_file(self, local_path: Union[str, Path], storage_key_or_uri: str, content_type: Optional[str] = None) -> str:
        """Uploads a local file to S3."""
        src = Path(local_path).resolve()
        if not src.is_file():
            raise StorageFileNotFoundError(f"Local file not found: {local_path}")
        key = self.resolve_key(storage_key_or_uri)
        extra_args: dict[str, Any] = {}
        if content_type:
            extra_args["ContentType"] = content_type
        self.client.upload_file(
            str(src),
            self.bucket_name,
            key,
            ExtraArgs=extra_args if extra_args else None,
        )
        return f"s3://{self.bucket_name}/{key}"

    def download_file(self, storage_key_or_uri: str, target_local_path: Union[str, Path]) -> Path:
        """Downloads an S3 object to a local destination path."""
        key = self.resolve_key(storage_key_or_uri)
        dest = Path(target_local_path).resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.client.download_file(self.bucket_name, key, str(dest))
            return dest
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404", "NoSuchBucket"):
                raise StorageFileNotFoundError(f"Storage object not found: {storage_key_or_uri}") from e
            raise StorageError(f"S3 error downloading {storage_key_or_uri}: {e}") from e

