"""
Unit and integration tests for Phase 1 manifest-driven ingestion.
Tests:
- Valid manifest ingestion
- Repository-root-relative PDF paths
- Path traversal and approved-root containment
- Missing files and malformed PDFs
- Invalid manifest fields and duplicate document IDs
- Duplicate SHA-256 content behavior
- Deterministic chunk IDs and preserved provenance
- Local snapshot generation without a database
- Backward compatibility with the existing five-document SEBI ingestion path
"""
import json
import pytest
from pathlib import Path

from app.ingestion.manifest_runner import (
    ManifestIngestionRunner,
    ManifestSecurityError,
    ManifestValidationError,
    resolve_and_validate_pdf_path,
    load_and_validate_manifest,
)
from app.schemas.manifest import CollectionManifest, ManifestDocumentItem
from app.ingestion.service import IngestionService
from tests.fixtures.pdf_builder import create_synthetic_pdf


@pytest.fixture
def test_env(tmp_path: Path):
    """Sets up an isolated test environment with data directories and synthetic PDFs."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)

    data_dir = repo_root / "data"
    raw_dir = data_dir / "raw"
    processed_dir = data_dir / "processed"
    indexes_dir = data_dir / "indexes"
    manifests_dir = data_dir / "manifests"

    for d in (raw_dir, processed_dir, indexes_dir, manifests_dir):
        d.mkdir(parents=True)

    # Create synthetic PDFs
    pdf1 = create_synthetic_pdf(raw_dir / "sample_doc1.pdf", ["Page 1 content for doc 1", "Page 2 content for doc 1"])
    pdf2 = create_synthetic_pdf(raw_dir / "sample_doc2.pdf", ["Page 1 content for doc 2"])

    return {
        "repo_root": repo_root,
        "raw_dir": raw_dir,
        "processed_dir": processed_dir,
        "indexes_dir": indexes_dir,
        "manifests_dir": manifests_dir,
        "pdf1": pdf1,
        "pdf2": pdf2,
    }


def test_valid_manifest_ingestion_and_provenance(test_env):
    """Verify that a valid declarative manifest ingests cleanly with preserved dual-page provenance."""
    manifest_data = {
        "collection_id": "test_collection",
        "name": "Test Collection",
        "description": "Demonstration collection for Phase 1 manifest ingestion.",
        "regulator": "SEBI",
        "domain": "TEST",
        "policy": {
            "policy_type": "general_static",
            "enforce_regulatory_graph": False,
        },
        "documents": [
            {
                "document_id": "test_doc_001",
                "title": "First Test Document",
                "pdf_path": "data/raw/sample_doc1.pdf",
                "circular_number": "TEST/2026/01",
                "issue_date": "2026-01-01",
                "document_type": "guideline",
            },
            {
                "document_id": "test_doc_002",
                "title": "Second Test Document",
                "pdf_path": "data/raw/sample_doc2.pdf",
                "circular_number": "TEST/2026/02",
                "issue_date": "2026-02-01",
                "document_type": "clarification",
            },
        ],
    }
    manifest_file = test_env["manifests_dir"] / "test_collection.json"
    manifest_file.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    runner = ManifestIngestionRunner(
        repo_root=test_env["repo_root"],
        processed_dir=test_env["processed_dir"],
        indexes_dir=test_env["indexes_dir"],
    )

    report = runner.run_manifest(manifest_file, publish_index=False)

    assert report.collection_id == "test_collection"
    assert report.total_declared == 2
    assert report.success_count == 2
    assert report.error_count == 0
    assert report.duplicate_count == 0

    # Verify persisted document structure
    doc1_dir = test_env["processed_dir"] / "test_doc_001"
    assert (doc1_dir / "document.json").exists()
    assert (doc1_dir / "pages.json").exists()
    assert (doc1_dir / "chunks.json").exists()

    # Verify deterministic chunk IDs and dual page provenance
    with open(doc1_dir / "chunks.json", "r", encoding="utf-8") as f:
        chunks = json.load(f)
    assert len(chunks) >= 1
    assert chunks[0]["chunk_id"].startswith("test_doc_001_p1_")
    assert chunks[0]["page_start"] in (1, 2)


def test_disabled_collection_rejected_before_ingestion(test_env):
    """Verify that a disabled collection (enabled=False) is rejected before any extraction or publication."""
    manifest_data = {
        "collection_id": "disabled_collection",
        "name": "Disabled Collection",
        "enabled": False,
        "policy": {
            "policy_type": "general_static",
            "enforce_regulatory_graph": False,
        },
        "documents": [
            {
                "document_id": "disabled_doc",
                "title": "Should Not Ingest",
                "pdf_path": "data/raw/sample_doc1.pdf",
            }
        ],
    }
    manifest_file = test_env["manifests_dir"] / "disabled.json"
    manifest_file.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    runner = ManifestIngestionRunner(
        repo_root=test_env["repo_root"],
        processed_dir=test_env["processed_dir"],
        indexes_dir=test_env["indexes_dir"],
    )

    with pytest.raises(ManifestValidationError) as exc_info:
        runner.run_manifest(manifest_file, publish_index=True)
    assert "is disabled (enabled=False)" in str(exc_info.value)

    # Verify that NO files were created in processed_dir or indexes_dir
    assert not (test_env["processed_dir"] / "disabled_doc").exists()
    assert not (test_env["indexes_dir"] / "active_snapshot.json").exists()


def test_repository_root_relative_path_resolution(test_env):
    """Verify paths are resolved strictly relative to repository root."""
    resolved = resolve_and_validate_pdf_path(
        "data/raw/sample_doc1.pdf",
        repo_root=test_env["repo_root"],
    )
    assert resolved == (test_env["repo_root"] / "data" / "raw" / "sample_doc1.pdf").resolve()


def test_path_traversal_protection(test_env):
    """Verify that path traversal attempts (e.g. ../../) are caught and rejected."""
    # Attempting to escape data root
    with pytest.raises(ManifestSecurityError) as exc_info:
        resolve_and_validate_pdf_path(
            "../../etc/passwd",
            repo_root=test_env["repo_root"],
        )
    assert "Path traversal or unauthorized path detected" in str(exc_info.value)


def test_missing_pdf_file_handling(test_env):
    """Verify that a manifest referencing a non-existent PDF logs an error without crashing."""
    manifest_data = {
        "collection_id": "missing_file_col",
        "name": "Missing File Collection",
        "policy": {
            "policy_type": "general_static",
            "enforce_regulatory_graph": False,
        },
        "documents": [
            {
                "document_id": "nonexistent_doc",
                "title": "Ghost PDF",
                "pdf_path": "data/raw/ghost_file.pdf",
            }
        ],
    }
    manifest_file = test_env["manifests_dir"] / "missing.json"
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    runner = ManifestIngestionRunner(
        repo_root=test_env["repo_root"],
        processed_dir=test_env["processed_dir"],
        indexes_dir=test_env["indexes_dir"],
    )

    report = runner.run_manifest(manifest_file, publish_index=False)
    assert report.total_declared == 1
    assert report.error_count == 1
    assert report.success_count == 0
    assert "PDF file not found" in report.document_outcomes[0].message


def test_malformed_pdf_failure_handling(test_env):
    """Verify that a malformed (non-PDF) file fails gracefully with clear error."""
    corrupt_file = test_env["raw_dir"] / "corrupt.pdf"
    corrupt_file.write_text("NOT A REAL PDF CONTENT", encoding="utf-8")

    manifest_data = {
        "collection_id": "corrupt_col",
        "name": "Corrupt Collection",
        "policy": {
            "policy_type": "general_static",
            "enforce_regulatory_graph": False,
        },
        "documents": [
            {
                "document_id": "corrupt_doc",
                "title": "Corrupt PDF",
                "pdf_path": "data/raw/corrupt.pdf",
            }
        ],
    }
    manifest_file = test_env["manifests_dir"] / "corrupt.json"
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    runner = ManifestIngestionRunner(
        repo_root=test_env["repo_root"],
        processed_dir=test_env["processed_dir"],
        indexes_dir=test_env["indexes_dir"],
    )

    report = runner.run_manifest(manifest_file, publish_index=False)
    assert report.error_count == 1
    assert "PDF extraction failed" in report.document_outcomes[0].message


def test_duplicate_document_ids_in_manifest_rejected(test_env):
    """Verify that duplicate document IDs within the same manifest are rejected at load time."""
    manifest_data = {
        "collection_id": "dup_ids_col",
        "name": "Duplicate IDs Collection",
        "policy": {
            "policy_type": "general_static",
            "enforce_regulatory_graph": False,
        },
        "documents": [
            {
                "document_id": "same_id",
                "title": "First Entry",
                "pdf_path": "data/raw/sample_doc1.pdf",
            },
            {
                "document_id": "same_id",
                "title": "Second Duplicate Entry",
                "pdf_path": "data/raw/sample_doc2.pdf",
            },
        ],
    }
    manifest_file = test_env["manifests_dir"] / "dup_ids.json"
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    with pytest.raises(ManifestValidationError) as exc_info:
        load_and_validate_manifest(manifest_file)
    assert "Duplicate document_id(s) declared" in str(exc_info.value)


def test_duplicate_content_sha256_detection(test_env):
    """Verify duplicate SHA-256 handling: idempotent duplicate vs. conflicting document ID."""
    manifest_data = {
        "collection_id": "dup_content_col",
        "name": "Duplicate Content Collection",
        "policy": {
            "policy_type": "general_static",
            "enforce_regulatory_graph": False,
        },
        "documents": [
            {
                "document_id": "doc_a",
                "title": "Document A",
                "pdf_path": "data/raw/sample_doc1.pdf",
            }
        ],
    }
    manifest_file = test_env["manifests_dir"] / "dup1.json"
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    runner = ManifestIngestionRunner(
        repo_root=test_env["repo_root"],
        processed_dir=test_env["processed_dir"],
        indexes_dir=test_env["indexes_dir"],
    )

    # 1. Ingest doc_a
    rep1 = runner.run_manifest(manifest_file, publish_index=False)
    assert rep1.success_count == 1

    # 2. Re-run identical manifest -> duplicate detected
    rep2 = runner.run_manifest(manifest_file, publish_index=False)
    assert rep2.duplicate_count == 1
    assert "already ingested" in rep2.document_outcomes[0].message

    # 3. Attempt to ingest same PDF bytes under conflicting document_id 'doc_b'
    manifest_conflict = {
        "collection_id": "conflict_col",
        "name": "Conflict Collection",
        "policy": {
            "policy_type": "general_static",
            "enforce_regulatory_graph": False,
        },
        "documents": [
            {
                "document_id": "doc_b",
                "title": "Document B with identical bytes",
                "pdf_path": "data/raw/sample_doc1.pdf",
            }
        ],
    }
    manifest_conflict_file = test_env["manifests_dir"] / "conflict.json"
    manifest_conflict_file.write_text(json.dumps(manifest_conflict), encoding="utf-8")

    rep3 = runner.run_manifest(manifest_conflict_file, publish_index=False)
    assert rep3.error_count == 1
    assert "Duplicate content detected" in rep3.document_outcomes[0].message
    assert "doc_a" in rep3.document_outcomes[0].message


def test_local_snapshot_generation_without_database(test_env):
    """Verify that index snapshot is published atomically to local files without requiring PostgreSQL."""
    manifest_data = {
        "collection_id": "snapshot_col",
        "name": "Snapshot Collection",
        "policy": {
            "policy_type": "general_static",
            "enforce_regulatory_graph": False,
        },
        "documents": [
            {
                "document_id": "snap_doc_01",
                "title": "Snapshot Document",
                "pdf_path": "data/raw/sample_doc1.pdf",
            }
        ],
    }
    manifest_file = test_env["manifests_dir"] / "snapshot_col.json"
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    runner = ManifestIngestionRunner(
        repo_root=test_env["repo_root"],
        processed_dir=test_env["processed_dir"],
        indexes_dir=test_env["indexes_dir"],
    )

    report = runner.run_manifest(manifest_file, publish_index=True)
    assert report.index_published is True
    assert report.snapshot_id is not None

    # Verify active snapshot pointer exists and points to valid directory under collection partition
    active_ptr = test_env["indexes_dir"] / "snapshot_col" / "active_snapshot.json"
    assert active_ptr.exists()

    with open(active_ptr, "r", encoding="utf-8") as f:
        ptr_data = json.load(f)
    assert ptr_data["snapshot_id"] == report.snapshot_id
    assert ptr_data["collection_id"] == "snapshot_col"

    snap_dir = test_env["indexes_dir"] / "snapshot_col" / "snapshots" / report.snapshot_id
    assert (snap_dir / "manifest.json").exists()
    assert (snap_dir / "chunks.json").exists()
    assert (snap_dir / "documents.json").exists()
    assert (snap_dir / "dense_index.npy").exists()


def test_sebi_cscrf_manifest_schema_compatibility():
    """Verify that the official data/manifests/sebi_cscrf.json conforms to CollectionManifest schema."""
    cscrf_manifest_path = Path("data/manifests/sebi_cscrf.json")
    assert cscrf_manifest_path.exists(), "data/manifests/sebi_cscrf.json must exist"

    manifest = load_and_validate_manifest(cscrf_manifest_path)
    assert manifest.collection_id == "sebi_cscrf"
    assert len(manifest.documents) == 5

    expected_ids = [
        "sebi_cscrf_2024_113",
        "sebi_cscrf_2025_045",
        "sebi_cscrf_2025_060",
        "sebi_cscrf_2025_096",
        "sebi_cscrf_2025_119",
    ]
    actual_ids = [d.document_id for d in manifest.documents]
    assert actual_ids == expected_ids

    # Confirm all referenced PDFs exist in data/raw/
    for doc in manifest.documents:
        pdf_file = Path(doc.pdf_path)
        assert pdf_file.exists(), f"PDF file '{doc.pdf_path}' for '{doc.document_id}' must exist"
