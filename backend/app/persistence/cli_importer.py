"""
CLI entrypoint for the SEBI Regulatory Intelligence Platform corpus importer.

Usage:
    python -m app.persistence.cli_importer --dry-run
    python -m app.persistence.cli_importer --database-url "postgresql+psycopg://user:pass@localhost:5432/sebi_rag"
    python -m app.persistence.cli_importer --all-or-nothing
"""
import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Add backend directory to path if running directly
backend_dir = Path(__file__).resolve().parent.parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.persistence.importer import CorpusImporter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("importer_cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Idempotent corpus importer for SEBI Regulatory Intelligence Platform."
    )
    parser.add_argument(
        "--database-url",
        type=str,
        default=None,
        help="PostgreSQL connection string. Defaults to DATABASE_URL environment variable if set.",
    )
    parser.add_argument(
        "--processed-dir",
        type=str,
        default="data/processed",
        help="Path to processed corpus directory containing manifest.json (default: data/processed).",
    )
    parser.add_argument(
        "--indexes-dir",
        type=str,
        default="data/indexes",
        help="Path to indexes directory containing dense_index.npy and meta (default: data/indexes).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate corpus files and simulate import without performing database writes.",
    )
    parser.add_argument(
        "--all-or-nothing",
        action="store_true",
        help="Import entire corpus in a single atomic transaction instead of per-document transactions.",
    )
    parser.add_argument(
        "--json-output",
        action="store_true",
        help="Emit final import statistics summary as JSON.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    # Determine database URL
    db_url = args.database_url
    if not db_url:
        settings = get_settings()
        db_url = settings.database_url

    session_factory = None
    if not args.dry_run:
        if not db_url:
            logger.error(
                "DATABASE_URL is not set and no --database-url provided. "
                "Specify a database URL or run with --dry-run."
            )
            return 1
        try:
            engine = create_engine(db_url, pool_pre_ping=True)
            session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        except Exception as e:
            logger.error("Failed to initialize database engine for URL: %s", e)
            return 1

    importer = CorpusImporter(
        session_factory=session_factory,
        processed_dir=args.processed_dir,
        indexes_dir=args.indexes_dir,
    )

    try:
        stats = importer.import_corpus(
            dry_run=args.dry_run,
            all_or_nothing=args.all_or_nothing,
        )
    except Exception as e:
        logger.error("Corpus import aborted with error: %s", e)
        return 1

    if args.json_output:
        print(json.dumps(stats.to_dict(), indent=2))
    else:
        print("\n" + "=" * 60)
        print("SEBI Regulatory Platform Corpus Import Summary")
        print("=" * 60)
        print(f"Mode: {'DRY RUN (Validation only)' if args.dry_run else 'PERSISTENT IMPORT'}")
        print(f"Transaction Strategy: {'All-or-Nothing' if args.all_or_nothing else 'Per-Document Atomic'}")
        print("-" * 60)
        print(f"Documents: Discovered={stats.documents_discovered}, Validated={stats.documents_validated}, "
              f"Inserted={stats.documents_inserted}, Existing={stats.documents_already_present}, Failed={stats.documents_failed}")
        print(f"Versions:  Inserted={stats.versions_inserted}, Existing={stats.versions_already_present}")
        print(f"Chunks:    Discovered={stats.chunks_discovered}, Validated={stats.chunks_validated}, "
              f"Inserted={stats.chunks_inserted}, Existing={stats.chunks_already_present}, Failed={stats.chunks_failed}")
        print(f"Vectors:   Discovered={stats.embeddings_discovered}, Validated={stats.embeddings_validated}, "
              f"Inserted={stats.embeddings_inserted}, Existing={stats.embeddings_already_present}, Skipped={stats.embeddings_skipped}")
        print(f"Edges:     Discovered={stats.edges_discovered}, Validated={stats.edges_validated}, "
              f"Inserted={stats.edges_inserted}, Existing={stats.edges_already_present}, Failed={stats.edges_failed}")
        print("-" * 60)
        if stats.conflicts:
            print(f"Conflicts ({len(stats.conflicts)}):")
            for c in stats.conflicts:
                print(f"  - {c}")
        if stats.errors:
            print(f"Errors ({len(stats.errors)}):")
            for err in stats.errors:
                print(f"  - {err}")
        if stats.skipped_reasons:
            print(f"Skipped / Informational ({len(stats.skipped_reasons)}):")
            for s in stats.skipped_reasons:
                print(f"  - {s}")
        print("=" * 60 + "\n")

    return 0 if not stats.errors else 1


if __name__ == "__main__":
    sys.exit(main())
