"""
Operational CLI for the SEBI Regulatory Intelligence Platform dynamic ingestion pipeline.

Commands:
    register   Register a regulatory PDF URL for background ingestion
    inspect    Inspect state, heartbeat, and failure details of an ingestion job
    list       List recent ingestion jobs with pagination and status filters
    work       Run the background ingestion worker in controlled foreground mode
    readiness  Check database, storage factory, and retrieval readiness

Usage:
    python -m app.ingestion.cli register --url <URL> --idempotency-key <KEY>
    python -m app.ingestion.cli inspect --job-id <UUID>
    python -m app.ingestion.cli list [--status queued] [--limit 20]
    python -m app.ingestion.cli work [--max-jobs 5] [--poll-interval 1.0]
    python -m app.ingestion.cli readiness
"""
import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
from typing import Optional
import uuid

# Add backend directory to sys.path if invoked directly
backend_dir = Path(__file__).resolve().parent.parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.core.config import get_settings
from app.persistence.interfaces import StorageRepositoryFactory
from app.persistence.postgres import PostgresStorageRepositoryFactory
from app.persistence.file_repo import FileStorageRepositoryFactory
from app.schemas.ingestion_job import JobStatus, ProcessingStage
from app.ingestion.orchestrator import IngestionWorker, IngestionOrchestrator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("ingestion_cli")


def get_factory(database_url: Optional[str] = None) -> StorageRepositoryFactory:
    settings = get_settings(force_reload=True)
    db_url = database_url or settings.database_url
    if db_url and db_url.strip():
        return PostgresStorageRepositoryFactory(db_url)
    return FileStorageRepositoryFactory(processed_dir=settings.processed_dir)


def cmd_register(args: argparse.Namespace) -> int:
    """Register a document URL for ingestion."""
    url = args.url.strip()
    key = args.idempotency_key.strip()

    if not url.startswith("https://") and not (args.allow_http and url.startswith("http://")):
        print(f"Error: URL must begin with https:// (or use --allow-http in testing): {url}", file=sys.stderr)
        return 1

    factory = get_factory(args.database_url)
    job_repo = factory.get_ingestion_job_repository()

    existing = job_repo.get_job_by_idempotency_key(key)
    if existing:
        print(f"Job already registered under idempotency key '{key}':")
        print(json.dumps({
            "job_id": str(existing.id),
            "status": existing.status.value,
            "stage": existing.stage.value,
            "document_id": existing.document_id,
            "source_url": existing.source_url,
            "created_at": existing.created_at.isoformat(),
            "duplicate": True,
        }, indent=2))
        return 0

    job = job_repo.create_job(
        source_url=url,
        idempotency_key=key,
        document_id=args.document_id,
        max_attempts=args.max_attempts,
    )

    print("Document successfully registered for ingestion:")
    print(json.dumps({
        "job_id": str(job.id),
        "status": job.status.value,
        "stage": job.stage.value,
        "document_id": job.document_id,
        "source_url": job.source_url,
        "created_at": job.created_at.isoformat(),
        "duplicate": False,
    }, indent=2))
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """Inspect status of an ingestion job."""
    try:
        j_id = uuid.UUID(args.job_id.strip())
    except ValueError:
        print(f"Error: Invalid UUID format for job ID: '{args.job_id}'", file=sys.stderr)
        return 1

    factory = get_factory(args.database_url)
    job_repo = factory.get_ingestion_job_repository()
    job = job_repo.get_job(j_id)

    if not job:
        print(f"Error: Job '{j_id}' not found", file=sys.stderr)
        return 1

    out = {
        "job_id": str(job.id),
        "status": job.status.value,
        "stage": job.stage.value,
        "document_id": job.document_id,
        "sha256": job.sha256,
        "source_url": job.source_url,
        "attempt_count": job.attempt_count,
        "max_attempts": job.max_attempts,
        "heartbeat_at": job.heartbeat_at.isoformat() if job.heartbeat_at else None,
        "next_retry_at": job.next_retry_at.isoformat() if job.next_retry_at else None,
        "error_details": job.error_details,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "is_terminal": job.is_terminal,
        "searchable": False,
        "publication_status": "not_published",
    }
    print(json.dumps(out, indent=2))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """List recent ingestion jobs."""
    status_filter = JobStatus(args.status) if args.status else None
    factory = get_factory(args.database_url)
    job_repo = factory.get_ingestion_job_repository()

    jobs = job_repo.list_jobs(status=status_filter, limit=args.limit, offset=args.offset)
    out = [
        {
            "job_id": str(j.id),
            "status": j.status.value,
            "stage": j.stage.value,
            "document_id": j.document_id,
            "source_url": j.source_url,
            "attempt_count": j.attempt_count,
            "created_at": j.created_at.isoformat(),
        }
        for j in jobs
    ]
    print(json.dumps({"total": len(out), "jobs": out}, indent=2))
    return 0


