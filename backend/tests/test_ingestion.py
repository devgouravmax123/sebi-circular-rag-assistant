import pytest
from pathlib import Path
from app.ingestion.extractor import PDFExtractor, compute_sha256
from app.ingestion.chunker import TextChunker
from app.schemas.document import DocumentRecord, PageContent, ChunkRecord
from tests.fixtures.pdf_builder import create_synthetic_pdf


@pytest.fixture
def tmp_pdf_factory(tmp_path: Path):
    """Fixture factory to build temporary synthetic PDF files."""
    def _create(filename: str, pages_text: list[str]) -> Path:
        pdf_path = tmp_path / filename
        return create_synthetic_pdf(pdf_path, pages_text)
    return _create


def test_schema_validation():
    """Test Pydantic document and chunk model validation."""
    doc = DocumentRecord(
        document_id="doc_synth_001",
        circular_number="SEBI/SYNTH/2026/01",
        title="Synthetic Test Circular on Disclosure Norms",
        department="Corporation Finance",
        issue_date="2026-01-15",
        effective_date="2026-02-01",
        source_url="https://example.gov/synthetic_circular.pdf",
        sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        pdf_path="data/raw/synth_001.pdf",
        extraction_status="pending_review",
    )
    assert doc.document_id == "doc_synth_001"
    assert doc.title == "Synthetic Test Circular on Disclosure Norms"

    chunk = ChunkRecord(
        chunk_id="doc_synth_001_p1_0",
        document_id="doc_synth_001",
        text="Sample paragraph regarding regulatory disclosure requirements.",
        page_start=1,
        page_end=1,
        is_ocr=False,
        chunk_type="body",
        metadata={"word_count": 7},
    )
    assert chunk.page_start == 1
    assert chunk.metadata["word_count"] == 7


def test_pdf_extraction_page_provenance(tmp_pdf_factory):
    """Test text extraction with exact page-level boundaries from synthetic multi-page PDF."""
    p1 = "Paragraph on page one discussing the initial framework for synthetic portfolio entities."
    p2 = "Paragraph on page two detailing compliance deadlines and audit requirements."
    p3 = "Paragraph on page three outlining reporting formats and submission schedules."

    pdf_file = tmp_pdf_factory("sample_3page.pdf", [p1, p2, p3])
    sha256 = compute_sha256(pdf_file)
    assert len(sha256) == 64

    extractor = PDFExtractor(min_char_threshold=15)
    pages = extractor.extract_pages(pdf_file)

    assert len(pages) == 3
    assert pages[0].page_number == 1
    assert "page one discussing the initial framework" in pages[0].text
    assert pages[0].extraction_quality == "ok"

    assert pages[1].page_number == 2
    assert "compliance deadlines and audit requirements" in pages[1].text

    assert pages[2].page_number == 3
    assert "reporting formats and submission schedules" in pages[2].text


def test_pdf_extraction_empty_and_sparse_pages(tmp_pdf_factory):
    """Test quality flags for empty and short/sparse pages."""
    pdf_file = tmp_pdf_factory("sparse.pdf", ["", "Hi", "Normal length sentence for valid content page."])
    extractor = PDFExtractor(min_char_threshold=20)
    pages = extractor.extract_pages(pdf_file)

    assert len(pages) == 3
    assert pages[0].extraction_quality == "empty"
    assert pages[0].char_count == 0

    assert pages[1].extraction_quality == "sparse"
    assert pages[1].text == "Hi"

    assert pages[2].extraction_quality == "ok"


def test_pdf_extraction_invalid_and_missing_file(tmp_path: Path):
    """Test exception handling for nonexistent or corrupted PDF files."""
    extractor = PDFExtractor()
    
    with pytest.raises(FileNotFoundError):
        extractor.extract_pages(tmp_path / "nonexistent.pdf")

    corrupted_file = tmp_path / "corrupt.pdf"
    corrupted_file.write_text("Not a valid PDF header")
    with pytest.raises(ValueError, match="Failed to parse PDF"):
        extractor.extract_pages(corrupted_file)


def test_deterministic_chunking_sliding_window():
    """Test chunker produces deterministic chunk IDs and preserves page spans."""
    # Build 3 simulated pages with 100 words each
    page1_text = " ".join([f"w1_{i}" for i in range(100)])
    page2_text = " ".join([f"w2_{i}" for i in range(100)])
    page3_text = " ".join([f"w3_{i}" for i in range(100)])

    pages = [
        PageContent(page_number=1, text=page1_text, char_count=len(page1_text)),
        PageContent(page_number=2, text=page2_text, char_count=len(page2_text)),
        PageContent(page_number=3, text=page3_text, char_count=len(page3_text)),
    ]

    # Chunk size 150 words, overlap 50 words -> step = 100 words
    # Total words = 300
    # Chunk 0: words 0..150 (Page 1 start, Page 2 end)
    # Chunk 1: words 100..250 (Page 2 start, Page 3 end)
    # Chunk 2: words 200..300 (Page 3 start, Page 3 end)
    chunker = TextChunker(chunk_size_words=150, chunk_overlap_words=50)
    chunks = chunker.chunk_document("doc_test_100", pages)

    assert len(chunks) == 3
    assert chunks[0].chunk_id == "doc_test_100_p1_0"
    assert chunks[0].page_start == 1
    assert chunks[0].page_end == 2
    assert chunks[0].metadata["word_count"] == 150

    assert chunks[1].chunk_id == "doc_test_100_p2_1"
    assert chunks[1].page_start == 2
    assert chunks[1].page_end == 3
    assert chunks[1].metadata["word_count"] == 150

    assert chunks[2].chunk_id == "doc_test_100_p3_2"
    assert chunks[2].page_start == 3
    assert chunks[2].page_end == 3
    assert chunks[2].metadata["word_count"] == 100

    # Ensure no text loss: first word of doc and last word of doc are retained
    assert "w1_0" in chunks[0].text
    assert "w3_99" in chunks[2].text


def test_chunker_empty_and_invalid_arguments():
    """Test chunker edge cases for empty pages and invalid overlap parameters."""
    with pytest.raises(ValueError, match="chunk_overlap_words must be strictly smaller"):
        TextChunker(chunk_size_words=100, chunk_overlap_words=100)

    chunker = TextChunker(chunk_size_words=100, chunk_overlap_words=20)
    assert chunker.chunk_document("doc_empty", []) == []

    empty_page = [PageContent(page_number=1, text="   ", char_count=0)]
    assert chunker.chunk_document("doc_whitespace", empty_page) == []
