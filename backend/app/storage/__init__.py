"""
Storage abstractions for private tenant documents (Milestone 10 & 11).
"""
from typing import Optional

from app.storage.base import (
    BaseStorageAdapter,
    StorageSaveResult,
    StorageObjectMetadata,
    ConditionalWriteResult,
    StorageError,
    StorageConfigurationError,
    PathTraversalError,
    StorageLimitExceededError,
    InvalidFileFormatError,
    StorageFileNotFoundError,
    StoragePreconditionFailedError,
    StorageConditionalWriteUnsupportedError,
    PDF_MAGIC_BYTES,
    DEFAULT_MAX_UPLOAD_BYTES,
)
from app.storage.local import LocalStorageAdapter
from app.storage.s3 import S3StorageAdapter


def get_storage_adapter(settings=None) -> BaseStorageAdapter:
    """
    Factory / dependency provider for selecting the active storage adapter.
    Supports 'local' (default), 's3' (AWS S3 or Cloudflare R2), and 'b2'/'backblaze' (Backblaze B2).
    Invalid or unsupported provider configuration raises StorageConfigurationError explicitly.
    Does NOT silently fall back to local when an invalid provider is specified.
    """
    if settings is None:
        from app.core.config import get_settings
        settings = get_settings()

    provider = (getattr(settings, "storage_provider", None) or "local").strip().lower()
    if provider == "local":
        return LocalStorageAdapter(
            root_dir=settings.uploads_dir,
            max_upload_bytes=settings.max_upload_bytes,
        )
    elif provider in ("s3", "r2", "cloudflare_r2", "b2", "backblaze", "backblaze_b2"):
        return S3StorageAdapter(
            bucket_name=getattr(settings, "s3_bucket_name", None),
            access_key_id=getattr(settings, "s3_access_key_id", None),
            secret_access_key=getattr(settings, "s3_secret_access_key", None),
            endpoint_url=getattr(settings, "s3_endpoint_url", None),
            region_name=getattr(settings, "s3_region_name", "auto"),
            max_upload_bytes=settings.max_upload_bytes,
            provider_type=provider,
        )

    raise StorageConfigurationError(
        f"Unsupported storage provider '{provider}'. Supported providers: ['local', 's3', 'b2']."
    )


__all__ = [
    "BaseStorageAdapter",
    "LocalStorageAdapter",
    "S3StorageAdapter",
    "get_storage_adapter",
    "StorageSaveResult",
    "StorageObjectMetadata",
    "StorageError",
    "StorageConfigurationError",
    "StorageLimitExceededError",
    "InvalidFileFormatError",
    "StorageFileNotFoundError",
    "StoragePreconditionFailedError",
    "StorageConditionalWriteUnsupportedError",
    "PathTraversalError",
    "PDF_MAGIC_BYTES",
    "DEFAULT_MAX_UPLOAD_BYTES",
]