def cmd_work(args: argparse.Namespace) -> int:
    """Run worker in foreground mode."""
    factory = get_factory(args.database_url)
    worker = IngestionWorker(
        repository_factory=factory,
        worker_id=args.worker_id,
        poll_interval_seconds=args.poll_interval,
        lease_timeout_seconds=args.lease_timeout,
    )

    print(f"Starting ingestion worker '{worker.worker_id}' (max_jobs={args.max_jobs or 'unlimited'})...")
    processed = worker.run_loop(max_jobs=args.max_jobs)
    print(f"Worker completed. Processed {processed} jobs.")
    return 0


def cmd_readiness(args: argparse.Namespace) -> int:
    """Check storage, database, and index readiness."""
    settings = get_settings()
    is_db = settings.is_database_enabled

    status_dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "storage_mode": "postgresql" if is_db else "file_backed",
        "database_configured": is_db,
        "database_url_sanitized": settings.sanitized_database_url,
        "processed_dir": str(Path(settings.processed_dir).resolve()),
        "indexes_dir": str(Path(settings.indexes_dir).resolve()),
    }

    if is_db and settings.database_url:
        try:
            from sqlalchemy import create_engine, text
            engine = create_engine(settings.database_url, pool_pre_ping=True)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1;"))
            status_dict["database_connectivity"] = "connected"
        except Exception as e:
            status_dict["database_connectivity"] = f"error: {e}"
    else:
        manifest = Path(settings.processed_dir) / "manifest.json"
        status_dict["manifest_present"] = manifest.exists()

    print(json.dumps(status_dict, indent=2))
    return 0


def cmd_publish_index(args: argparse.Namespace) -> int:
    """Build and publish candidate search index snapshot."""
    from app.retrieval.publisher import IndexPublisher
    settings = get_settings(force_reload=True)
    factory = get_factory(args.database_url)
    publisher = IndexPublisher(
        repository_factory=factory,
        indexes_dir=settings.indexes_dir,
        processed_dir=settings.processed_dir,
    )

    print(f"Executing search index publication (dry_run={args.dry_run})...")
    res = publisher.build_and_publish_snapshot(dry_run=args.dry_run)
    print(json.dumps(res.model_dump(), indent=2, default=str))
    return 0 if res.success else 1


