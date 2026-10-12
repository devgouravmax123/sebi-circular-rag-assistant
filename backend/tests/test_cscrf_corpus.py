import json
import pytest
from pathlib import Path
from app.ingestion.service import IngestionService
from app.ingestion.corpus_registry import SEBI_CSCRF_CORPUS
from tests.fixtures.pdf_builder import create_synthetic_pdf


def test_corpus_registry_metadata_structure():
    """Verify registry contains 5 unique, non-empty circular definitions."""
    assert len(SEBI_CSCRF_CORPUS) == 5
    doc_ids = [c["document_id"] for c in SEBI_CSCRF_CORPUS]
    assert len(set(doc_ids)) == 5
    for c in SEBI_CSCRF_CORPUS:
        assert c["document_id"].startswith("sebi_cscrf_")
        assert c["circular_number"].startswith("SEBI/HO/")
        assert len(c["expected_sha256"]) == 64
        assert c["issue_date"] is not None


def test_mock_cscrf_corpus_ingestion(tmp_path: Path):
    """Test simulated ingestion of the 5-document CSCRF corpus using isolated temporary fixtures."""
    processed_dir = tmp_path / "data" / "processed_cscrf"
    service = IngestionService(processed_dir=processed_dir)

    for entry in SEBI_CSCRF_CORPUS:
        # Create a synthetic PDF matching the metadata
        synth_pdf = create_synthetic_pdf(
            tmp_path / entry["filename"],
            [f"Page 1 text for {entry['title']}", f"Page 2 provisions for {entry['circular_number']}"]
        )

        res = service.ingest_pdf(synth_pdf, entry)
        assert res.status == "success"
        assert res.document_id == entry["document_id"]
        assert res.page_count == 2
        assert res.chunk_count >= 1

        # Check deterministic chunk structure
        for chunk in res.chunks:
            assert chunk.document_id == entry["document_id"]
            assert chunk.page_start in (1, 2)
            assert chunk.chunk_id.startswith(entry["document_id"])

    # Verify manifest tracks all 5 documents
    with open(service.manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    assert len(manifest["documents"]) == 5
    for entry in SEBI_CSCRF_CORPUS:
        assert entry["document_id"] in manifest["documents"]
        assert (processed_dir / entry["document_id"] / "document.json").exists()
        assert (processed_dir / entry["document_id"] / "pages.json").exists()
        assert (processed_dir / entry["document_id"] / "chunks.json").exists()


def test_cscrf_duplicate_and_collision_scenarios(tmp_path: Path):
    """Test repeat ingestion and collision protection on corpus definitions."""
    processed_dir = tmp_path / "data" / "processed_test"
    service = IngestionService(processed_dir=processed_dir)

    entry = SEBI_CSCRF_CORPUS[0]
    pdf_path = create_synthetic_pdf(tmp_path / entry["filename"], ["Page text"])

    # 1. Ingest once
    res1 = service.ingest_pdf(pdf_path, entry)
    assert res1.status == "success"

    # 2. Ingest duplicate
    res2 = service.ingest_pdf(pdf_path, entry)
    assert res2.status == "duplicate"

    # 3. Collision: different PDF using same document_id
    pdf_path_diff = create_synthetic_pdf(tmp_path / "diff.pdf", ["Completely different content bytes"])
    res3 = service.ingest_pdf(pdf_path_diff, {**entry, "title": "Conflicting Doc"})
    assert res3.status == "error"
    assert "Document ID collision" in res3.message


def test_pilot_baseline_corpus_integrity():
    """
    Phase 8B.4.1 Regression test:
    Confirms the 5 original CSCRF pilot documents, exactly 168 chunks,
    and 9 verified graph edges remain intact and uncorrupted.
    """
    processed_dir = Path("data/processed")
    assert processed_dir.exists(), "data/processed directory must exist"

    manifest_path = processed_dir / "manifest.json"
    assert manifest_path.exists(), "manifest.json must exist in data/processed"

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    # Exactly 5 documents
    docs = manifest.get("documents", {})
    assert len(docs) == 5, f"Expected exactly 5 pilot documents, found {len(docs)}"

    expected_doc_ids = {
        "sebi_cscrf_2024_113",
        "sebi_cscrf_2025_045",
        "sebi_cscrf_2025_060",
        "sebi_cscrf_2025_096",
        "sebi_cscrf_2025_119",
    }
    assert set(docs.keys()) == expected_doc_ids

    # Exactly 168 chunks across the 5 documents
    total_chunks = 0
    for doc_id in expected_doc_ids:
        chunks_file = processed_dir / doc_id / "chunks.json"
        assert chunks_file.exists(), f"chunks.json missing for {doc_id}"
        with open(chunks_file, "r", encoding="utf-8") as f:
            chunk_list = json.load(f)
        total_chunks += len(chunk_list)

    assert total_chunks == 168, f"Expected exactly 168 chunks, found {total_chunks}"

    # Exactly 9 verified regulatory graph edges in data/processed/cscrf_graph.json
    graph_path = processed_dir / "cscrf_graph.json"
    assert graph_path.exists(), "cscrf_graph.json must exist in data/processed"
    with open(graph_path, "r", encoding="utf-8") as f:
        graph_data = json.load(f)

    # cscrf_graph.json in data/processed is either a list of edges or dict
    if isinstance(graph_data, list):
        edges_count = len(graph_data)
    elif isinstance(graph_data, dict):
        edges_count = len(graph_data.get("edges", graph_data))
    else:
        edges_count = 0

    assert edges_count == 9, f"Expected exactly 9 verified graph edges, found {edges_count}"
