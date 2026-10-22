"""
Manifest-driven ingestion runner for Phase 1.

Reads declarative collection manifests from data/manifests/{collection_id}.json,
safely resolves repository-relative PDF paths with directory traversal guards,
executes deterministic text extraction, chunking, and persistence via IngestionService,
and triggers local atomic search index snapshot publication via IndexPublisher.
"""
import json
import logging
import os
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from pydantic import ValidationError

from app.schemas.manifest import (
    CollectionManifest,
    ManifestDocumentItem,
    ManifestIngestionOutcome,
    ManifestIngestionReport,
)
from app.ingestion.service import IngestionService, IngestionResult
from app.retrieval.publisher import IndexPublisher
from app.persistence.file_repo import FileStorageRepositoryFactory

logger = logging.getLogger(__name__)


class ManifestSecurityError(ValueError):
    """Raised when a manifest PDF path escapes authorized data roots or violates security."""
    pass


class ManifestValidationError(ValueError):
    """Raised when manifest structure, required fields, or document IDs are invalid."""
    pass


def resolve_and_validate_pdf_path(
    pdf_path_str: str,
    repo_root: Path,
    approved_roots: Optional[List[Path]] = None,
) -> Path:
    """
    Safely resolve a repository-relative or corpus-root-relative PDF path.
    Prevents directory traversal (e.g. ../../) and ensures the target file
    is strictly contained within approved data roots.
    """
    repo_root = repo_root.resolve()
    clean_str = pdf_path_str.strip().replace("\\", "/")

    # Reject null bytes or empty strings
    if "\0" in clean_str or not clean_str:
        raise ManifestSecurityError(f"Invalid characters in PDF path: '{pdf_path_str}'")

    # If approved_roots not provided, default to repo_root / "data"
    if approved_roots is None:
        approved_roots = [
            (repo_root / "data" / "raw").resolve(),
            (repo_root / "data").resolve(),
        ]
    else:
        approved_roots = [r.resolve() for r in approved_roots]

    # Resolve against repo_root
    raw_target = Path(clean_str)
    if raw_target.is_absolute():
        resolved_path = raw_target.resolve()
    else:
        resolved_path = (repo_root / raw_target).resolve()

    # Check path containment against approved roots
    is_contained = any(
        resolved_path == app_root or app_root in resolved_path.parents
        for app_root in approved_roots
    )

    if not is_contained:
        raise ManifestSecurityError(
            f"Path traversal or unauthorized path detected: '{pdf_path_str}' "
            f"resolves to '{resolved_path}' which is outside approved data roots: "
            f"{[str(r) for r in approved_roots]}"
        )

    return resolved_path


