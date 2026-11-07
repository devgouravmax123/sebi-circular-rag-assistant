"""
Durable Snapshot Store and Local Hydration Engine (Milestone 11 - Wave B).

Manages authoritative remote snapshot bundles (chunks, documents, dense vectors, manifest)
and atomic local cache hydration for multi-tenant collection indexes.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import time
from typing import Dict, List, Optional, Set, Tuple, Any, Generator
import uuid

from app.core.config import get_settings
from app.storage.base import (
    BaseStorageAdapter,
    StorageError,
    StorageConfigurationError,
    StorageFileNotFoundError,
    StoragePreconditionFailedError,
)
from app.schemas.publication import (
    SnapshotManifest,
    ActiveSnapshotPointer,
)

logger = logging.getLogger(__name__)

REQUIRED_BUNDLE_ARTIFACTS = [
    "chunks.json",
    "documents.json",
    "dense_index.npy",
    "dense_index_meta.json",
]


class SnapshotIntegrityError(Exception):
    """Raised when snapshot bundle files or checksums violate verification checks."""
    pass


class StaleSnapshotWriterError(SnapshotIntegrityError):
    """Raised when a concurrent writer attempts to overwrite a newer active snapshot pointer."""
    pass


STAGING_HYDRATE_REGEX = re.compile(r"^_staging_hydrate_([a-zA-Z0-9_-]+)_([a-zA-Z0-9]+)$")
TOMBSTONE_SNAPSHOT_REGEX = re.compile(r"^_tombstone_([a-zA-Z0-9_-]+)_([0-9]+)$")


def _is_symlink_or_reparse_point(path: Path) -> bool:
    """Detects whether a filesystem path is a symlink or directory reparse point (Windows junction)."""
    if path.is_symlink():
        return True
    if os.name == "nt":
        try:
            st = os.lstat(path)
            if hasattr(st, "st_file_attributes") and (st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                return True
        except Exception:
            return True
    return False


def compute_file_sha256(filepath: Path) -> str:
    """Computes SHA-256 digest of a local file."""
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


class DurableSnapshotStore:
    """
    Coordinates durable snapshot bundle upload and local cache hydration.
    When configured with a remote storage provider (e.g. S3 / Cloudflare R2),
    object storage serves as the authoritative source of truth.
    """

    def __init__(
        self,
        storage_adapter: BaseStorageAdapter,
        indexes_dir: str | Path = "data/indexes",
        session_factory: Optional[Any] = None,
    ):
        self.storage_adapter = storage_adapter
        self.indexes_dir = Path(indexes_dir).resolve()
        self.session_factory = session_factory

    def get_bundle_prefix(self, collection_id: str, snapshot_id: str) -> str:
        """Constructs canonical remote object key prefix for a snapshot bundle."""
        cid = BaseStorageAdapter.sanitize_identifier(collection_id, "collection_id")
        sid = BaseStorageAdapter.sanitize_identifier(snapshot_id, "snapshot_id")
        return f"indexes/{cid}/snapshots/{sid}"

    def get_active_pointer_key(self, collection_id: str) -> str:
        """Constructs canonical remote object key for a collection's active pointer."""
        cid = BaseStorageAdapter.sanitize_identifier(collection_id, "collection_id")
        return f"indexes/{cid}/active_snapshot.json"

    def publish_bundle(
        self,
        collection_id: str,
        snapshot_id: str,
        local_snapshot_dir: Path,
        manifest: SnapshotManifest,
        pointer: ActiveSnapshotPointer,
        observed_validator: Optional[str] = None,
    ) -> None:
        """
        Uploads all snapshot bundle artifacts to durable remote storage, verifies their
        presence and integrity, and atomically writes the authoritative active pointer.
        If any upload or verification step fails, the pointer is NOT updated.
        """
        cid = BaseStorageAdapter.sanitize_identifier(collection_id, "collection_id")
        sid = BaseStorageAdapter.sanitize_identifier(snapshot_id, "snapshot_id")
        bundle_prefix = self.get_bundle_prefix(cid, sid)

        # 1. Verify local presence of all required artifacts before uploading
        for artifact_name in REQUIRED_BUNDLE_ARTIFACTS:
            local_path = local_snapshot_dir / artifact_name
            if not local_path.is_file():
                raise SnapshotIntegrityError(f"Missing required snapshot artifact: {local_path}")

        local_manifest_path = local_snapshot_dir / "manifest.json"
        if not local_manifest_path.is_file():
            raise SnapshotIntegrityError(f"Missing snapshot manifest: {local_manifest_path}")

        # 2. Upload all bundle artifacts
        uploaded_keys: List[str] = []
        try:
            for artifact_name, expected_sha in manifest.artifact_checksums.items():
                local_path = local_snapshot_dir / artifact_name
                remote_key = f"{bundle_prefix}/{artifact_name}"
                content_type = "application/json" if artifact_name.endswith(".json") else "application/octet-stream"

                self.storage_adapter.upload_file(local_path, remote_key, content_type=content_type)
                uploaded_keys.append(remote_key)

                # Verify remote artifact metadata
                meta = self.storage_adapter.get_metadata(remote_key)
                if not meta or meta.byte_count != local_path.stat().st_size:
                    raise SnapshotIntegrityError(
                        f"Remote artifact verification failed for {remote_key}: "
                        f"expected {local_path.stat().st_size} bytes, got {getattr(meta, 'byte_count', None)}"
                    )

            # Upload manifest.json
            manifest_remote_key = f"{bundle_prefix}/manifest.json"
            self.storage_adapter.upload_file(local_manifest_path, manifest_remote_key, content_type="application/json")
            uploaded_keys.append(manifest_remote_key)

            manifest_meta = self.storage_adapter.get_metadata(manifest_remote_key)
            if not manifest_meta or manifest_meta.byte_count != local_manifest_path.stat().st_size:
                raise SnapshotIntegrityError(
                    f"Remote manifest verification failed for {manifest_remote_key}."
                )

            # 3. Read current remote pointer and validator for conditional promotion
            remote_existing, current_validator = self.get_remote_active_pointer_and_validator(cid)
            if remote_existing and remote_existing.snapshot_id != sid:
                existing_ts = remote_existing.activated_at
                new_ts = pointer.activated_at
                if existing_ts and new_ts:
                    if existing_ts.tzinfo is None:
                        existing_ts = existing_ts.replace(tzinfo=timezone.utc)
                    if new_ts.tzinfo is None:
                        new_ts = new_ts.replace(tzinfo=timezone.utc)
                    if existing_ts > new_ts:
                        raise StaleSnapshotWriterError(
                            f"Stale publication rejected for collection '{cid}': remote active pointer already "
                            f"points to a newer snapshot '{remote_existing.snapshot_id}' "
                            f"(activated at {existing_ts.isoformat()} > candidate {new_ts.isoformat()})."
                        )
                if pointer.predecessor_snapshot_id is None:
                    pointer.predecessor_snapshot_id = remote_existing.snapshot_id

            # 4. Promote the authoritative active pointer
            pointer_key = self.get_active_pointer_key(cid)
            pointer_bytes = pointer.model_dump_json(indent=2).encode("utf-8")

            if getattr(self.storage_adapter, "supports_conditional_writes", True):
                if observed_validator is not None:
                    expected_validator = observed_validator
                else:
                    expected_validator = current_validator if remote_existing is not None else None

                try:
                    write_res = self.storage_adapter.put_bytes_conditional(
                        pointer_key,
                        pointer_bytes,
                        expected_validator=expected_validator,
                        content_type="application/json",
                    )
                    # Success confirmed on authoritative storage! Update local cache mirror
                    self._update_local_pointer_cache(cid, pointer)
                    logger.info(
                        f"Durable snapshot '{sid}' published and activated successfully for collection '{cid}' "
                        f"({len(uploaded_keys)} artifacts, conditional pointer validator={write_res.validator})."
                    )
                except StoragePreconditionFailedError as conflict_err:
                    # Concurrency conflict detected! Re-read authoritative pointer to determine winner
                    logger.warning(
                        f"Precondition failed promoting active pointer for collection '{cid}' to '{sid}': {conflict_err}. "
                        "Re-reading authoritative remote pointer to resolve conflict."
                    )
                    winning_pointer, _ = self.get_remote_active_pointer_and_validator(cid)
                    if winning_pointer and winning_pointer.snapshot_id == sid:
                        # Idempotent success: this snapshot was already activated
                        logger.info(
                            f"Idempotent publication: snapshot '{sid}' is already active for collection '{cid}'."
                        )
                        self._update_local_pointer_cache(cid, pointer)
                        return

                    # Another publication won the race
                    winning_id = getattr(winning_pointer, "snapshot_id", "unknown")
                    raise StaleSnapshotWriterError(
                        f"Conditional pointer promotion conflict for collection '{cid}': "
                        f"remote active pointer was updated concurrently to snapshot '{winning_id}' (candidate was '{sid}')."
                    ) from conflict_err

                except Exception as write_err:
                    # Ambiguous outcome (e.g. timeout or network disconnect during write).
                    # Re-read authoritative pointer before deciding whether to fail.
                    logger.warning(
                        f"Ambiguous error during conditional pointer write for collection '{cid}' to '{sid}': {write_err}. "
                        "Re-reading authoritative pointer to determine state."
                    )
                    try:
                        current_ptr, _ = self.get_remote_active_pointer_and_validator(cid)
                        if current_ptr and current_ptr.snapshot_id == sid:
                            logger.info(
                                f"Authoritative re-read confirmed snapshot '{sid}' was successfully activated for collection '{cid}' "
                                f"despite ambiguous provider error."
                            )
                            self._update_local_pointer_cache(cid, pointer)
                            return
                    except Exception as re_read_err:
                        logger.debug(f"Could not resolve ambiguous write outcome via re-read: {re_read_err}")

                    # If re-read does not confirm this snapshot is active, propagate the error. Never fall back to unconditional write!
                    raise write_err

            else:
                # Concurrency-safe coordination lock promotion (e.g. Backblaze B2 lacking conditional PutObject)
                with self._acquire_coordination_lock(cid):
                    # 1. Re-read remote authoritative pointer under exclusive lock
                    latest_existing, latest_validator = self.get_remote_active_pointer_and_validator(cid)
                    if observed_validator is not None and latest_validator != observed_validator:
                        winning_id = getattr(latest_existing, "snapshot_id", "unknown")
                        raise StaleSnapshotWriterError(
                            f"Conditional pointer promotion conflict for collection '{cid}': "
                            f"remote active pointer was updated concurrently to snapshot '{winning_id}' "
                            f"(expected validator '{observed_validator}', got '{latest_validator}')."
                        )
                    if latest_existing and latest_existing.snapshot_id != sid:
                        existing_ts = latest_existing.activated_at
                        new_ts = pointer.activated_at
                        if existing_ts and new_ts:
                            if existing_ts.tzinfo is None:
                                existing_ts = existing_ts.replace(tzinfo=timezone.utc)
                            if new_ts.tzinfo is None:
                                new_ts = new_ts.replace(tzinfo=timezone.utc)
                            if existing_ts > new_ts:
                                raise StaleSnapshotWriterError(
                                    f"Stale publication rejected for collection '{cid}': remote active pointer already "
                                    f"points to a newer snapshot '{latest_existing.snapshot_id}' "
                                    f"(activated at {existing_ts.isoformat()} > candidate {new_ts.isoformat()})."
                                )
                        if pointer.predecessor_snapshot_id is None:
                            pointer.predecessor_snapshot_id = latest_existing.snapshot_id
                    elif latest_existing and latest_existing.snapshot_id == sid:
                        logger.info(
                            f"Idempotent publication: snapshot '{sid}' is already active for collection '{cid}'."
                        )
                        self._update_local_pointer_cache(cid, pointer)
                        return

                    # 2. Persist pointer under exclusive coordination lock
                    self.storage_adapter.put_bytes(
                        pointer_key,
                        pointer_bytes,
                        content_type="application/json",
                    )
                    self._update_local_pointer_cache(cid, pointer)
                    logger.info(
                        f"Durable snapshot '{sid}' published and activated successfully for collection '{cid}' "
                        f"({len(uploaded_keys)} artifacts, published via coordination lock)."
                    )

        except Exception as e:
            logger.error(f"Failed to publish durable snapshot bundle '{sid}' for collection '{cid}': {e}", exc_info=True)
            raise e

    @contextmanager
    def _acquire_coordination_lock(self, collection_id: str) -> Generator[None, None, None]:
        """
        Acquires a concurrency-safe coordination lock for active pointer promotion.
        If a PostgreSQL session_factory is provided, acquires a transaction-level advisory
        lock via pg_try_advisory_xact_lock.
        If running in database-enabled mode or staging/production environments, PostgreSQL
        coordination is strictly required and fails closed if unavailable.
        A local process file lock is permitted ONLY in non-database development/test scenarios.
        """
        cid = BaseStorageAdapter.sanitize_identifier(collection_id, "collection_id")
        settings = get_settings()
        is_staging_or_prod_or_render = (
            getattr(settings, "environment", "development").lower() in ("staging", "production")
            or bool(os.getenv("RENDER"))
        )
        requires_postgres_coordination = is_staging_or_prod_or_render

        if self.session_factory is not None:
            # Deterministic 64-bit signed integer hash from collection_id
            lock_key = int.from_bytes(
                hashlib.sha256(f"snapshot_publication:{cid}".encode("utf-8")).digest()[:8],
                byteorder="big",
                signed=True,
            )
            with self.session_factory() as session:
                is_postgres = False
                bind = None
                try:
                    bind = getattr(session, "bind", None)
                    if bind is None and hasattr(session, "get_bind"):
                        bind = session.get_bind()
                    if bind and hasattr(bind, "dialect") and bind.dialect.name == "postgresql":
                        is_postgres = True
                except Exception as bind_err:
                    logger.warning(f"Could not resolve bind dialect for collection '{cid}': {bind_err}")

                if is_postgres:
                    from sqlalchemy import text
                    with session.begin():
                        acquired = session.execute(
                            text("SELECT pg_try_advisory_xact_lock(:key)"),
                            {"key": lock_key},
                        ).scalar()
                        if not acquired:
                            raise StaleSnapshotWriterError(
                                f"Concurrent publication conflict for collection '{cid}': "
                                "another worker currently holds the PostgreSQL transaction advisory lock."
                            )
                        yield
                        return
                else:
                    resolved_dialect = getattr(getattr(bind, "dialect", None), "name", None)
                    raise StorageConfigurationError(
                        f"Backblaze B2 snapshot publication for collection '{cid}' requires a PostgreSQL database bind "
                        f"for cross-node coordination locking, but resolved dialect '{resolved_dialect}'. "
                        "Cannot fall back to local file lock."
                    )

        if requires_postgres_coordination:
            raise StorageConfigurationError(
                f"Backblaze B2 snapshot publication for collection '{cid}' requires an active PostgreSQL "
                "database session factory for cross-node coordination locking. "
                "Cannot fall back to local file lock in staging, production, or Render environments."
            )

        # Process lock fallback for local/single-node or test environments
        pointer_lock_path = self.indexes_dir / cid / f".{cid}.active_pointer.lock"
        with self._acquire_process_lock(pointer_lock_path, timeout=30.0):
            yield

    @contextmanager
    def _acquire_process_lock(self, lock_path: Path, timeout: float = 30.0) -> Generator[None, None, None]:
        """
        Acquires an exclusive advisory process lock on an adjacent lockfile.
        Ensures process-safe and thread-safe serialization for promotion and quarantine.
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
                    raise SnapshotIntegrityError(
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

    # =========================================================================
    # Lock Acquisition Hierarchy:
    # Level 1: Staging Ownership Lock (.{staging_dir_name}.lock)
    #          Acquired before staging creation and held throughout download,
    #          validation, promotion, and local staging cleanup.
    # Level 2: Snapshot Promotion Lock (.{snapshot_id}.lock)
    #          Acquired only during the final atomic cache promotion/quarantine
    #          section.
    #
    # Order Rule:
    # Always acquire Level 1 before Level 2. Never acquire Level 1 while
    # holding Level 2. Staging cleanup only ever acquires Level 1 non-blockingly
    # and NEVER acquires Level 2.
    # =========================================================================

    @contextmanager
    def _try_acquire_process_lock(
        self, lock_path: Path
    ) -> Generator[Tuple[bool, Optional[str]], None, None]:
        """
        Attempts to non-blockingly acquire an exclusive advisory process lock on a lockfile.
        Yields (True, None) if acquired, (False, reason) if locked or unable to open.
        Guarantees release on exit if acquired. Lockfile is never unlinked.
        """
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            f = open(lock_path, "a+b")
        except (PermissionError, OSError) as open_err:
            yield False, f"cannot open lockfile: {open_err}"
            return

        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except (BlockingIOError, PermissionError, OSError) as lock_err:
            f.close()
            yield False, f"lock held: {lock_err}"
            return

        try:
            yield True, None
        finally:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception as release_err:
                logger.debug(f"Error releasing lock {lock_path}: {release_err}")
            finally:
                f.close()

    @contextmanager
    def _try_acquire_staging_lock(
        self, lock_path: Path
    ) -> Generator[Tuple[bool, Optional[str]], None, None]:
        """Backward-compatible alias for _try_acquire_process_lock."""
        with self._try_acquire_process_lock(lock_path) as res:
            yield res

    def _update_local_pointer_cache(self, collection_id: str, pointer: ActiveSnapshotPointer) -> None:
        """Atomically updates the local active_snapshot.json cache file with Windows sharing conflict handling."""
        cid = BaseStorageAdapter.sanitize_identifier(collection_id, "collection_id")
        local_pointer_path = self.indexes_dir / cid / "active_snapshot.json"

        # Check if local pointer already matches to avoid redundant disk I/O and lock contention
        if local_pointer_path.is_file():
            try:
                with open(local_pointer_path, "r", encoding="utf-8") as f:
                    existing_data = json.load(f)
                if (
                    existing_data.get("snapshot_id") == pointer.snapshot_id
                    and existing_data.get("manifest_checksum") == pointer.manifest_checksum
                ):
                    return
            except Exception:
                pass

        temp_local_pointer = self.indexes_dir / cid / f".tmp_pointer_{uuid.uuid4().hex[:8]}.json"
        local_pointer_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(temp_local_pointer, "w", encoding="utf-8") as f:
                f.write(pointer.model_dump_json(indent=2))

            # On Windows NTFS, concurrent os.replace can briefly collide with Access Denied (WinError 5)
            for attempt in range(10):
                try:
                    os.replace(temp_local_pointer, local_pointer_path)
                    break
                except (PermissionError, OSError) as replace_err:
                    # Check if another concurrent thread successfully wrote the target pointer matching both ID and checksum
                    if local_pointer_path.is_file():
                        try:
                            with open(local_pointer_path, "r", encoding="utf-8") as f:
                                existing_data = json.load(f)
                            if (
                                existing_data.get("snapshot_id") == pointer.snapshot_id
                                and existing_data.get("manifest_checksum") == pointer.manifest_checksum
                            ):
                                break
                        except Exception:
                            pass
                    if attempt == 9:
                        raise replace_err
                    time.sleep(0.01 * (attempt + 1))
        finally:
            if temp_local_pointer.exists():
                try:
                    temp_local_pointer.unlink(missing_ok=True)
                except Exception:
                    pass

    def get_remote_active_pointer_and_validator(
        self, collection_id: str
    ) -> Tuple[Optional[ActiveSnapshotPointer], Optional[str]]:
        """
        Retrieves the authoritative remote active snapshot pointer and its opaque validator
        (ETag for S3/R2, SHA-256 hex string for local storage).
        Returns (None, None) if the pointer does not exist.
        Raises StorageError on network/provider outage.
        """
        cid = BaseStorageAdapter.sanitize_identifier(collection_id, "collection_id")
        pointer_key = self.get_active_pointer_key(cid)

        try:
            raw_bytes = self.storage_adapter.get_bytes(pointer_key)
            if not raw_bytes:
                return None, None

            # Determine validator:
            # For local storage, validator is SHA-256 of raw bytes
            # For S3 / remote storage, read ETag from metadata if present
            meta = self.storage_adapter.get_metadata(pointer_key)
            if meta and getattr(meta, "etag", None):
                validator = meta.etag
            else:
                validator = hashlib.sha256(raw_bytes).hexdigest()

            data = json.loads(raw_bytes.decode("utf-8"))
            pointer = ActiveSnapshotPointer(**data)
            if pointer.collection_id != cid:
                logger.error(
                    f"Remote pointer collection mismatch: pointer declares '{pointer.collection_id}', expected '{cid}'."
                )
                return None, None
            return pointer, validator
        except StorageFileNotFoundError:
            return None, None
        except Exception as e:
            logger.warning(f"Error reading remote active pointer for collection '{cid}': {e}")
            raise e

    def get_remote_active_pointer(self, collection_id: str) -> Optional[ActiveSnapshotPointer]:
        """
        Retrieves and parses the authoritative remote active snapshot pointer.
        Returns None if not published.
        Raises StorageError on network/provider outage.
        """
        pointer, _ = self.get_remote_active_pointer_and_validator(collection_id)
        return pointer

    def is_cache_valid(
        self,
        cache_dir: Path,
        expected_manifest_chk: Optional[str] = None,
    ) -> bool:
        """
        Validates that a local cache directory exists, has a valid manifest matching
        the expected checksum (if provided), and contains all required bundle artifacts.
        """
        if not cache_dir.is_dir():
            return False
        manifest_file = cache_dir / "manifest.json"
        if not manifest_file.is_file():
            return False
        if expected_manifest_chk:
            try:
                chk = compute_file_sha256(manifest_file)
                if chk != expected_manifest_chk:
                    return False
            except Exception:
                return False
        for artifact_name in REQUIRED_BUNDLE_ARTIFACTS:
            if not (cache_dir / artifact_name).is_file():
                return False
        return True

    def get_cached_snapshot_dir(self, collection_id: str) -> Optional[Path]:
        """
        Inspects the local cache for collection_id and returns the local Path if a previously
        verified snapshot is available.
        """
        cid = BaseStorageAdapter.sanitize_identifier(collection_id, "collection_id")
        local_pointer_path = self.indexes_dir / cid / "active_snapshot.json"
        if not local_pointer_path.is_file():
            return None

        try:
            with open(local_pointer_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            snap_id = data.get("snapshot_id")
            manifest_chk = data.get("manifest_checksum")
            if not snap_id:
                return None

            cache_dir = self.indexes_dir / cid / "snapshots" / snap_id
            if self.is_cache_valid(cache_dir, manifest_chk):
                return cache_dir
        except Exception:
            return None
        return None

    def hydrate_snapshot(
        self,
        collection_id: str,
        force_refresh: bool = False,
    ) -> Optional[Path]:
        """
        Resolves the remote active pointer, checks the local cache, and hydrates
        the complete immutable snapshot bundle if absent, stale, or incomplete.
        Guarantees atomic cache availability, concurrency-safe cache reuse, and checksum verification.

        Remote Outage Policy:
        - If remote storage is unreachable due to a network or connection error:
          - If a previously verified local cache exists, logs a warning and returns it.
          - If no valid local cache exists, returns None (fails closed).
        """
        cid = BaseStorageAdapter.sanitize_identifier(collection_id, "collection_id")

        try:
            remote_pointer = self.get_remote_active_pointer(cid)
        except Exception as conn_err:
            logger.warning(
                f"Remote storage unreachable when checking active pointer for collection '{cid}': {conn_err}."
            )
            # Apply explicit outage policy: serve verified cache if available
            cached_dir = self.get_cached_snapshot_dir(cid)
            if cached_dir is not None:
                logger.warning(
                    f"Serving previously verified local snapshot cache '{cached_dir.name}' for collection '{cid}' "
                    "during remote storage outage."
                )
                return cached_dir
            return None

        if remote_pointer is None:
            return None

        expected_snap_id = remote_pointer.snapshot_id
        expected_manifest_chk = remote_pointer.manifest_checksum
        cache_dir = self.indexes_dir / cid / "snapshots" / expected_snap_id

        # 1. Check local cache validity: if already complete and valid, reuse without downloading
        if not force_refresh and self.is_cache_valid(cache_dir, expected_manifest_chk):
            # Cache is up to date and valid! Avoid redundant network downloads.
            self._update_local_pointer_cache(cid, remote_pointer)
            return cache_dir

        # Cache miss, stale, or incomplete: hydrate into isolated staging directory under full-lifecycle ownership lock
        bundle_prefix = self.get_bundle_prefix(cid, expected_snap_id)
        staging_dir_name = f"_staging_hydrate_{expected_snap_id}_{uuid.uuid4().hex[:8]}"
        staging_dir = self.indexes_dir / cid / "snapshots" / staging_dir_name
        staging_lock_path = self.indexes_dir / cid / "snapshots" / f".{staging_dir_name}.lock"

        # Staging directory parent must exist for lockfile and staging directory
        staging_dir.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Full-lifecycle staging ownership lock is acquired BEFORE creating the staging directory
            # and held throughout download, validation, promotion, and failure cleanup.
            with self._acquire_process_lock(staging_lock_path):
                staging_dir.mkdir(parents=True, exist_ok=True)
                try:
                    # 1. Download and verify manifest.json
                    remote_manifest_key = f"{bundle_prefix}/manifest.json"
                    local_manifest_path = staging_dir / "manifest.json"
                    self.storage_adapter.download_file(remote_manifest_key, local_manifest_path)

                    downloaded_manifest_chk = compute_file_sha256(local_manifest_path)
                    if downloaded_manifest_chk != expected_manifest_chk:
                        raise SnapshotIntegrityError(
                            f"Downloaded manifest checksum mismatch: expected {expected_manifest_chk}, got {downloaded_manifest_chk}"
                        )

                    with open(local_manifest_path, "r", encoding="utf-8") as f:
                        manifest_data = json.load(f)
                    manifest = SnapshotManifest(**manifest_data)

                    if manifest.collection_id != cid:
                        raise SnapshotIntegrityError(
                            f"Downloaded manifest collection mismatch: expected {cid}, got {manifest.collection_id}"
                        )

                    # 2. Download and verify all bundle artifacts
                    for artifact_name, expected_sha in manifest.artifact_checksums.items():
                        remote_key = f"{bundle_prefix}/{artifact_name}"
                        local_path = staging_dir / artifact_name
                        self.storage_adapter.download_file(remote_key, local_path)

                        computed_sha = compute_file_sha256(local_path)
                        if computed_sha != expected_sha:
                            raise SnapshotIntegrityError(
                                f"Checksum mismatch on hydrated artifact '{artifact_name}': "
                                f"expected {expected_sha}, got {computed_sha}"
                            )

                    # 3. Atomically expose to local cache directory under exclusive promotion lock
                    cache_dir.parent.mkdir(parents=True, exist_ok=True)
                    lock_path = cache_dir.parent / f".{expected_snap_id}.lock"

                    with self._acquire_process_lock(lock_path):
                        # Under lock: re-check whether another concurrent process has already produced a valid target cache
                        if self.is_cache_valid(cache_dir, expected_manifest_chk):
                            logger.info(
                                f"Concurrent hydration: valid cache directory already produced by another worker "
                                f"for snapshot '{expected_snap_id}' in collection '{cid}'. Reusing existing cache."
                            )
                            shutil.rmtree(staging_dir, ignore_errors=True)
                            self._update_local_pointer_cache(cid, remote_pointer)
                            return cache_dir

                        # Target directory exists but is incomplete or invalid:
                        # Do NOT recursively delete in-place. Attempt to move it out of the way to an isolated quarantine path.
                        if cache_dir.exists():
                            quarantine_dir = cache_dir.parent / f"_corrupt_{expected_snap_id}_{uuid.uuid4().hex[:8]}"
                            try:
                                os.rename(cache_dir, quarantine_dir)
                                shutil.rmtree(quarantine_dir, ignore_errors=True)
                            except (PermissionError, OSError) as lock_err:
                                logger.error(
                                    f"Cannot safely replace invalid cache directory '{cache_dir}': "
                                    f"directory is locked or in use by another process ({lock_err}). Failing closed."
                                )
                                raise SnapshotIntegrityError(
                                    f"Cannot safely replace invalid cache directory '{cache_dir}': "
                                    f"directory is locked or in use by another process ({lock_err})."
                                ) from lock_err

                        # Atomically promote staging directory into cache_dir
                        try:
                            staging_dir.rename(cache_dir)
                        except (FileExistsError, OSError) as rename_err:
                            # Defensive fallback: another competing writer promoted first
                            shutil.rmtree(staging_dir, ignore_errors=True)
                            if self.is_cache_valid(cache_dir, expected_manifest_chk):
                                self._update_local_pointer_cache(cid, remote_pointer)
                                return cache_dir
                            else:
                                raise SnapshotIntegrityError(
                                    f"Concurrent promotion race: target cache '{cache_dir}' was created concurrently "
                                    f"but failed integrity validation: {rename_err}"
                                )

                        # 4. Atomically update local active pointer cache
                        self._update_local_pointer_cache(cid, remote_pointer)

                        logger.info(
                            f"Hydrated durable snapshot '{expected_snap_id}' into local cache for collection '{cid}'."
                        )
                        return cache_dir

                finally:
                    if staging_dir.exists():
                        shutil.rmtree(staging_dir, ignore_errors=True)

        except Exception as e:
            logger.error(f"Failed to hydrate snapshot '{expected_snap_id}' for collection '{cid}': {e}", exc_info=True)
            raise e

    def cleanup_abandoned_hydration_staging(
        self,
        collection_id: str,
        min_age_seconds: int = 3600,
        dry_run: bool = True,
        max_entries: int = 100,
    ) -> Dict[str, Any]:
        """
        Safely identifies and purges abandoned local hydration staging directories.
        Guarantees that active hydrator staging directories, completed snapshots,
        and quarantined directories are never touched.

        Lock Acquisition Hierarchy:
        Level 1: Staging Ownership Lock (.{staging_dir_name}.lock)
        Level 2: Snapshot Promotion Lock (.{snapshot_id}.lock)
        Cleanup only ever attempts non-blocking acquisition of Level 1 locks.

        Eligibility Rules:
        1. Target root must be confirmed within self.indexes_dir.
        2. Directory name must strictly match _staging_hydrate_{snapshot_id}_{suffix}.
        3. Directory must be a direct child of snapshots_dir, NOT a symlink or reparse point.
        4. Directory age (time since mtime) must be strictly >= min_age_seconds.
        5. Exclusive staging ownership lock must be successfully acquired non-blockingly.

        Returns structured dictionary reporting scanned, skipped, candidates, deleted, and errors.
        """
        try:
            cid = BaseStorageAdapter.sanitize_identifier(collection_id, "collection_id")
        except Exception as e:
            logger.error(f"Invalid collection_id for staging cleanup '{collection_id}': {e}")
            return {
                "collection_id": str(collection_id),
                "dry_run": dry_run,
                "scanned_entries": 0,
                "candidates_found": 0,
                "candidates": [],
                "deleted_paths": [],
                "skipped": [],
                "errors": [{"path": str(collection_id), "error": str(e)}],
            }

        result: Dict[str, Any] = {
            "collection_id": cid,
            "dry_run": dry_run,
            "scanned_entries": 0,
            "candidates_found": 0,
            "candidates": [],
            "deleted_paths": [],
            "skipped": [],
            "errors": [],
        }

        snapshots_dir = (self.indexes_dir / cid / "snapshots").resolve()

        # Confinement check: snapshots_dir must be inside indexes_dir
        indexes_dir_resolved = self.indexes_dir.resolve()
        try:
            if not snapshots_dir.is_relative_to(indexes_dir_resolved):
                result["errors"].append({
                    "path": str(snapshots_dir),
                    "error": "Snapshots directory escapes designated indexes root.",
                })
                return result
        except Exception as e:
            result["errors"].append({"path": str(snapshots_dir), "error": str(e)})
            return result

        if not snapshots_dir.is_dir():
            return result

        try:
            entries = sorted(snapshots_dir.iterdir(), key=lambda p: p.name)
        except Exception as scan_err:
            logger.error(f"Cannot scan snapshots directory {snapshots_dir}: {scan_err}")
            result["errors"].append({"path": str(snapshots_dir), "error": str(scan_err)})
            return result

        candidates_evaluated = 0

        for entry in entries:
            result["scanned_entries"] += 1

            # 1. Non-directory entries (including lock files, pointers, etc.) are strictly ignored
            if not entry.is_dir():
                continue

            name = entry.name

            # 2. Never inspect completed snapshots, corrupt quarantine dirs, or unrelated directories
            if not name.startswith("_staging_hydrate_"):
                continue

            # Bound work per invocation using max_entries
            if candidates_evaluated >= max_entries:
                logger.info(
                    f"Reached max_entries limit ({max_entries}) for staging cleanup on collection '{cid}'."
                )
                break
            candidates_evaluated += 1

            # 3. Validate exact name structure: _staging_hydrate_{snapshot_id}_{suffix}
            match = STAGING_HYDRATE_REGEX.match(name)
            if not match:
                result["skipped"].append({"path": str(entry), "reason": "malformed_staging_name"})
                continue

            snap_id, suffix = match.group(1), match.group(2)
            try:
                BaseStorageAdapter.sanitize_identifier(snap_id, "snapshot_id")
            except Exception:
                result["skipped"].append({"path": str(entry), "reason": "malformed_staging_name"})
                continue

            # 4. Confinement & Symlink / Reparse Point Rejection
            if _is_symlink_or_reparse_point(entry):
                result["skipped"].append({"path": str(entry), "reason": "symlink_or_reparse_point"})
                continue

            try:
                resolved_entry = entry.resolve()
                if resolved_entry.parent != snapshots_dir or not resolved_entry.is_relative_to(snapshots_dir):
                    result["skipped"].append({"path": str(entry), "reason": "path_traversal_rejected"})
                    continue
            except Exception as resolve_err:
                result["skipped"].append({"path": str(entry), "reason": "resolve_failed", "detail": str(resolve_err)})
                continue

            # 5. Age Threshold Check
            try:
                mtime = entry.stat().st_mtime
                age_seconds = time.time() - mtime
            except Exception as stat_err:
                result["skipped"].append({"path": str(entry), "reason": "stat_failed", "detail": str(stat_err)})
                continue

            if age_seconds < min_age_seconds:
                result["skipped"].append({
                    "path": str(entry),
                    "reason": "recent_staging_below_min_age",
                    "age_seconds": round(age_seconds, 2),
                    "min_age_seconds": min_age_seconds,
                })
                continue

            # 6. Non-blocking Staging Ownership Lock Acquisition
            lock_path = snapshots_dir / f".{name}.lock"
            with self._try_acquire_staging_lock(lock_path) as (acquired, lock_reason):
                if not acquired:
                    result["skipped"].append({
                        "path": str(entry),
                        "reason": "locked_or_in_use",
                        "detail": lock_reason or "lock held by live process",
                    })
                    continue

                # Lock held exclusively by this cleanup worker!
                result["candidates_found"] += 1
                result["candidates"].append(str(entry))

                if dry_run:
                    logger.debug(f"Dry-run: abandoned staging candidate identified: {entry}")
                    continue

                # Actual cleanup execution under exclusive staging ownership lock
                try:
                    shutil.rmtree(entry)
                    result["deleted_paths"].append(str(entry))
                    logger.info(f"Deleted abandoned hydration staging directory: {entry}")
                except Exception as del_err:
                    logger.error(f"Failed to delete abandoned staging directory {entry}: {del_err}")
                    result["errors"].append({"path": str(entry), "error": str(del_err)})

            # Invariant: Never unlink lock_path. The staging ownership lockfile is retained as a
            # persistent coordination token to prevent the release-and-unlink race where another
            # process opens the path while a concurrent cleaner unlinks it.

        return result

    def tombstone_retired_snapshots(
        self,
        collection_id: str,
        min_deactivation_age_seconds: int = 86400,
        dry_run: bool = True,
        max_entries: int = 50,
    ) -> Dict[str, Any]:
        """
        Stage 1 of local cache retention: safely identifies completed snapshot cache directories
        that are retired (neither active N nor predecessor N-1) and older than min_deactivation_age_seconds,
        and renames them to _tombstone_{snapshot_id}_{timestamp}.

        Safety guarantees:
        - Never touches remote storage or remote snapshots.
        - Node-local cache only.
        - Authoritative remote active pointer is the source of truth for N and N-1.
        - If remote pointer cannot be retrieved or predecessor N-1 is not established, safely aborts.
        - Acquires Level 2 lifecycle lock (.{snapshot_id}.lock) non-blockingly before renaming.
        - If an active loader holds the lifecycle lock, the snapshot is safely skipped.
        - Retains lockfiles indefinitely (never unlinks).
        - Defaults to dry_run=True.
        """
        cid = BaseStorageAdapter.sanitize_identifier(collection_id, "collection_id")
        result: Dict[str, Any] = {
            "collection_id": cid,
            "dry_run": dry_run,
            "aborted": False,
            "abort_reason": None,
            "active_snapshot_id": None,
            "predecessor_snapshot_id": None,
            "scanned_entries": 0,
            "candidates_found": 0,
            "candidates": [],
            "tombstoned_paths": [],
            "skipped": [],
            "errors": [],
        }

        # 1. Retrieve authoritative active pointer
        try:
            remote_pointer = self.get_remote_active_pointer(cid)
        except Exception as e:
            result["aborted"] = True
            result["abort_reason"] = f"Failed to retrieve authoritative remote active pointer: {e}"
            logger.warning(f"Retention sweep aborted for '{cid}': {result['abort_reason']}")
            return result

        if remote_pointer is None:
            result["aborted"] = True
            result["abort_reason"] = "No active snapshot pointer found on authoritative storage"
            return result

        active_id = remote_pointer.snapshot_id
        pred_id = remote_pointer.predecessor_snapshot_id
        result["active_snapshot_id"] = active_id
        result["predecessor_snapshot_id"] = pred_id

        # Precondition check: N-1 must be authoritatively established
        if not pred_id:
            result["aborted"] = True
            result["abort_reason"] = "Authoritative predecessor (N-1) is not established in active pointer; aborting retention sweep safely"
            logger.warning(f"Retention sweep aborted for '{cid}': {result['abort_reason']}")
            return result

        # Check deactivation age grounding: N was activated at remote_pointer.activated_at
        activated_at = remote_pointer.activated_at
        if activated_at is None:
            result["aborted"] = True
            result["abort_reason"] = "Authoritative active pointer missing activated_at timestamp"
            return result

        if activated_at.tzinfo is None:
            activated_at = activated_at.replace(tzinfo=timezone.utc)

        now_utc = datetime.now(timezone.utc)
        deactivation_elapsed = (now_utc - activated_at).total_seconds()
        if deactivation_elapsed < min_deactivation_age_seconds:
            result["aborted"] = True
            result["abort_reason"] = (
                f"Active snapshot was activated {deactivation_elapsed:.1f}s ago, which is below "
                f"min_deactivation_age_seconds ({min_deactivation_age_seconds}s); retired snapshots are not yet eligible"
            )
            return result

        protected_ids = {active_id, pred_id}

        # 2. Confinement and root path checks
        snapshots_dir = (self.indexes_dir / cid / "snapshots").resolve()
        indexes_dir_resolved = self.indexes_dir.resolve()
        try:
            if not snapshots_dir.is_relative_to(indexes_dir_resolved):
                result["errors"].append({
                    "path": str(snapshots_dir),
                    "error": "Snapshots directory escapes designated indexes root.",
                })
                return result
        except Exception as e:
            result["errors"].append({"path": str(snapshots_dir), "error": str(e)})
            return result

        if not snapshots_dir.is_dir():
            return result

        try:
            entries = sorted(snapshots_dir.iterdir(), key=lambda p: p.name)
        except Exception as scan_err:
            result["errors"].append({"path": str(snapshots_dir), "error": str(scan_err)})
            return result

        candidates_evaluated = 0

        for entry in entries:
            result["scanned_entries"] += 1
            if not entry.is_dir():
                continue

            name = entry.name
            # Skip staging, quarantine, and existing tombstones
            if name.startswith(("_staging_hydrate_", "_corrupt_", "_tombstone_")):
                continue

            if candidates_evaluated >= max_entries:
                break
            candidates_evaluated += 1

            # Validate snapshot id name structure
            try:
                BaseStorageAdapter.sanitize_identifier(name, "snapshot_id")
            except Exception:
                result["skipped"].append({"path": str(entry), "reason": "malformed_snapshot_name"})
                continue

            # Symlink / junction / reparse point check
            if _is_symlink_or_reparse_point(entry):
                result["skipped"].append({"path": str(entry), "reason": "symlink_or_reparse_point"})
                continue

            # Path confinement check
            try:
                resolved_entry = entry.resolve()
                if resolved_entry.parent != snapshots_dir or not resolved_entry.is_relative_to(snapshots_dir):
                    result["skipped"].append({"path": str(entry), "reason": "path_traversal_rejected"})
                    continue
            except Exception as resolve_err:
                result["skipped"].append({"path": str(entry), "reason": "resolve_failed", "detail": str(resolve_err)})
                continue

            # Protected N / N-1 check
            if name in protected_ids:
                result["skipped"].append({"path": str(entry), "reason": "protected_active_or_predecessor"})
                continue

            # Age check on filesystem mtime as additional guard
            try:
                mtime = entry.stat().st_mtime
                fs_age = time.time() - mtime
                if fs_age < min_deactivation_age_seconds:
                    result["skipped"].append({
                        "path": str(entry),
                        "reason": "recent_mtime_below_min_deactivation_age",
                        "fs_age_seconds": round(fs_age, 2),
                        "min_deactivation_age_seconds": min_deactivation_age_seconds,
                    })
                    continue
            except Exception as stat_err:
                result["skipped"].append({"path": str(entry), "reason": "stat_failed", "detail": str(stat_err)})
                continue

            # Non-blocking Level 2 lifecycle lock acquisition
            lock_path = snapshots_dir / f".{name}.lock"
            with self._try_acquire_process_lock(lock_path) as (acquired, lock_reason):
                if not acquired:
                    result["skipped"].append({
                        "path": str(entry),
                        "reason": "locked_or_in_use",
                        "detail": lock_reason or "lock held by active loader or cleaner",
                    })
                    continue

                # Under lock: re-verify target directory still exists and is not protected
                if not entry.is_dir() or name in protected_ids:
                    continue

                result["candidates_found"] += 1
                result["candidates"].append(str(entry))

                if dry_run:
                    logger.debug(f"Dry-run: retired snapshot tombstone candidate identified: {entry}")
                    continue

                # Stage 1 eviction: rename to tombstone directory
                tombstone_name = f"_tombstone_{name}_{int(time.time())}"
                tombstone_path = snapshots_dir / tombstone_name
                try:
                    entry.rename(tombstone_path)
                    result["tombstoned_paths"].append(str(tombstone_path))
                    logger.info(f"Tombstoned retired snapshot directory: {entry} -> {tombstone_path}")
                except Exception as ren_err:
                    logger.error(f"Failed to tombstone snapshot directory {entry}: {ren_err}")
                    result["errors"].append({"path": str(entry), "error": str(ren_err)})

            # Invariant: Persistent lockfile lock_path is NEVER unlinked.

        return result

    def purge_drained_tombstones(
        self,
        collection_id: str,
        drain_window_seconds: int = 3600,
        dry_run: bool = True,
        max_entries: int = 50,
    ) -> Dict[str, Any]:
        """
        Stage 2 of local cache retention: safely identifies tombstoned snapshot directories
        (_tombstone_{snapshot_id}_{timestamp}) whose drain window has elapsed, and deletes them.

        Safety guarantees:
        - Only purges directories matching TOMBSTONE_SNAPSHOT_REGEX.
        - Never touches canonical snapshots, active snapshots, or staging/quarantine directories.
        - Acquires Level 2 lifecycle lock (.{snapshot_id}.lock) non-blockingly before deleting.
        - If an active loader or concurrent process holds the lock, deletion is skipped.
        - Retains lockfiles indefinitely (never unlinks).
        - Defaults to dry_run=True.
        """
        cid = BaseStorageAdapter.sanitize_identifier(collection_id, "collection_id")
        result: Dict[str, Any] = {
            "collection_id": cid,
            "dry_run": dry_run,
            "scanned_entries": 0,
            "candidates_found": 0,
            "candidates": [],
            "purged_paths": [],
            "skipped": [],
            "errors": [],
        }

        snapshots_dir = (self.indexes_dir / cid / "snapshots").resolve()
        indexes_dir_resolved = self.indexes_dir.resolve()
        try:
            if not snapshots_dir.is_relative_to(indexes_dir_resolved):
                result["errors"].append({
                    "path": str(snapshots_dir),
                    "error": "Snapshots directory escapes designated indexes root.",
                })
                return result
        except Exception as e:
            result["errors"].append({"path": str(snapshots_dir), "error": str(e)})
            return result

        if not snapshots_dir.is_dir():
            return result

        try:
            entries = sorted(snapshots_dir.iterdir(), key=lambda p: p.name)
        except Exception as scan_err:
            result["errors"].append({"path": str(snapshots_dir), "error": str(scan_err)})
            return result

        candidates_evaluated = 0

        for entry in entries:
            result["scanned_entries"] += 1
            if not entry.is_dir():
                continue

            name = entry.name
            if not name.startswith("_tombstone_"):
                continue

            if candidates_evaluated >= max_entries:
                break
            candidates_evaluated += 1

            match = TOMBSTONE_SNAPSHOT_REGEX.match(name)
            if not match:
                result["skipped"].append({"path": str(entry), "reason": "malformed_tombstone_name"})
                continue

            snap_id, tombstone_ts_str = match.group(1), match.group(2)
            try:
                BaseStorageAdapter.sanitize_identifier(snap_id, "snapshot_id")
                tombstone_ts = int(tombstone_ts_str)
            except Exception:
                result["skipped"].append({"path": str(entry), "reason": "malformed_tombstone_name"})
                continue

            # Symlink / junction / reparse point check
            if _is_symlink_or_reparse_point(entry):
                result["skipped"].append({"path": str(entry), "reason": "symlink_or_reparse_point"})
                continue

            # Path confinement check
            try:
                resolved_entry = entry.resolve()
                if resolved_entry.parent != snapshots_dir or not resolved_entry.is_relative_to(snapshots_dir):
                    result["skipped"].append({"path": str(entry), "reason": "path_traversal_rejected"})
                    continue
            except Exception as resolve_err:
                result["skipped"].append({"path": str(entry), "reason": "resolve_failed", "detail": str(resolve_err)})
                continue

            # Check drain window age
            now = time.time()
            tombstone_age = now - tombstone_ts
            try:
                mtime_age = now - entry.stat().st_mtime
            except Exception:
                mtime_age = tombstone_age

            effective_age = min(tombstone_age, mtime_age)
            if effective_age < drain_window_seconds:
                result["skipped"].append({
                    "path": str(entry),
                    "reason": "drain_window_not_elapsed",
                    "effective_age_seconds": round(effective_age, 2),
                    "drain_window_seconds": drain_window_seconds,
                })
                continue

            # Non-blocking Level 2 lifecycle lock acquisition
            lock_path = snapshots_dir / f".{snap_id}.lock"
            with self._try_acquire_process_lock(lock_path) as (acquired, lock_reason):
                if not acquired:
                    result["skipped"].append({
                        "path": str(entry),
                        "reason": "locked_or_in_use",
                        "detail": lock_reason or "lock held by live process",
                    })
                    continue

                if not entry.is_dir():
                    continue

                result["candidates_found"] += 1
                result["candidates"].append(str(entry))

                if dry_run:
                    logger.debug(f"Dry-run: drained tombstone purge candidate identified: {entry}")
                    continue

                # Stage 2 purge
                try:
                    shutil.rmtree(entry)
                    result["purged_paths"].append(str(entry))
                    logger.info(f"Purged drained snapshot tombstone directory: {entry}")
                except Exception as del_err:
                    logger.error(f"Failed to purge tombstone directory {entry}: {del_err}")
                    result["errors"].append({"path": str(entry), "error": str(del_err)})

            # Invariant: Persistent lockfile lock_path is NEVER unlinked.

        return result
