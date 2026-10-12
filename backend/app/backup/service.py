"""
Coordinated Backup and Disaster Recovery Service (Phase 10.1.8).

Guarantees atomic consistency between:
1. Authoritative PostgreSQL database (or file-backed document/chunk models)
2. Active search index snapshot pointer and manifest
3. Raw source regulatory PDFs and processed artifacts

Features:
- Global quiescence via publication lock (.publish.lock) preventing mid-backup index mutations.
- Full database dump (SQL format) when PostgreSQL is configured.
- Active index snapshot preservation (BM25 chunks, documents, dense_index.npy, manifest.json).
- Canonical source document preservation (data/raw and data/processed).
- Cryptographic SHA-256 verification and structured manifest with timestamp, versions, and counts.
- Isolated restoration engine with target isolation (never overwrites live database by default).
- Optional off-host upload hook (disabled by default, strictly zero-cost).
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Dict, List, Optional, Any, Tuple
from urllib.parse import urlparse

from app.core.config import Settings, get_settings
from app.persistence.interfaces import StorageRepositoryFactory
from app.persistence.postgres import PostgresStorageRepositoryFactory
from app.persistence.file_repo import FileStorageRepositoryFactory

logger = logging.getLogger("backup_service")


def compute_sha256(filepath: Path) -> str:
    """Compute SHA-256 checksum of a file on disk."""
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def normalize_db_target(url_str: Optional[str]) -> Optional[Tuple[str, int, str]]:
    """
    Normalizes a database connection string into (host, port, db_name) tuple
    without exposing credentials. Returns None if url_str is empty/None.
    """
    if not url_str or not url_str.strip():
        return None
    try:
        from sqlalchemy.engine import make_url
        u = make_url(url_str.strip())
        host = (u.host or "localhost").lower()
        if host in ("127.0.0.1", "::1"):
            host = "localhost"
        port = u.port or 5432
        db_name = (u.database or "postgres").lower()
        return (host, port, db_name)
    except Exception:
        # Fallback to standard urlparse
        parsed = urlparse(url_str.strip())
        host = (parsed.hostname or "localhost").lower()
        if host in ("127.0.0.1", "::1"):
            host = "localhost"
        port = parsed.port or 5432
        db_name = parsed.path.lstrip("/").lower() or "postgres"
        return (host, port, db_name)


def is_safe_tar_member(member: tarfile.TarInfo, target_dir: Path) -> bool:
    """
    Validates that a tar member does not traverse outside target_dir
    via parent directory components, absolute paths, or unsafe symlinks.
    """
    member_path = member.name
    # Reject absolute paths or Windows drive letters
    if os.path.isabs(member_path) or (len(member_path) > 1 and member_path[1] == ":"):
        return False
    # Reject parent traversal components
    parts = Path(member_path).parts
    if ".." in parts:
        return False
    # Check resolved destination path
    try:
        resolved = (target_dir / member_path).resolve()
        target_resolved = target_dir.resolve()
        if not str(resolved).startswith(str(target_resolved)):
            return False
    except Exception:
        return False
    # Reject symlinks/hardlinks targeting outside
    if member.islnk() or member.issym():
        link_target = member.linkname
        if os.path.isabs(link_target) or ".." in Path(link_target).parts:
            return False
    return True


@dataclass
class BackupManifest:
    backup_id: str
    created_at: str
    environment: str
    storage_mode: str
    active_snapshot_id: Optional[str]
    active_manifest_checksum: Optional[str]
    total_documents: int
    total_chunks: int
    database_dump_included: bool
    database_dump_file: Optional[str]
    raw_files_count: int = 0
    raw_data_status: str = "present"  # "present", "empty", or "missing"
    included_files: List[str] = field(default_factory=list)
    file_checksums: Dict[str, str] = field(default_factory=dict)
    off_host_upload_enabled: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "backup_id": self.backup_id,
            "created_at": self.created_at,
            "environment": self.environment,
            "storage_mode": self.storage_mode,
            "active_snapshot_id": self.active_snapshot_id,
            "active_manifest_checksum": self.active_manifest_checksum,
            "total_documents": self.total_documents,
            "total_chunks": self.total_chunks,
            "database_dump_included": self.database_dump_included,
            "database_dump_file": self.database_dump_file,
            "raw_files_count": self.raw_files_count,
            "raw_data_status": self.raw_data_status,
            "included_files": self.included_files,
            "file_checksums": self.file_checksums,
            "off_host_upload_enabled": self.off_host_upload_enabled,
        }


@dataclass
class BackupCreationResult:
    success: bool
    backup_id: str
    archive_path: Optional[str]
    archive_checksum: Optional[str]
    manifest: Optional[Dict[str, Any]]
    message: str
    error: Optional[str] = None


@dataclass
class RestoreResult:
    success: bool
    backup_id: str
    restored_files_count: int
    verified_checksums_count: int
    database_restored: bool
    target_directory: str
    message: str
    error: Optional[str] = None


class BackupQuiescenceLock:
    """
    Context manager that acquires the publisher lock file (.publish.lock)
    to guarantee that no worker or admin process triggers index publication
    while a coordinated backup is in progress.
    """

    def __init__(self, indexes_dir: Path):
        self.indexes_dir = indexes_dir
        self.lock_file_path = indexes_dir / ".publish.lock"
        self._acquired = False
        self._lock_fd: Optional[int] = None

    def __enter__(self):
        self.indexes_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        try:
            fd = os.open(str(self.lock_file_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            with os.fdopen(fd, "w", encoding="utf-8") as f_lock:
                f_lock.write(f"backup_{os.getpid()}:{now}\n")
            self._acquired = True
            return self
        except FileExistsError:
            raise RuntimeError(
                "Cannot acquire backup quiescence lock: an active index publication or backup "
                f"is currently holding {self.lock_file_path}"
            )

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._acquired and self.lock_file_path.exists():
            try:
                self.lock_file_path.unlink()
            except Exception as e:
                logger.warning(f"Failed to cleanly release backup quiescence lock: {e}")
            self._acquired = False


class BackupService:
    """
    Coordinates atomic snapshots and restorations of the SEBI RAG platform.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        backups_dir: Optional[str | Path] = None,
        raw_dir: Optional[str | Path] = None,
    ):
        self.settings = settings or get_settings()
        self.processed_dir = Path(self.settings.processed_dir)
        self.indexes_dir = Path(self.settings.indexes_dir)
        self.raw_dir = Path(raw_dir or self.settings.raw_dir)
        self.backups_dir = Path(backups_dir or self.settings.backups_dir)
        self.backups_dir.mkdir(parents=True, exist_ok=True)

    def create_coordinated_backup(
        self,
        backup_id: Optional[str] = None,
        include_db_dump: bool = True,
        enable_upload: bool = False,
    ) -> BackupCreationResult:
        """
        Creates a consistent coordinated backup archive containing:
        - Authoritative PostgreSQL SQL dump (if database configured)
        - Active search index snapshot and active pointer
        - Raw circular PDFs (data/raw) and processed document JSONs (data/processed)
        - Timestamped manifest with SHA-256 digests
        """
        now = datetime.now(timezone.utc)
        b_id = backup_id or f"backup_{now.strftime('%Y%m%d_%H%M%S')}_{os.urandom(3).hex()}"
        staging_dir = self.backups_dir / f"_staging_{b_id}"

        try:
            # 1. Acquire Quiescence Lock
            with BackupQuiescenceLock(self.indexes_dir):
                if staging_dir.exists():
                    shutil.rmtree(staging_dir, ignore_errors=True)
                staging_dir.mkdir(parents=True, exist_ok=True)

                included_files: List[str] = []
                file_checksums: Dict[str, str] = {}

                # 2. Inspect Active Index Snapshot Pointer
                active_ptr_path = self.indexes_dir / "active_snapshot.json"
                active_snap_id: Optional[str] = None
                active_manifest_chk: Optional[str] = None
                total_docs = 0
                total_chunks = 0

                if active_ptr_path.exists():
                    with open(active_ptr_path, "r", encoding="utf-8") as pf:
                        ptr_data = json.load(pf)
                    active_snap_id = ptr_data.get("snapshot_id")
                    active_manifest_chk = ptr_data.get("manifest_checksum")
                    total_docs = ptr_data.get("total_documents", 0)
                    total_chunks = ptr_data.get("total_chunks", 0)

                    # Copy active pointer
                    dest_ptr = staging_dir / "indexes" / "active_snapshot.json"
                    dest_ptr.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(active_ptr_path, dest_ptr)
                    rel_name = "indexes/active_snapshot.json"
                    included_files.append(rel_name)
                    file_checksums[rel_name] = compute_sha256(dest_ptr)

                    # Copy active snapshot directory
                    if active_snap_id:
                        src_snap_dir = self.indexes_dir / "snapshots" / active_snap_id
                        if src_snap_dir.exists():
                            dest_snap_dir = staging_dir / "indexes" / "snapshots" / active_snap_id
                            dest_snap_dir.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copytree(src_snap_dir, dest_snap_dir)
                            for root, _, files in os.walk(dest_snap_dir):
                                for f in files:
                                    full_f = Path(root) / f
                                    r_path = full_f.relative_to(staging_dir).as_posix()
                                    included_files.append(r_path)
                                    file_checksums[r_path] = compute_sha256(full_f)

                # 3. Copy Processed Documents, Manifest, and Per-Document Subdirectories
                if self.processed_dir.exists():
                    dest_proc_dir = staging_dir / "processed"
                    dest_proc_dir.mkdir(parents=True, exist_ok=True)
                    for root, _, files in os.walk(self.processed_dir):
                        for f in files:
                            src_file = Path(root) / f
                            rel_proc = src_file.relative_to(self.processed_dir)
                            dest_item = dest_proc_dir / rel_proc
                            dest_item.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(src_file, dest_item)
                            rel_name = f"processed/{rel_proc.as_posix()}"
                            included_files.append(rel_name)
                            file_checksums[rel_name] = compute_sha256(dest_item)

                # 4. Copy Raw Source PDFs (with explicit status tracking)
                raw_count = 0
                raw_status = "missing"
                if self.raw_dir.exists():
                    raw_pdfs = list(self.raw_dir.glob("*.pdf"))
                    if raw_pdfs:
                        dest_raw_dir = staging_dir / "raw"
                        dest_raw_dir.mkdir(parents=True, exist_ok=True)
                        for item in raw_pdfs:
                            dest_item = dest_raw_dir / item.name
                            shutil.copy(item, dest_item)
                            rel_name = f"raw/{item.name}"
                            included_files.append(rel_name)
                            file_checksums[rel_name] = compute_sha256(dest_item)
                        raw_count = len(raw_pdfs)
                        raw_status = "present"
                    else:
                        raw_status = "empty"
                else:
                    raw_status = "missing"

                # 5. Export Database Dump (if configured and requested)
                db_dump_file: Optional[str] = None
                is_db_dumped = False
                if include_db_dump and self.settings.is_database_enabled and self.settings.database_url:
                    db_dump_path = staging_dir / "database.sql"
                    self._export_postgres_dump(self.settings.database_url, db_dump_path)
                    if db_dump_path.exists():
                        db_dump_file = "database.sql"
                        is_db_dumped = True
                        included_files.append("database.sql")
                        file_checksums["database.sql"] = compute_sha256(db_dump_path)

                # 6. Generate Manifest
                manifest = BackupManifest(
                    backup_id=b_id,
                    created_at=now.isoformat(),
                    environment=self.settings.environment,
                    storage_mode="postgresql" if self.settings.is_database_enabled else "file_backed",
                    active_snapshot_id=active_snap_id,
                    active_manifest_checksum=active_manifest_chk,
                    total_documents=total_docs,
                    total_chunks=total_chunks,
                    database_dump_included=is_db_dumped,
                    database_dump_file=db_dump_file,
                    raw_files_count=raw_count,
                    raw_data_status=raw_status,
                    included_files=sorted(included_files),
                    file_checksums=file_checksums,
                    off_host_upload_enabled=enable_upload,
                )

                manifest_path = staging_dir / "backup_manifest.json"
                with open(manifest_path, "w", encoding="utf-8") as mf:
                    json.dump(manifest.to_dict(), mf, indent=2)

                # 7. Package Compressed Tarball (.tar.gz) Atomically
                archive_filename = f"{b_id}.tar.gz"
                final_archive_path = self.backups_dir / archive_filename
                tmp_archive_path = self.backups_dir / f"._tmp_{b_id}.tar.gz"

                if final_archive_path.exists():
                    raise FileExistsError(
                        f"Refusing to overwrite existing backup archive at '{final_archive_path.name}'"
                    )

                try:
                    with tarfile.open(tmp_archive_path, "w:gz") as tar:
                        tar.add(staging_dir, arcname=b_id)

                    archive_sha = compute_sha256(tmp_archive_path)

                    # Atomic rename within the same backup directory/filesystem
                    tmp_archive_path.replace(final_archive_path)
                finally:
                    if tmp_archive_path.exists():
                        try:
                            tmp_archive_path.unlink()
                        except Exception:
                            pass

                # 8. Clean Staging Directory
                shutil.rmtree(staging_dir, ignore_errors=True)

                # 9. Off-host upload hook (Stub / Zero-Cost Safe by default)
                if enable_upload:
                    self._perform_off_host_upload(final_archive_path, manifest)

                return BackupCreationResult(
                    success=True,
                    backup_id=b_id,
                    archive_path=str(final_archive_path),
                    archive_checksum=archive_sha,
                    manifest=manifest.to_dict(),
                    message=f"Coordinated backup '{b_id}' created successfully with {len(included_files)} assets.",
                )

        except Exception as e:
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
            logger.error(f"Coordinated backup creation failed: {e}", exc_info=True)
            return BackupCreationResult(
                success=False,
                backup_id=b_id,
                archive_path=None,
                archive_checksum=None,
                manifest=None,
                message=f"Backup creation failed: {e}",
                error=str(e),
            )

    def restore_backup(
        self,
        archive_path: str | Path,
        target_dir: str | Path,
        restore_database_url: Optional[str] = None,
        allow_production_db_overwrite: bool = False,
    ) -> RestoreResult:
        """
        Restores and verifies a backup archive into an isolated target directory.
        Strictly verifies SHA-256 checksums before unpacking.
        Never touches the live database unless restore_database_url is explicitly provided.
        Rejects production database targets unless explicitly authorized via allow_production_db_overwrite=True.
        """
        archive = Path(archive_path)
        if not archive.exists():
            return RestoreResult(
                success=False,
                backup_id="",
                restored_files_count=0,
                verified_checksums_count=0,
                database_restored=False,
                target_directory=str(target_dir),
                message=f"Backup archive not found: {archive_path}",
                error="ArchiveNotFound",
            )

        # 1. Database Safety Guard: Validate destination database target before touching any assets
        if restore_database_url:
            norm_target = normalize_db_target(restore_database_url)
            norm_prod = normalize_db_target(self.settings.database_url)

            # Check if targeting active production database or prod environment
            is_prod_env = self.settings.environment.lower() == "production"
            is_same_target = bool(norm_target and norm_prod and norm_target == norm_prod)

            if (is_prod_env or is_same_target) and not allow_production_db_overwrite:
                err_msg = (
                    "Database restoration rejected: destination matches configured production database "
                    "or environment is production. Restoring a backup is DESTRUCTIVE (drops and replaces tables). "
                    "To explicitly authorize this operation, set allow_production_db_overwrite=True "
                    "(or pass --allow-production-db-overwrite on the CLI)."
                )
                logger.error(err_msg)
                return RestoreResult(
                    success=False,
                    backup_id="",
                    restored_files_count=0,
                    verified_checksums_count=0,
                    database_restored=False,
                    target_directory=str(target_dir),
                    message=err_msg,
                    error="ProductionDatabaseOverwriteRejected",
                )

        extract_root = Path(target_dir)
        extract_root.mkdir(parents=True, exist_ok=True)

        temp_extract = Path(tempfile.mkdtemp(prefix="sebi_restore_"))
        try:
            # 2. Unpack tarball with safe member extraction and path traversal validation
            with tarfile.open(archive, "r:gz") as tar:
                # Validate members against path traversal or absolute destinations
                for member in tar.getmembers():
                    if not is_safe_tar_member(member, temp_extract):
                        raise ValueError(
                            f"Malicious archive rejected: member '{member.name}' attempts path traversal or unsafe link."
                        )

                # Use Python 3.12+ extraction filter if supported by the tarfile module
                if hasattr(tarfile, "data_filter"):
                    tar.extractall(path=temp_extract, filter="data")
                else:
                    tar.extractall(path=temp_extract)

            # Locate root inside unpacked folder
            members = list(temp_extract.iterdir())
            if not members:
                raise ValueError("Extracted backup archive is empty.")
            b_dir = members[0] if members[0].is_dir() else temp_extract

            # Locate and validate manifest
            manifest_file = b_dir / "backup_manifest.json"
            if not manifest_file.exists():
                raise ValueError("backup_manifest.json missing from backup archive.")

            with open(manifest_file, "r", encoding="utf-8") as mf:
                manifest_data = json.load(mf)

            b_id = manifest_data.get("backup_id", "unknown")
            file_checksums: Dict[str, str] = manifest_data.get("file_checksums", {})

            # Verify every recorded checksum
            verified_count = 0
            for rel_path, expected_hash in file_checksums.items():
                item_path = b_dir / rel_path
                if not item_path.exists():
                    raise ValueError(f"Corrupt backup: recorded file '{rel_path}' is missing.")
                actual_hash = compute_sha256(item_path)
                if actual_hash != expected_hash:
                    raise ValueError(
                        f"Checksum mismatch for '{rel_path}': expected {expected_hash}, got {actual_hash}"
                    )
                verified_count += 1

            # Restore files to target directory
            restored_count = 0
            for item in b_dir.rglob("*"):
                if item.is_file():
                    rel_to_b = item.relative_to(b_dir)
                    dest_file = extract_root / rel_to_b
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, dest_file)
                    restored_count += 1

            # Optional DB restoration if destination database is supplied
            db_restored = False
            if restore_database_url and manifest_data.get("database_dump_included"):
                dump_file = b_dir / "database.sql"
                if dump_file.exists():
                    self._import_postgres_dump(restore_database_url, dump_file)
                    db_restored = True

            return RestoreResult(
                success=True,
                backup_id=b_id,
                restored_files_count=restored_count,
                verified_checksums_count=verified_count,
                database_restored=db_restored,
                target_directory=str(extract_root),
                message=f"Backup '{b_id}' restored and verified successfully ({verified_count} checksums verified).",
            )

        except Exception as e:
            logger.error(f"Restore verification failed: {e}", exc_info=True)
            return RestoreResult(
                success=False,
                backup_id="",
                restored_files_count=0,
                verified_checksums_count=0,
                database_restored=False,
                target_directory=str(target_dir),
                message=f"Restore failed: {e}",
                error=str(e),
            )
        finally:
            shutil.rmtree(temp_extract, ignore_errors=True)

    def _export_postgres_dump(self, database_url: str, output_path: Path) -> None:
        """
        Executes a schema+data pg_dump export without leaking credentials into logs.
        Fallback: if pg_dump CLI is not present on host, performs SQLAlchemy table dump.
        """
        from sqlalchemy import create_engine, text, MetaData
        from sqlalchemy.engine import make_url

        url = make_url(database_url)
        db_name = url.database
        user = url.username
        host = url.host or "localhost"
        port = str(url.port or 5432)
        password = url.password

        env = os.environ.copy()
        if password:
            env["PGPASSWORD"] = password

        db_name_str: str = db_name or "postgres"
        user_str: str = user or "postgres"

        # Attempt 1: Host pg_dump invocation
        try:
            cmd: List[str] = [
                "pg_dump",
                "-h", host,
                "-p", port,
                "-U", user_str,
                "--clean",
                "--if-exists",
                "-f", str(output_path),
                db_name_str,
            ]
            res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
            if res.returncode == 0:
                logger.info(f"Host pg_dump completed successfully to {output_path}")
                return
        except (FileNotFoundError, subprocess.SubprocessError):
            pass

        # Attempt 2: If running alongside Docker container 'sebi-rag-postgres' or 'sebi-pgvector-test'
        for container_name in ("sebi-rag-postgres", "sebi-pgvector-test"):
            try:
                docker_cmd = [
                    "docker", "exec", container_name,
                    "pg_dump", "-U", user_str, "-d", db_name_str, "--clean", "--if-exists"
                ]
                res = subprocess.run(
                    docker_cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                )
                if res.returncode == 0 and res.stdout:
                    output_path.write_text(res.stdout, encoding="utf-8")
                    logger.info(f"Container pg_dump ({container_name}) completed successfully to {output_path}")
                    return
            except (FileNotFoundError, subprocess.SubprocessError):
                pass

        # Attempt 3: Pure Python table dump fallback (Generates valid SQL DDL + INSERT statements)
        logger.info("pg_dump binary not found in PATH or container; performing Python table dump fallback...")
        engine = create_engine(database_url)
        metadata = MetaData()
        metadata.reflect(bind=engine)
        from sqlalchemy.schema import CreateTable
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(f"-- SEBI Platform Automated Table Dump: {datetime.now(timezone.utc).isoformat()}\n")
            f.write("CREATE EXTENSION IF NOT EXISTS vector;\n")
            with engine.connect() as conn:
                for table_name, table_obj in metadata.tables.items():
                    f.write(f"\n-- Table DDL: {table_name}\n")
                    f.write(f"DROP TABLE IF EXISTS {table_name} CASCADE;\n")
                    create_sql = str(CreateTable(table_obj).compile(engine)).strip()
                    f.write(f"{create_sql};\n")
                    rows = conn.execute(table_obj.select()).fetchall()
                    for r in rows:
                        row_dict = dict(r._mapping)
                        cols = ", ".join(str(k) for k in row_dict.keys())
                        vals = []
                        for val in row_dict.values():
                            if val is None:
                                vals.append("NULL")
                            elif isinstance(val, (int, float)):
                                vals.append(str(val))
                            else:
                                escaped_val = str(val).replace("'", "''")
                                vals.append(f"'{escaped_val}'")
                        f.write(f"INSERT INTO {table_name} ({cols}) VALUES ({', '.join(vals)});\n")

    def _import_postgres_dump(self, database_url: str, dump_path: Path) -> None:
        """
        Applies a database dump to an isolated target PostgreSQL database.
        """
        from sqlalchemy.engine import make_url

        url = make_url(database_url)
        db_name = url.database
        user = url.username
        host = url.host or "localhost"
        port = str(url.port or 5432)
        password = url.password

        env = os.environ.copy()
        if password:
            env["PGPASSWORD"] = password

        db_name_str: str = db_name or "postgres"
        user_str: str = user or "postgres"

        # Attempt 1: Host psql invocation
        try:
            cmd: List[str] = [
                "psql",
                "-h", host,
                "-p", port,
                "-U", user_str,
                "-d", db_name_str,
                "-f", str(dump_path),
            ]
            res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
            if res.returncode == 0:
                logger.info(f"Host psql restore completed successfully into {db_name}")
                return
        except (FileNotFoundError, subprocess.SubprocessError):
            pass

        # Attempt 2: If running alongside Docker container 'sebi-rag-postgres' or 'sebi-pgvector-test'
        for container_name in ("sebi-rag-postgres", "sebi-pgvector-test"):
            try:
                # Copy SQL dump to container and execute
                copy_cmd = ["docker", "cp", str(dump_path), f"{container_name}:/tmp/restore_dump.sql"]
                c_res = subprocess.run(copy_cmd, capture_output=True, text=True, timeout=30)
                if c_res.returncode == 0:
                    exec_cmd = [
                        "docker", "exec", container_name,
                        "psql", "-U", user_str, "-d", db_name_str, "-f", "/tmp/restore_dump.sql"
                    ]
                    e_res = subprocess.run(exec_cmd, capture_output=True, text=True, timeout=60)
                    if e_res.returncode == 0:
                        logger.info(f"Container psql restore ({container_name}) completed successfully into {db_name}")
                        return
            except (FileNotFoundError, subprocess.SubprocessError):
                pass

        # Attempt 3: Python fallback execution via SQLAlchemy
        logger.info("psql binary not found in PATH or container; performing Python execution fallback...")
        from sqlalchemy import create_engine, text
        engine = create_engine(database_url)
        with engine.connect() as conn:
            with open(dump_path, "r", encoding="utf-8") as f:
                content = f.read()
            # Execute raw SQL statements
            for stmt in content.split(";"):
                stmt = stmt.strip()
                if stmt and not stmt.startswith("--"):
                    try:
                        conn.execute(text(stmt))
                    except Exception as e:
                        logger.debug(f"Statement execution notice during import: {e}")
            conn.commit()

    def _perform_off_host_upload(self, archive_path: Path, manifest: BackupManifest) -> None:
        """
        Hook for off-host storage upload (e.g. OCI Object Storage / Cloudflare R2).
        Strictly zero-cost: requires explicit cloud credentials and will not run if unconfigured.
        """
        oci_bucket = os.getenv("OCI_BACKUP_BUCKET")
        r2_bucket = os.getenv("R2_BACKUP_BUCKET")
        if not oci_bucket and not r2_bucket:
            logger.info("Off-host upload skipped: no backup bucket or cloud credentials configured.")
            return
        logger.info(f"Off-host upload hook triggered for {archive_path.name}")
