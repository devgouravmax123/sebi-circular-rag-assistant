"""
Provider-neutral Storage Abstraction for Private Tenant Documents (Milestone 11 - Phase 1).

Defines the core interface and models for durable, tenant-partitioned document storage.
Supports local development storage and provides the contract for future S3/Cloudflare R2 providers.
"""
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import datetime
import logging
from pathlib import Path
import re
from typing import BinaryIO, Generator, Optional, Union
import uuid

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

PDF_MAGIC_BYTES: bytes = b"%PDF-"
DEFAULT_MAX_UPLOAD_BYTES: int = 30 * 1024 * 1024  # 30 MiB


class StorageError(Exception):
    """Base exception for all storage operations."""
    pass


class StorageConfigurationError(StorageError):
    """Raised when storage configuration is invalid or missing required credentials."""
    pass


class PathTraversalError(StorageError):
    """Raised when an operation attempts to access paths or keys outside the designated root."""
    pass


class StorageLimitExceededError(StorageError):
    """Raised when an uploaded file exceeds the configured size limit."""
    pass


class InvalidFileFormatError(StorageError):
    """Raised when an uploaded file violates PDF format or magic bytes."""
    pass


class StorageFileNotFoundError(StorageError):
    """Raised when a requested storage object is not found."""
    pass


class StoragePreconditionFailedError(StorageError):
    """Raised when a conditional write fails due to missing target, existing target, or validator mismatch."""
    pass


class StorageConditionalWriteUnsupportedError(StorageError):
    """Raised when the underlying storage provider does not support HTTP conditional writes (e.g. Backblaze B2)."""
    pass


class StorageSaveResult(BaseModel):
    """Structured result returned after successfully persisting an uploaded source file."""
    storage_path: str = Field(..., description="Provider-relative storage path or key")
    sha256: str = Field(..., description="Cryptographic SHA-256 digest of stored bytes")
    byte_count: int = Field(..., description="Total bytes persisted")
    source_uri: str = Field(..., description="Opaque canonical URI for application-level resolution")


class ConditionalWriteResult(BaseModel):
    """Structured result returned after successfully performing a conditional write."""
    source_uri: str = Field(..., description="Canonical URI of the stored object")
    validator: str = Field(
        ...,
        description=(
            "Opaque validator representing the state of the written object. "
            "For LocalStorageAdapter, this is the hex SHA-256 digest. "
            "For S3StorageAdapter, this is the provider ETag string."
        ),
    )
    byte_count: int = Field(..., description="Total bytes persisted")


class StorageObjectMetadata(BaseModel):
    """Metadata describing a stored object."""
    source_uri: str = Field(..., description="Canonical URI of the object")
    byte_count: int = Field(..., description="Object size in bytes")
    sha256: Optional[str] = Field(None, description="SHA-256 checksum if available")
    etag: Optional[str] = Field(None, description="HTTP/S3 ETag if available")
    content_type: str = Field("application/pdf", description="MIME content type")
    last_modified: Optional[datetime] = Field(None, description="Timestamp when object was last modified")