def load_and_validate_manifest(manifest_path: str | Path) -> CollectionManifest:
    """
    Load and parse a collection manifest JSON file.
    Validates Pydantic schema and duplicate document IDs.
    """
    path = Path(manifest_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Manifest file does not exist: {manifest_path}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
    except json.JSONDecodeError as e:
        raise ManifestValidationError(f"Malformed JSON in manifest '{path.name}': {e}") from e
    except Exception as e:
        raise ManifestValidationError(f"Failed to read manifest '{path.name}': {e}") from e

    try:
        manifest = CollectionManifest(**raw_data)
    except ValidationError as e:
        raise ManifestValidationError(f"Schema validation failed for manifest '{path.name}': {e}") from e

    # Check for duplicate document IDs within the manifest
    seen_ids = set()
    duplicates = []
    for doc in manifest.documents:
        if doc.document_id in seen_ids:
            duplicates.append(doc.document_id)
        seen_ids.add(doc.document_id)

    if duplicates:
        raise ManifestValidationError(
            f"Duplicate document_id(s) declared in manifest '{path.name}': {sorted(set(duplicates))}"
        )

    return manifest


class ManifestIngestionRunner:
    """
    Executes manifest-driven ingestion for a collection:
    1. Validates manifest structure and PDF file paths.
    2. Ingests each document via IngestionService (SHA-256 deduplication, chunking, atomic JSON output).
    3. Triggers IndexPublisher to build and publish a local search index snapshot.
    """

    def __init__(
        self,
        repo_root: Optional[str | Path] = None,
        processed_dir: Optional[str | Path] = None,
        indexes_dir: Optional[str | Path] = None,
        ingestion_service: Optional[IngestionService] = None,
        approved_roots: Optional[List[Path]] = None,
    ):
        if repo_root is None:
            # Default to repo root relative to backend/app/ingestion/manifest_runner.py -> 4 levels up
            self.repo_root = Path(__file__).resolve().parent.parent.parent.parent
        else:
            self.repo_root = Path(repo_root).resolve()

        self.processed_dir = Path(processed_dir or (self.repo_root / "data" / "processed")).resolve()
        self.indexes_dir = Path(indexes_dir or (self.repo_root / "data" / "indexes")).resolve()
        self.service = ingestion_service or IngestionService(processed_dir=self.processed_dir)
        self.approved_roots = approved_roots

    def run_manifest(
        self,
        manifest_path: str | Path,
        publish_index: bool = True,
        dry_run_publish: bool = False,
    ) -> ManifestIngestionReport:
        """
        Execute ingestion for all documents declared in the manifest.
        """
        manifest_path = Path(manifest_path).resolve()
        manifest = load_and_validate_manifest(manifest_path)

        # Enforce enabled status: disabled collections cannot be ingested or published
        if not manifest.enabled:
            raise ManifestValidationError(
                f"Collection '{manifest.collection_id}' is disabled (enabled=False). "
                f"Cannot ingest documents or publish search index for a disabled collection."
            )

        report = ManifestIngestionReport(
            manifest_path=str(manifest_path),
            collection_id=manifest.collection_id,
            total_declared=len(manifest.documents),
        )

        for doc_item in manifest.documents:
            # 1. Resolve and validate path
            try:
                resolved_pdf = resolve_and_validate_pdf_path(
                    pdf_path_str=doc_item.pdf_path,
                    repo_root=self.repo_root,
                    approved_roots=self.approved_roots,
                )
            except ManifestSecurityError as e:
                report.error_count += 1
                report.document_outcomes.append(
                    ManifestIngestionOutcome(
                        document_id=doc_item.document_id,
                        status="error",
                        message=f"Path security violation: {e}",
                        pdf_path=doc_item.pdf_path,
                    )
                )
                continue

            if not resolved_pdf.exists():
                report.error_count += 1
                report.document_outcomes.append(
                    ManifestIngestionOutcome(
                        document_id=doc_item.document_id,
                        status="error",
                        message=f"PDF file not found at resolved path: {resolved_pdf}",
                        pdf_path=str(resolved_pdf),
                    )
                )
                continue

            # 2. Build metadata input for IngestionService
            metadata_input: Dict[str, Any] = {
                "document_id": doc_item.document_id,
                "title": doc_item.title,
                "circular_number": doc_item.circular_number,
                "department": doc_item.department,
                "issue_date": doc_item.issue_date,
                "effective_date": doc_item.effective_date,
                "document_type": doc_item.document_type,
                "source_url": doc_item.source_url or f"file://{resolved_pdf.as_posix()}",
                "regulator": manifest.regulator or "SEBI",
                "domain": manifest.domain or "CSCRF",
                "collection_id": manifest.collection_id,
            }
            if doc_item.metadata:
                metadata_input.update(doc_item.metadata)

            # 3. Execute ingestion
            ingest_res: IngestionResult = self.service.ingest_pdf(
                pdf_path=resolved_pdf,
                metadata_input=metadata_input,
            )

            # 4. Optional expected SHA-256 verification
            if ingest_res.status == "success" and doc_item.expected_sha256:
                if ingest_res.sha256.lower() != doc_item.expected_sha256.strip().lower():
                    # Checksum mismatch
                    ingest_res.status = "error"
                    ingest_res.message = (
                        f"Checksum mismatch: expected SHA-256 '{doc_item.expected_sha256}', "
                        f"computed '{ingest_res.sha256}'"
                    )

            if ingest_res.status == "success":
                report.success_count += 1
            elif ingest_res.status == "duplicate":
                report.duplicate_count += 1
            else:
                report.error_count += 1

            report.document_outcomes.append(
                ManifestIngestionOutcome(
                    document_id=doc_item.document_id,
                    status=ingest_res.status,
                    message=ingest_res.message,
                    sha256=ingest_res.sha256,
                    page_count=ingest_res.page_count,
                    chunk_count=ingest_res.chunk_count,
                    pdf_path=str(resolved_pdf),
                )
            )

        # 5. Index Publication if requested and at least one document succeeded or was already present
        if publish_index:
            try:
                factory = FileStorageRepositoryFactory(processed_dir=self.processed_dir)
                publisher = IndexPublisher(
                    repository_factory=factory,
                    indexes_dir=self.indexes_dir,
                    processed_dir=self.processed_dir,
                    collection_id=manifest.collection_id,
                )
                pub_result = publisher.build_and_publish_snapshot(dry_run=dry_run_publish)
                report.index_published = pub_result.success
                report.snapshot_id = pub_result.snapshot_id
                if not pub_result.success:
                    report.publication_error = pub_result.message
            except Exception as e:
                report.index_published = False
                report.publication_error = f"Index publication failed: {str(e)}"

        return report
