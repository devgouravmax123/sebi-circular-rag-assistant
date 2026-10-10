import json
import pytest
import threading
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
    """Test re-ingesting the exact same document ID and content reports 'duplicate' status idempotently."""
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


def test_reusing_existing_document_id_with_different_pdf_rejected(temp_service: IngestionService, tmp_path: Path):
    """Test assigning an existing document_id to a DIFFERENT PDF content is rejected without overwriting original."""
    pdf_path_1 = create_synthetic_pdf(tmp_path / "original.pdf", ["Original content of doc A."])
    pdf_path_2 = create_synthetic_pdf(tmp_path / "different.pdf", ["Completely different content bytes."])

    # 1. Ingest original document
    res1 = temp_service.ingest_pdf(pdf_path_1, {
        "document_id": "doc_unique_100",
        "title": "Original Document",
        "source_url": "https://example.gov/original.pdf"
    })
    assert res1.status == "success"
    orig_sha = res1.sha256

    # 2. Attempt to ingest different PDF using the SAME document_id
    res2 = temp_service.ingest_pdf(pdf_path_2, {
        "document_id": "doc_unique_100",
        "title": "Attempted Overwrite Document",
        "source_url": "https://example.gov/different.pdf"
    })
    assert res2.status == "error"
    assert "Document ID collision" in res2.message
    assert "doc_unique_100" in res2.message

    # 3. Verify original document artifacts remain pristine
    doc_file = temp_service.processed_dir / "doc_unique_100" / "document.json"
    with open(doc_file, "r", encoding="utf-8") as f:
        persisted_doc = json.load(f)
    assert persisted_doc["title"] == "Original Document"
    assert persisted_doc["sha256"] == orig_sha


def test_simulated_persistence_failure_leaves_no_corrupted_state(temp_service: IngestionService, tmp_path: Path, monkeypatch):
    """Test that if manifest saving fails after files are staged, partial files and staging are cleaned up."""
    pdf_path = create_synthetic_pdf(tmp_path / "fail_doc.pdf", ["Text for failure test."])

    # Monkeypatch _save_manifest to simulate a disk/I/O error during final commit
    def mock_save_manifest(data):
        raise OSError("Simulated disk full or permission error during manifest save")

    monkeypatch.setattr(temp_service, "_save_manifest", mock_save_manifest)

    res = temp_service.ingest_pdf(pdf_path, {
        "document_id": "doc_will_fail",
        "title": "Fail Doc",
    })

    assert res.status == "error"
    assert "Failed to persist ingested artifacts" in res.message

    # Ensure no partial directory or manifest entry leaked
    assert not (temp_service.processed_dir / "doc_will_fail").exists()
    # Check that staging dirs are cleaned up
    staging_dirs = list(temp_service.processed_dir.glob(".staging_*"))
    assert len(staging_dirs) == 0

    with open(temp_service.manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    assert "doc_will_fail" not in manifest["documents"]


def test_retry_after_failed_ingestion_succeeds(temp_service: IngestionService, tmp_path: Path, monkeypatch):
    """Test that after a transient failure is resolved, retrying ingestion succeeds cleanly."""
    pdf_path = create_synthetic_pdf(tmp_path / "retry_doc.pdf", ["Valid text for retry test."])

    # 1. First attempt fails due to simulated error
    original_save = temp_service._save_manifest
    fail_counter = [0]

    def faulty_save(data):
        if fail_counter[0] == 0:
            fail_counter[0] += 1
            raise IOError("Transient disk error")
        return original_save(data)

    monkeypatch.setattr(temp_service, "_save_manifest", faulty_save)

    res1 = temp_service.ingest_pdf(pdf_path, {
        "document_id": "doc_retry_01",
        "title": "Retry Doc",
    })
    assert res1.status == "error"

    # 2. Retry with healthy service
    res2 = temp_service.ingest_pdf(pdf_path, {
        "document_id": "doc_retry_01",
        "title": "Retry Doc",
    })
    assert res2.status == "success"
    assert res2.document_id == "doc_retry_01"
    assert (temp_service.processed_dir / "doc_retry_01" / "document.json").exists()


def test_multiple_instances_shared_lock_concurrency(tmp_path: Path):
    """Test that two separate IngestionService instances targeting the same directory share a lock and avoid race conditions."""
    processed_dir = tmp_path / "data" / "processed_shared"
    service_a = IngestionService(processed_dir=processed_dir)
    service_b = IngestionService(processed_dir=processed_dir)

    pdf_path = create_synthetic_pdf(tmp_path / "multi_instance.pdf", ["Text for multi instance concurrency test."])
    metadata = {
        "document_id": "doc_shared_lock_01",
        "title": "Multi-Instance Concurrent Doc",
    }

    results = []
    errors = []

    def worker(srv: IngestionService):
        try:
            res = srv.ingest_pdf(pdf_path, metadata)
            results.append(res)
        except Exception as e:
            errors.append(e)

    # Thread 1 uses service_a, Thread 2 uses service_b
    t1 = threading.Thread(target=worker, args=(service_a,))
    t2 = threading.Thread(target=worker, args=(service_b,))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(errors) == 0
    assert len(results) == 2

    statuses = {r.status for r in results}
    assert statuses == {"success", "duplicate"}

    # Manifest and files should be consistent
    with open(service_a.manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    assert "doc_shared_lock_01" in manifest["documents"]
    assert (processed_dir / "doc_shared_lock_01" / "document.json").exists()


def test_orphaned_directory_detection_and_rejection(temp_service: IngestionService, tmp_path: Path):
    """Test that untracked orphaned document directories on disk are detected and never silently overwritten."""
    # 1. Ingest a normal tracked document
    pdf_normal = create_synthetic_pdf(tmp_path / "normal.pdf", ["Normal document content."])
    res_normal = temp_service.ingest_pdf(pdf_normal, {"document_id": "doc_normal", "title": "Normal"})
    assert res_normal.status == "success"

    # 2. Simulate an orphaned directory left on disk after an abrupt crash/power loss
    orphan_dir = temp_service.processed_dir / "doc_orphan_999"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    (orphan_dir / "untracked_file.txt").write_text("important untracked data")

    # 3. Check for orphans
    orphans = temp_service.check_for_orphans()
    assert orphans == ["doc_orphan_999"]

    # 4. Attempt to ingest a new PDF using the orphaned document_id
    pdf_new = create_synthetic_pdf(tmp_path / "new_for_orphan.pdf", ["New PDF text."])
    res_orphan_attempt = temp_service.ingest_pdf(pdf_new, {"document_id": "doc_orphan_999", "title": "Orphan Attempt"})

    assert res_orphan_attempt.status == "error"
    assert "Integrity check failed: Directory 'doc_orphan_999' already exists on disk but is not tracked" in res_orphan_attempt.message

    # 5. Verify untracked directory was NOT deleted or overwritten
    assert (orphan_dir / "untracked_file.txt").exists()
    assert (orphan_dir / "untracked_file.txt").read_text() == "important untracked data"

    # 6. Verify normal tracked document still functions perfectly
    assert (temp_service.processed_dir / "doc_normal" / "document.json").exists()


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