class BaseStorageAdapter(ABC):
    """
    Abstract base interface for tenant-scoped document storage providers.
    All operations enforce tenant isolation and provide bounded, stream-safe I/O.
    """

    @abstractmethod
    def save_upload_stream(
        self,
        workspace_id: Union[uuid.UUID, str],
        collection_id: str,
        document_id: str,
        stream: BinaryIO,
        original_filename: Optional[str] = None,
    ) -> StorageSaveResult:
        """
        Saves an uploaded binary stream to tenant-scoped storage.
        Enforces size limits, magic byte validation, and calculates SHA-256 in a single bounded pass.
        Returns StorageSaveResult containing the canonical source URI and checksum.
        """
        pass

    @abstractmethod
    @contextmanager
    def open_stream(self, source_uri: str) -> Generator[BinaryIO, None, None]:
        """
        Opens a readable binary stream for the specified source URI.
        Guarantees that the stream is closed when the context manager exits.
        Raises StorageFileNotFoundError if the object does not exist.
        """
        pass

    @abstractmethod
    @contextmanager
    def materialize_to_local(self, source_uri: str) -> Generator[Path, None, None]:
        """
        Provides a local filesystem Path for tools (such as PyMuPDF) that require
        a file path. If the provider is remote, downloads to a temporary file and guarantees
        its deletion on context exit.
        Raises StorageFileNotFoundError if the object does not exist.
        """
        pass

    @abstractmethod
    def delete_file(self, file_path_or_uri: Union[str, Path]) -> bool:
        """
        Safely deletes a stored file or object identified by URI or path.
        Returns True if deleted, False if the object did not exist or could not be removed.
        Never raises unhandled errors to avoid masking primary application faults.
        """
        pass

    @abstractmethod
    def exists(self, source_uri: str) -> bool:
        """
        Returns True if the object identified by source_uri exists, False otherwise.
        """
        pass

    @abstractmethod
    def get_metadata(self, source_uri: str) -> Optional[StorageObjectMetadata]:
        """
        Retrieves metadata (size, content type, modification timestamp) for the object.
        Returns None if the object does not exist.
        """
        pass

    @property
    def supports_conditional_writes(self) -> bool:
        """
        Indicates whether this storage adapter natively supports HTTP / filesystem conditional writes.
        Defaults to True. Storage adapters for providers lacking conditional PutObject (e.g. Backblaze B2)
        override this to False.
        """
        return True

    def put_bytes(self, storage_key_or_uri: str, data: bytes, content_type: Optional[str] = None) -> str:
        """Persists raw bytes to storage key or URI. Returns canonical URI."""
        raise NotImplementedError

    @abstractmethod
    def put_bytes_conditional(
        self,
        storage_key_or_uri: str,
        data: bytes,
        expected_validator: Optional[str],
        content_type: Optional[str] = None,
    ) -> ConditionalWriteResult:
        """
        Conditionally persists raw bytes to storage.

        Explicit Semantics:
        - Create-only write: When expected_validator is None, the write succeeds ONLY if the target
          object does NOT currently exist. If it already exists, raises StoragePreconditionFailedError.
        - Replace-only write: When expected_validator is a non-empty string, the write succeeds ONLY if
          the target object currently exists AND its current validator matches expected_validator.
          If target is missing or validator mismatches, raises StoragePreconditionFailedError.
        - Never silently falls back to an unconditional write.

        Returns:
            ConditionalWriteResult containing canonical source URI, new validator, and byte count.
            The validator is an opaque token: SHA-256 hex string for local storage, ETag string for S3.
        """
        pass

    def get_bytes(self, storage_key_or_uri: str) -> bytes:
        """Retrieves raw bytes from storage key or URI."""
        with self.open_stream(storage_key_or_uri) as stream:
            return stream.read()

    def upload_file(self, local_path: Union[str, Path], storage_key_or_uri: str, content_type: Optional[str] = None) -> str:
        """Uploads a local file to storage key or URI. Returns canonical URI."""
        raise NotImplementedError

    def download_file(self, storage_key_or_uri: str, target_local_path: Union[str, Path]) -> Path:
        """Downloads a stored object to a designated local path."""
        dest = Path(target_local_path).resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.open_stream(storage_key_or_uri) as src, open(dest, "wb") as dst:
            while True:
                buf = src.read(65536)
                if not buf:
                    break
                dst.write(buf)
        return dest

    @staticmethod
    def sanitize_identifier(value: str, name: str = "identifier") -> str:
        """Validates that identifiers contain only safe alphanumeric, hyphen, and underscore characters."""
        val = str(value).strip()
        if not val or not re.match(r"^[a-zA-Z0-9_-]+$", val):
            raise StorageError(f"Invalid {name} '{val}': must contain only alphanumeric characters, underscores, and hyphens.")
        return val

