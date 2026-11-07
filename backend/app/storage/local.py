"""
Local Development Storage Adapter for Private Document Uploads (Milestone 10 & 11).

Provides bounded, tenant-scoped local file storage implementing BaseStorageAdapter with:
- Strict path traversal prevention via relative_to containment checks.
- Bounded file size streaming (default max 30 MiB).
- Magic byte validation (%PDF-).
- Streaming cryptographic SHA-256 calculation.
- Isolated temporary write with atomic replacement.
- Guaranteed cleanup of temporary files on any failure path.

Note: Local disk storage is intended for development and local testing.
Production deployment requires a durable private cloud storage provider
(e.g., Cloudflare R2 / S3-compatible private object storage).
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import logging
import os
from pathlib import Path
import time
from typing import BinaryIO, Generator, Optional, Union
import uuid

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
    StorageSaveResult,
    StorageObjectMetadata,
    ConditionalWriteResult,
)

logger = logging.getLogger(__name__)

__all__ = [
    "LocalStorageAdapter",
    "BaseStorageAdapter",
    "StorageSaveResult",
    "StorageObjectMetadata",
    "ConditionalWriteResult",
    "StorageError",
    "StorageConfigurationError",
    "StorageLimitExceededError",
    "InvalidFileFormatError",
    "StorageFileNotFoundError",
    "StoragePreconditionFailedError",
    "PathTraversalError",
    "PDF_MAGIC_BYTES",
    "DEFAULT_MAX_UPLOAD_BYTES",
]


class LocalStorageAdapter(BaseStorageAdapter):
    """
    Manages tenant-partitioned source PDF files under a dedicated storage root.
    Path layout:
      {root_dir}/{workspace_id}/{collection_id}/{document_id}.pdf
    """

    def __init__(
        self,
        root_dir: Union[str, Path] = "data/uploads",
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    ):
        self.root_dir = Path(root_dir).resolve()
        self.max_upload_bytes = max_upload_bytes
        self.root_dir.mkdir(parents=True, exist_ok=True)
        if os.getenv("RENDER") or os.getenv("ENVIRONMENT") == "production":
            logger.warning(
                f"LocalStorageAdapter initialized at '{self.root_dir}'. "
                "Notice: Local disk storage is ephemeral on free-tier container hosts (e.g. Render Free). "
                "Production persistence requires a mounted persistent disk volume or a private cloud object storage adapter."
            )

    def _validate_path_containment(self, target_path: Path) -> Path:
        """Enforces that target_path is strictly within root_dir."""
        resolved = target_path.resolve()
        try:
            resolved.relative_to(self.root_dir)
        except ValueError:
            raise PathTraversalError(
                f"Path traversal detected: path '{target_path}' is outside storage root '{self.root_dir}'."
            )
        return resolved

    def _sanitize_identifier(self, value: str, name: str = "identifier") -> str:
        """Compatibility wrapper delegates to BaseStorageAdapter.sanitize_identifier."""
        return self.sanitize_identifier(value, name)

    def get_collection_dir(self, workspace_id: Union[uuid.UUID, str], collection_id: str) -> Path:
        """Returns the isolated directory for a specific workspace collection."""
        ws_str = self.sanitize_identifier(str(workspace_id), "workspace_id")
        col_str = self.sanitize_identifier(collection_id, "collection_id")
        target_dir = self.root_dir / ws_str / col_str
        self._validate_path_containment(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir

    def save_upload_stream(
        self,
        workspace_id: Union[uuid.UUID, str],
        collection_id: str,
        document_id: str,
        stream: BinaryIO,
        original_filename: Optional[str] = None,
    ) -> StorageSaveResult:
        """
        Incrementally writes stream to an isolated temporary file, validating size,
        magic bytes, and calculating SHA-256 before atomically renaming to destination.
        """
        doc_str = self.sanitize_identifier(document_id, "document_id")
        collection_dir = self.get_collection_dir(workspace_id, collection_id)
        final_file_path = collection_dir / f"{doc_str}.pdf"
        self._validate_path_containment(final_file_path)

        # Temporary file for atomic streaming write
        temp_file_path = collection_dir / f".tmp_{doc_str}_{uuid.uuid4().hex[:8]}.pdf"
        hasher = hashlib.sha256()
        bytes_written = 0
        header_checked = False

        try:
            with open(temp_file_path, "wb") as f_out:
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

                    # Validate PDF magic bytes on first chunk
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

            # Atomically replace final file
            os.replace(temp_file_path, final_file_path)
            logger.info(
                f"Persisted source PDF {doc_str} ({bytes_written} bytes, sha256={sha256[:12]}...) "
                f"to {final_file_path}"
            )

            relative_path = str(final_file_path.relative_to(Path.cwd())) if final_file_path.is_relative_to(Path.cwd()) else str(final_file_path)
            source_uri = f"upload://{self.sanitize_identifier(str(workspace_id))}/{self.sanitize_identifier(collection_id)}/{doc_str}.pdf"

            return StorageSaveResult(
                storage_path=relative_path.replace("\\", "/"),
                sha256=sha256,
                byte_count=bytes_written,
                source_uri=source_uri,
            )

        except Exception as e:
            # Guarantee removal of temporary file on failure
            if temp_file_path.exists():
                try:
                    temp_file_path.unlink()
                except Exception as clean_err:
                    logger.warning(f"Failed to remove temp file {temp_file_path}: {clean_err}")
            raise e

    @contextmanager
    def open_stream(self, source_uri: str) -> Generator[BinaryIO, None, None]:
        """
        Opens a readable binary stream for the specified source URI or local path.
        Guarantees that the stream is closed when the context manager exits.
        Raises StorageFileNotFoundError if the object does not exist.
        """
        target = self.resolve_source_file(source_uri)
        if not target or not target.is_file():
            raise StorageFileNotFoundError(f"Storage object not found: {source_uri}")
        with open(target, "rb") as f:
            yield f

    @contextmanager
    def materialize_to_local(self, source_uri: str) -> Generator[Path, None, None]:
        """
        Yields the local Path directly since LocalStorageAdapter already stores files locally.
        Raises StorageFileNotFoundError if the object does not exist.
        """
        target = self.resolve_source_file(source_uri)
        if not target or not target.is_file():
            raise StorageFileNotFoundError(f"Storage object not found: {source_uri}")
        yield target

    def exists(self, source_uri: str) -> bool:
        """Returns True if the object identified by source_uri exists locally."""
        target = self.resolve_source_file(source_uri)
        return bool(target and target.is_file())

    def get_metadata(self, source_uri: str) -> Optional[StorageObjectMetadata]:
        """Retrieves metadata for the stored local file."""
        target = self.resolve_source_file(source_uri)
        if not target or not target.is_file():
            return None
        stat = target.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        return StorageObjectMetadata(
            source_uri=source_uri,
            byte_count=stat.st_size,
            last_modified=mtime,
        )

    def resolve_source_file(self, source_url_or_path: str) -> Optional[Path]:
        """
        Resolves an upload URI (upload://...) or local path within root_dir.
        Returns Path if valid and exists; otherwise returns None.
        Preserved for backwards compatibility with existing callers.
        """
        if not source_url_or_path:
            return None

        clean = source_url_or_path.strip()
        if clean.startswith("upload://"):
            # Format: upload://{workspace_id}/{collection_id}/{doc_id}.pdf
            rel_part = clean[len("upload://") :]
            target = (self.root_dir / rel_part).resolve()
        elif clean.startswith("file://"):
            rel_part = clean[len("file://") :]
            target = Path(rel_part).resolve()
        else:
            p = Path(clean)
            if p.is_absolute():
                target = p.resolve()
            else:
                target = (self.root_dir / clean).resolve()

        try:
            self._validate_path_containment(target)
        except PathTraversalError:
            return None

        if target.is_file():
            return target
        return None

    def delete_file(self, file_path_or_uri: Union[str, Path]) -> bool:
        """
        Safely deletes a stored file within root_dir, validating path containment.
        Returns True if deleted, False if file did not exist or could not be deleted.
        """
        if not file_path_or_uri:
            return False
        resolved = self.resolve_source_file(str(file_path_or_uri))
        if resolved and resolved.is_file():
            try:
                resolved.unlink()
                logger.info(f"Deleted storage file at '{resolved}'")
                return True
            except Exception as e:
                logger.warning(f"Failed to delete storage file at '{resolved}': {e}")
                return False
        return False

    def _resolve_target_path(self, key_or_uri: str) -> Path:
        """Resolves a destination path under root_dir with path traversal protection."""
        clean = str(key_or_uri).strip()
        if clean.startswith("upload://"):
            rel_part = clean[len("upload://") :]
            target = (self.root_dir / rel_part).resolve()
        elif clean.startswith("file://"):
            rel_part = clean[len("file://") :]
            target = Path(rel_part).resolve()
        else:
            p = Path(clean)
            if p.is_absolute():
                target = p.resolve()
            else:
                target = (self.root_dir / clean).resolve()
        self._validate_path_containment(target)
        return target

    def put_bytes(self, storage_key_or_uri: str, data: bytes, content_type: Optional[str] = None) -> str:
        """Persists raw bytes to local storage atomically."""
        target = self._resolve_target_path(storage_key_or_uri)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_target = target.parent / f".tmp_{uuid.uuid4().hex[:8]}"
        try:
            with open(temp_target, "wb") as f:
                f.write(data)
            os.replace(temp_target, target)
        finally:
            if temp_target.exists():
                try:
                    temp_target.unlink()
                except Exception:
                    pass
        rel = str(target.relative_to(self.root_dir)).replace("\\", "/")
        return f"upload://{rel}"

    def upload_file(self, local_path: Union[str, Path], storage_key_or_uri: str, content_type: Optional[str] = None) -> str:
        """Copies local file to destination under root_dir."""
        src = Path(local_path).resolve()
        if not src.is_file():
            raise StorageFileNotFoundError(f"Local source file not found: {local_path}")
        target = self._resolve_target_path(storage_key_or_uri)
        target.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy2(src, target)
        rel = str(target.relative_to(self.root_dir)).replace("\\", "/")
        return f"upload://{rel}"

    def download_file(self, storage_key_or_uri: str, target_local_path: Union[str, Path]) -> Path:
        """Copies file from root_dir to target_local_path."""
        target = self.resolve_source_file(str(storage_key_or_uri))
        if not target or not target.is_file():
            raise StorageFileNotFoundError(f"Storage object not found: {storage_key_or_uri}")
        dest = Path(target_local_path).resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy2(target, dest)
        return dest

    @contextmanager
    def _acquire_process_lock(self, lock_path: Path, timeout: float = 10.0) -> Generator[None, None, None]:
        """
        Acquires an exclusive OS-level advisory lock on an adjacent lockfile.
        Ensures process-safe compare-and-swap coordination on supported local filesystems
        (POSIX ext4/xfs/apfs and Windows NTFS/ReFS).
        Note: Does not guarantee coordination across multi-host network filesystems (NFS/CIFS).
        """
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        f = open(lock_path, "a+b")
        start_time = time.monotonic()
        acquired = False

        while not acquired:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (BlockingIOError, PermissionError, OSError):
                if time.monotonic() - start_time > timeout:
                    f.close()
                    raise StorageError(
                        f"Timeout ({timeout}s) waiting to acquire local process lock on '{lock_path}'."
                    )
                time.sleep(0.01)

        try:
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception as lock_err:
                logger.debug(f"Error releasing process lock on {lock_path}: {lock_err}")
            finally:
                f.close()

    def put_bytes_conditional(
        self,
        storage_key_or_uri: str,
        data: bytes,
        expected_validator: Optional[str],
        content_type: Optional[str] = None,
    ) -> ConditionalWriteResult:
        """
        Atomically persists raw bytes to local storage with conditional checks.

        Validator contract:
        - The validator is the SHA-256 cryptographic digest of the file contents (hex string).
        - If expected_validator is None (create-only):
          Succeeds ONLY if target does NOT exist. Raises StoragePreconditionFailedError if target exists.
        - If expected_validator is a non-empty string (replace-only):
          Succeeds ONLY if target exists AND current SHA-256 matches expected_validator.
          Raises StoragePreconditionFailedError if target is missing or hash mismatches.
        - The read of current content and atomic replacement occur under the exact same process lock.
        - Never silently falls back to an unconditional write.
        """
        if expected_validator is not None and not str(expected_validator).strip():
            raise StoragePreconditionFailedError(
                f"Invalid expected_validator '{expected_validator}': must be None for create-only or a non-empty string for replace-only."
            )

        target = self._resolve_target_path(storage_key_or_uri)
        target.parent.mkdir(parents=True, exist_ok=True)
        lock_path = target.parent / f".{target.name}.lock"

        with self._acquire_process_lock(lock_path):
            if expected_validator is None:
                if target.exists():
                    raise StoragePreconditionFailedError(
                        f"Create-only conditional write failed: target object already exists at '{storage_key_or_uri}'."
                    )
            else:
                if not target.is_file():
                    raise StoragePreconditionFailedError(
                        f"Replace-only conditional write failed: target object does not exist at '{storage_key_or_uri}'."
                    )
                hasher = hashlib.sha256()
                with open(target, "rb") as f_curr:
                    while chunk := f_curr.read(65536):
                        hasher.update(chunk)
                current_validator = hasher.hexdigest()
                if current_validator.lower() != expected_validator.strip().lower():
                    raise StoragePreconditionFailedError(
                        f"Replace-only conditional write failed on '{storage_key_or_uri}': "
                        f"validator mismatch (expected {expected_validator}, current {current_validator})."
                    )

            temp_target = target.parent / f".tmp_{uuid.uuid4().hex[:8]}"
            try:
                with open(temp_target, "wb") as f_out:
                    f_out.write(data)
                os.replace(temp_target, target)
            finally:
                if temp_target.exists():
                    try:
                        temp_target.unlink()
                    except Exception:
                        pass

            new_validator = hashlib.sha256(data).hexdigest()
            rel = str(target.relative_to(self.root_dir)).replace("\\", "/")
            return ConditionalWriteResult(
                source_uri=f"upload://{rel}",
                validator=new_validator,
                byte_count=len(data),
            )
