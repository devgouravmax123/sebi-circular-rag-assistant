"""
Standalone Background Ingestion Worker Daemon Entrypoint (Phase 8F.1).

Runs the IngestionWorker in a continuous, supervised loop with:
- Signal handling (SIGINT, SIGTERM) for graceful shutdown.
- Stale lease recovery and exponential backoff retries.
- Exception supervision: single job failures do NOT crash the worker loop.
- Sanitized structured logging (never exposing raw secrets).
- Process exit codes (0 on clean termination, 1 on fatal configuration).

Usage:
    python -m app.ingestion.worker
    python -m app.ingestion.worker --poll-interval 1.5 --lease-timeout 60.0
"""
import argparse
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import signal
import sys
import time
from typing import Optional

# Ensure backend root is on sys.path
backend_dir = Path(__file__).resolve().parent.parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.core.config import get_settings
from app.persistence.interfaces import StorageRepositoryFactory
from app.persistence.postgres import PostgresStorageRepositoryFactory
from app.persistence.file_repo import FileStorageRepositoryFactory
from app.ingestion.orchestrator import IngestionWorker, IngestionOrchestrator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ingestion_worker_daemon")


def get_repository_factory() -> StorageRepositoryFactory:
    settings = get_settings(force_reload=True)
    if settings.is_database_enabled and settings.database_url:
        logger.info(f"Worker connecting to PostgreSQL storage: {settings.sanitized_database_url}")
        return PostgresStorageRepositoryFactory(
            database_url=settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout,
            pool_recycle=settings.db_pool_recycle,
        )
    logger.info(f"Worker operating in file-backed storage mode (processed_dir={settings.processed_dir})")
    return FileStorageRepositoryFactory(processed_dir=settings.processed_dir)


def run_worker_daemon(
    poll_interval: float = 2.0,
    lease_timeout: float = 60.0,
    stale_recovery_interval: float = 30.0,
    max_jobs: Optional[int] = None,
    worker_id: Optional[str] = None,
) -> int:
    """
    Supervised worker loop execution.
    Attaches OS signal handlers for clean termination.
    """
    settings = get_settings()
    logger.info(f"Initializing IngestionWorker in environment '{settings.environment}'...")

    try:
        factory = get_repository_factory()
    except Exception as e:
        logger.critical(f"Fatal error initializing storage repository: {e}")
        return 1

    worker = IngestionWorker(
        repository_factory=factory,
        worker_id=worker_id,
        poll_interval_seconds=poll_interval,
        lease_timeout_seconds=lease_timeout,
        stale_recovery_interval_seconds=stale_recovery_interval,
    )

    # Attach signal handlers for graceful termination
    def handle_signal(sig, frame):
        sig_name = signal.Signals(sig).name
        logger.info(f"Received termination signal {sig_name}. Initiating graceful worker shutdown...")
        worker.stop()

    try:
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
    except (ValueError, AttributeError):
        # In non-main thread environments or specific platforms
        pass

    logger.info(
        f"Ingestion worker '{worker.worker_id}' started. Polling every {poll_interval}s "
        f"(lease_timeout={lease_timeout}s, stale_recovery={stale_recovery_interval}s, max_jobs={max_jobs or 'unlimited'})."
    )

    processed = 0
    try:
        processed = worker.run_loop(max_jobs=max_jobs)
        logger.info(f"Worker loop stopped gracefully. Processed {processed} total job(s).")
        return 0
    except Exception as loop_err:
        logger.error(f"Supervised worker loop encountered unexpected fatal exception: {loop_err}", exc_info=True)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Standalone background ingestion worker daemon.")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Polling interval in seconds (default: 2.0).")
    parser.add_argument("--lease-timeout", type=float, default=60.0, help="Lease timeout in seconds (default: 60.0).")
    parser.add_argument("--stale-recovery-interval", type=float, default=30.0, help="Stale recovery check interval in seconds (default: 30.0).")
    parser.add_argument("--max-jobs", type=int, default=None, help="Stop after processing max jobs (default: run indefinitely).")
    parser.add_argument("--worker-id", type=str, default=None, help="Explicit worker ID identifier.")
    args = parser.parse_args()

    return run_worker_daemon(
        poll_interval=args.poll_interval,
        lease_timeout=args.lease_timeout,
        stale_recovery_interval=args.stale_recovery_interval,
        max_jobs=args.max_jobs,
        worker_id=args.worker_id,
    )


if __name__ == "__main__":
    sys.exit(main())