def cmd_ingest_manifest(args: argparse.Namespace) -> int:
    """Ingest documents declared in a collection manifest (Phase 1)."""
    from app.ingestion.manifest_runner import ManifestIngestionRunner, ManifestValidationError, ManifestSecurityError
    settings = get_settings(force_reload=True)
    manifest_path = Path(args.manifest)

    runner = ManifestIngestionRunner(
        processed_dir=settings.processed_dir,
        indexes_dir=settings.indexes_dir,
    )

    print(f"Executing manifest-driven ingestion for '{manifest_path}' (publish_index={not args.no_publish})...")
    try:
        report = runner.run_manifest(
            manifest_path=manifest_path,
            publish_index=not args.no_publish,
            dry_run_publish=args.dry_run_publish,
        )
    except (ManifestValidationError, ManifestSecurityError, FileNotFoundError) as e:
        print(f"Manifest Ingestion Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        logger.exception("Unexpected error during manifest ingestion")
        print(f"Unexpected Ingestion Error: {e}", file=sys.stderr)
        return 1

    print(json.dumps(report.model_dump(), indent=2, default=str))
    # Return 0 if at least one document succeeded or was duplicate and index published (if requested)
    if report.error_count > 0 and report.success_count == 0 and report.duplicate_count == 0:
        return 1
    if not args.no_publish and not report.index_published:
        return 1
    return 0


def cmd_inspect_index(args: argparse.Namespace) -> int:
    """Inspect active search index snapshot and pending un-indexed documents."""
    from app.retrieval.publisher import IndexPublisher
    settings = get_settings(force_reload=True)
    factory = get_factory(args.database_url)
    publisher = IndexPublisher(
        repository_factory=factory,
        indexes_dir=settings.indexes_dir,
        processed_dir=settings.processed_dir,
    )

    status = publisher.get_index_status()
    print(json.dumps(status.model_dump(), indent=2, default=str))
    return 0


def cmd_edges_list(args: argparse.Namespace) -> int:
    """List regulatory edges with filters and pagination."""
    factory = get_factory(args.database_url)
    edge_repo = factory.get_graph_edge_repository()
    edges = edge_repo.list_edges(
        status=args.status,
        relationship=args.relationship,
        source_document_id=args.source,
        target_document_id=args.target,
        limit=args.limit,
        offset=args.offset,
    )
    print(f"Discovered {len(edges)} matching edges:")
    print(json.dumps([e.model_dump(mode="json") for e in edges], indent=2))
    return 0


def cmd_edges_propose(args: argparse.Namespace) -> int:
    """Propose a candidate regulatory relationship edge."""
    from app.schemas.graph import GraphEdge, RelationshipType, ScopeType, ReviewStatus, ConfidenceLevel, EdgeProvenance
    factory = get_factory(args.database_url)
    edge_repo = factory.get_graph_edge_repository()

    edge_id = args.edge_id or f"edge_{args.source}_{args.relationship.lower()}_{args.target}_{uuid.uuid4().hex[:8]}"
    provisions = [p.strip() for p in args.provisions.split(",")] if args.provisions else []

    edge = GraphEdge(
        edge_id=edge_id,
        source_document_id=args.source,
        target_document_id=args.target,
        relationship=RelationshipType(args.relationship),
        scope=ScopeType(args.scope),
        affected_provisions=provisions,
        evidence_text=args.evidence,
        evidence_page=args.page,
        effective_date=args.effective_date,
        confidence=ConfidenceLevel(args.confidence),
        review_status=ReviewStatus.PROPOSED,
        provenance=EdgeProvenance.USER_PROPOSED,
        reviewed_by=args.proposer,
        review_rationale=args.rationale,
    )

    created = edge_repo.propose_edge(edge)
    print("Edge successfully proposed:")
    print(json.dumps(created.model_dump(mode="json"), indent=2))
    return 0


def cmd_edges_review(args: argparse.Namespace) -> int:
    """Submit a human review decision (verify or reject) for an edge."""
    factory = get_factory(args.database_url)
    edge_repo = factory.get_graph_edge_repository()

    reviewed = edge_repo.review_edge(
        edge_id=args.edge_id,
        decision=args.decision,
        reviewer_id=args.reviewer_id,
        reviewer_name=args.reviewer_name,
        rationale=args.rationale,
    )
    print("Review decision successfully recorded:")
    print(json.dumps(reviewed.model_dump(mode="json"), indent=2))
    return 0


def cmd_edges_revoke(args: argparse.Namespace) -> int:
    """Revoke a previously verified relationship edge."""
    factory = get_factory(args.database_url)
    edge_repo = factory.get_graph_edge_repository()

    revoked = edge_repo.revoke_edge(
        edge_id=args.edge_id,
        reviewer_id=args.reviewer_id,
        reviewer_name=args.reviewer_name,
        rationale=args.rationale,
    )
    print("Edge successfully revoked:")
    print(json.dumps(revoked.model_dump(mode="json"), indent=2))
    return 0


def cmd_edges_reconcile_baseline(args: argparse.Namespace) -> int:
    """Idempotently reconcile baseline CSCRF edges."""
    from app.schemas.graph import GraphEdge
    from app.schemas.cscrf_graph_data import VERIFIED_CSCRF_EDGES_DATA
    factory = get_factory(args.database_url)
    edge_repo = factory.get_graph_edge_repository()

    baseline_edges = [GraphEdge(**e) for e in VERIFIED_CSCRF_EDGES_DATA]
    res = edge_repo.reconcile_baseline_edges(
        baseline_edges=baseline_edges,
        allow_overwrite=args.allow_overwrite,
    )
    print("Baseline edges reconciled:")
    print(json.dumps(res, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Operational CLI for SEBI ingestion pipeline.")
    parser.add_argument("--database-url", type=str, default=None, help="Database URL override.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # register
    p_reg = subparsers.add_parser("register", help="Register a document URL for ingestion.")
    p_reg.add_argument("--url", required=True, help="HTTPS document URL.")
    p_reg.add_argument("--idempotency-key", required=True, help="Client idempotency key.")
    p_reg.add_argument("--document-id", default=None, help="Optional document ID.")
    p_reg.add_argument("--max-attempts", type=int, default=3, help="Max retry attempts.")
    p_reg.add_argument("--allow-http", action="store_true", help="Allow HTTP for local tests.")
    p_reg.set_defaults(func=cmd_register)

    # inspect
    p_insp = subparsers.add_parser("inspect", help="Inspect status of an ingestion job.")
    p_insp.add_argument("--job-id", required=True, help="Job UUID.")
    p_insp.set_defaults(func=cmd_inspect)

    # list
    p_list = subparsers.add_parser("list", help="List recent ingestion jobs.")
    p_list.add_argument("--status", choices=[s.value for s in JobStatus], default=None)
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--offset", type=int, default=0)
    p_list.set_defaults(func=cmd_list)

    # work
    p_work = subparsers.add_parser("work", help="Run background worker in foreground mode.")
    p_work.add_argument("--worker-id", default=None)
    p_work.add_argument("--max-jobs", type=int, default=None)
    p_work.add_argument("--poll-interval", type=float, default=2.0)
    p_work.add_argument("--lease-timeout", type=float, default=60.0)
    p_work.set_defaults(func=cmd_work)

    # readiness
    p_ready = subparsers.add_parser("readiness", help="Check storage and runtime readiness.")
    p_ready.set_defaults(func=cmd_readiness)

    # publish-index
    p_pub = subparsers.add_parser("publish-index", help="Build, validate, and activate search index snapshot.")
    p_pub.add_argument("--dry-run", action="store_true", help="Validate candidate build without activating.")
    p_pub.set_defaults(func=cmd_publish_index)

    # ingest-manifest (Phase 1)
    p_mani = subparsers.add_parser("ingest-manifest", help="Ingest documents declared in a collection manifest (Phase 1).")
    p_mani.add_argument("--manifest", required=True, help="Path to collection manifest JSON file.")
    p_mani.add_argument("--no-publish", action="store_true", help="Skip index snapshot publication after ingestion.")
    p_mani.add_argument("--dry-run-publish", action="store_true", help="Execute dry-run index publication.")
    p_mani.set_defaults(func=cmd_ingest_manifest)

    # inspect-index
    p_idx = subparsers.add_parser("inspect-index", help="Inspect active search index snapshot status.")
    p_idx.set_defaults(func=cmd_inspect_index)

    # edges-list
    p_el = subparsers.add_parser("edges-list", help="List regulatory relationship edges.")
    p_el.add_argument("--status", default=None)
    p_el.add_argument("--relationship", default=None)
    p_el.add_argument("--source", default=None)
    p_el.add_argument("--target", default=None)
    p_el.add_argument("--limit", type=int, default=50)
    p_el.add_argument("--offset", type=int, default=0)
    p_el.set_defaults(func=cmd_edges_list)

    # edges-propose
    p_ep = subparsers.add_parser("edges-propose", help="Propose a candidate relationship edge.")
    p_ep.add_argument("--source", required=True)
    p_ep.add_argument("--target", required=True)
    p_ep.add_argument("--relationship", required=True)
    p_ep.add_argument("--evidence", required=True)
    p_ep.add_argument("--proposer", required=True)
    p_ep.add_argument("--rationale", required=True)
    p_ep.add_argument("--edge-id", default=None)
    p_ep.add_argument("--scope", default="full")
    p_ep.add_argument("--provisions", default=None)
    p_ep.add_argument("--page", type=int, default=None)
    p_ep.add_argument("--effective-date", default=None)
    p_ep.add_argument("--confidence", default="medium")
    p_ep.set_defaults(func=cmd_edges_propose)

    # edges-review
    p_er = subparsers.add_parser("edges-review", help="Submit review decision for edge.")
    p_er.add_argument("--edge-id", required=True)
    p_er.add_argument("--decision", required=True, choices=["verify", "reject"])
    p_er.add_argument("--reviewer-id", required=True)
    p_er.add_argument("--rationale", required=True)
    p_er.add_argument("--reviewer-name", default=None)
    p_er.set_defaults(func=cmd_edges_review)

    # edges-revoke
    p_ev = subparsers.add_parser("edges-revoke", help="Revoke a relationship edge.")
    p_ev.add_argument("--edge-id", required=True)
    p_ev.add_argument("--reviewer-id", required=True)
    p_ev.add_argument("--rationale", required=True)
    p_ev.add_argument("--reviewer-name", default=None)
    p_ev.set_defaults(func=cmd_edges_revoke)

    # edges-reconcile-baseline
    p_rb = subparsers.add_parser("edges-reconcile-baseline", help="Reconcile 9 verified CSCRF baseline edges.")
    p_rb.add_argument("--allow-overwrite", action="store_true")
    p_rb.set_defaults(func=cmd_edges_reconcile_baseline)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
