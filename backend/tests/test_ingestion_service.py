import json
import pytest
from pathlib import Path
from app.ingestion.service import IngestionService
from tests.fixtures.pdf_builder import create_synthetic_pdf


@pytest.fixture
def temp_service(tmp_path: Path):
    """Instantiate IngestionService pointing to an isolated temp directory."""
    processed_dir = tmp_path / "data" / "processed"
    return IngestionService(processed_dir=processed_dir)


def test_successful_pdf_ingestion(temp_service: IngestionService, tmp_path: Path):
    """Test end-to-end ingestion of a multi-page synthetic PDF with atomic persistence."""
    p1 = "First page text regarding initial regulatory compliance framework."
    p2 = "Second page text specifying periodic reporting schedules."
    pdf_path = create_synthetic_pdf(tmp_path / "doc1.pdf", [p1, p2])

    metadata = {
        "document_id": "sebi_synth_001",
        "circular_number": "SEBI/HO/2026/01",
        "title": "Synthetic Circular on Compliance",
        "department": "Integrated Surveillance",
        "issue_date": "2026-01-10",
        "effective_date": "2026-01-15",
        "source_url": "https://example.gov.in/doc1.pdf",
    }

    result = temp_service.ingest_pdf(pdf_path, metadata)

    assert result.status == "success"
    assert result.document_id == "sebi_synth_001"
    assert result.page_count == 2
    assert result.chunk_count >= 1
    assert result.document is not None
    assert result.document.title == "Synthetic Circular on Compliance"

    # Verify persisted files
    doc_dir = temp_service.processed_dir / "sebi_synth_001"
    assert (doc_dir / "document.json").exists()
    assert (doc_dir / "pages.json").exists()
    assert (doc_dir / "chunks.json").exists()
    assert (temp_service.processed_dir / "manifest.json").exists()

    # Verify manifest entry
    with open(temp_service.manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    assert "sebi_synth_001" in manifest["documents"]
    assert result.sha256 in manifest["sha256_index"]


def test_duplicate_pdf_detection_same_doc_id(temp_service: IngestionService, tmp_path: Path):
    """Test re-ingesting the exact same document ID and content reports 'duplicate' status."""
    pdf_path = create_synthetic_pdf(tmp_path / "doc_repeat.pdf", ["Exact duplicate content on page 1."])
    metadata = {
        "document_id": "sebi_repeat_001",
        "title": "Repeat Document",
        "source_url": "https://example.gov.in/repeat.pdf",
    }

    res1 = temp_service.ingest_pdf(pdf_path, metadata)
    assert res1.status == "success"

    # Second ingestion of the same file and same document ID
    res2 = temp_service.ingest_pdf(pdf_path, metadata)
    assert res2.status == "duplicate"
    assert "already ingested" in res2.message
    assert res2.sha256 == res1.sha256


def test_duplicate_sha256_different_doc_id_rejected(temp_service: IngestionService, tmp_path: Path):
    """Test ingesting duplicate PDF bytes under a conflicting document ID is rejected."""
    pdf_path = create_synthetic_pdf(tmp_path / "doc_identical.pdf", ["Identical bytes."])
    
    res1 = temp_service.ingest_pdf(pdf_path, {
        "document_id": "doc_first",
        "title": "First Doc",
        "source_url": "https://example.gov/first.pdf"
    })
    assert res1.status == "success"

    # Ingest same PDF bytes with a DIFFERENT document ID
    res2 = temp_service.ingest_pdf(pdf_path, {
        "document_id": "doc_second_conflict",
        "title": "Second Conflicting Doc",
        "source_url": "https://example.gov/second.pdf"
    })
    assert res2.status == "error"
    assert "Duplicate content detected" in res2.message
    assert "doc_first" in res2.message


def test_missing_file_handling(temp_service: IngestionService, tmp_path: Path):
    """Test handling of nonexistent PDF path without crashing."""
    res = temp_service.ingest_pdf(tmp_path / "nonexistent.pdf", {"document_id": "doc_missing"})
    assert res.status == "error"
    assert "PDF file does not exist" in res.message


def test_corrupted_pdf_handling(temp_service: IngestionService, tmp_path: Path):
    """Test handling of invalid/corrupted PDF without persisting partial records."""
    corrupted_path = tmp_path / "corrupted.pdf"
    corrupted_path.write_bytes(b"INVALID_HEADER_GARBAGE_BYTES")

    res = temp_service.ingest_pdf(corrupted_path, {"document_id": "doc_corrupt"})
    assert res.status == "error"
    assert "PDF extraction failed" in res.message

    # Ensure no partial folder persisted
    assert not (temp_service.processed_dir / "doc_corrupt").exists()


def test_missing_document_id_handling(temp_service: IngestionService, tmp_path: Path):
    """Test rejection when document_id is missing or empty."""
    pdf_path = create_synthetic_pdf(tmp_path / "valid.pdf", ["Valid text."])
    
    res = temp_service.ingest_pdf(pdf_path, {"title": "No Doc ID"})
    assert res.status == "error"
    assert "Missing required 'document_id'" in res.message
