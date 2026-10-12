"""
Operational CLI for SEBI Platform Coordinated Backup & Disaster Recovery.

Usage:
    python -m app.backup.cli create [--no-db] [--upload] [--backup-id ID]
    python -m app.backup.cli restore --archive <PATH> --target-dir <DIR> [--restore-db-url URL]
    python -m app.backup.cli verify --archive <PATH>
"""
import argparse
import json
import logging
import os
from pathlib import Path
import sys

# Ensure backend root on sys.path
backend_dir = Path(__file__).resolve().parent.parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.core.config import get_settings
from app.backup.service import BackupService, compute_sha256

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("backup_cli")


def cmd_create(args: argparse.Namespace) -> int:
    settings = get_settings()
    service = BackupService(
        settings=settings,
        backups_dir=args.backups_dir,
        raw_dir=args.raw_dir,
    )
    res = service.create_coordinated_backup(
        backup_id=args.backup_id,
        include_db_dump=not args.no_db,
        enable_upload=args.upload,
    )
    print(json.dumps({
        "success": res.success,
        "backup_id": res.backup_id,
        "archive_path": res.archive_path,
        "archive_checksum": res.archive_checksum,
        "message": res.message,
        "error": res.error,
        "manifest": res.manifest,
    }, indent=2))
    return 0 if res.success else 1


def cmd_restore(args: argparse.Namespace) -> int:
    settings = get_settings()
    service = BackupService(settings=settings)
    res = service.restore_backup(
        archive_path=args.archive,
        target_dir=args.target_dir,
        restore_database_url=args.restore_db_url,
        allow_production_db_overwrite=args.allow_production_db_overwrite,
    )
    print(json.dumps({
        "success": res.success,
        "backup_id": res.backup_id,
        "restored_files_count": res.restored_files_count,
        "verified_checksums_count": res.verified_checksums_count,
        "database_restored": res.database_restored,
        "target_directory": res.target_directory,
        "message": res.message,
        "error": res.error,
    }, indent=2))
    return 0 if res.success else 1


def cmd_verify(args: argparse.Namespace) -> int:
    """Verifies archive checksum and manifest integrity in a temporary directory."""
    import tempfile
    settings = get_settings()
    service = BackupService(settings=settings)
    with tempfile.TemporaryDirectory(prefix="backup_verify_") as tmpdir:
        res = service.restore_backup(
            archive_path=args.archive,
            target_dir=tmpdir,
            restore_database_url=None,
        )
    print(json.dumps({
        "verified": res.success,
        "backup_id": res.backup_id,
        "verified_checksums_count": res.verified_checksums_count,
        "message": res.message,
        "error": res.error,
    }, indent=2))
    return 0 if res.success else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="SEBI Platform Coordinated Backup & Restore CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # create
    p_create = subparsers.add_parser("create", help="Create a coordinated backup archive")
    p_create.add_argument("--backup-id", type=str, default=None, help="Explicit backup ID")
    p_create.add_argument("--backups-dir", type=str, default=None, help="Target backups directory (default: from Settings)")
    p_create.add_argument("--raw-dir", type=str, default=None, help="Path to raw source PDFs (default: from Settings)")
    p_create.add_argument("--no-db", action="store_true", help="Exclude database dump")
    p_create.add_argument("--upload", action="store_true", help="Enable off-host upload hook if configured")
    p_create.set_defaults(func=cmd_create)

    # restore
    p_restore = subparsers.add_parser("restore", help="Restore backup archive into isolated directory")
    p_restore.add_argument("--archive", type=str, required=True, help="Path to .tar.gz archive")
    p_restore.add_argument("--target-dir", type=str, required=True, help="Target isolated directory")
    p_restore.add_argument("--restore-db-url", type=str, default=None, help="Optional test DB URL to restore SQL dump")
    p_restore.add_argument(
        "--allow-production-db-overwrite",
        action="store_true",
        help="DESTRUCTIVE: Explicitly allow overwriting production database or when APP_ENV is production",
    )
    p_restore.set_defaults(func=cmd_restore)

    # verify
    p_verify = subparsers.add_parser("verify", help="Verify backup archive integrity and checksums")
    p_verify.add_argument("--archive", type=str, required=True, help="Path to .tar.gz archive")
    p_verify.set_defaults(func=cmd_verify)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
