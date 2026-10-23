import hashlib
import os
from pathlib import Path
from typing import List, Tuple
from app.schemas.document import PageContent, DocumentRecord


def compute_sha256(file_path: str | Path) -> str:
    """Compute SHA-256 hash of a file."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


class PDFExtractor:
    """Extracts text content page-by-page from PDF documents using PyMuPDF."""

    def __init__(self, min_char_threshold: int = 20):
        self.min_char_threshold = min_char_threshold

    def extract_pages(self, pdf_path: str | Path) -> List[PageContent]:
        """Extract text from all pages of a PDF file, preserving 1-indexed page numbering."""
        path = Path(pdf_path)
        if not path.exists():
            raise FileNotFoundError(f"PDF file not found: {pdf_path}")

        try:
            import fitz  # PyMuPDF
        except ImportError as err:
            raise RuntimeError(
                "PyMuPDF ('fitz') is required for PDF text extraction but is not installed. "
                "Install PyMuPDF or use pre-indexed snapshot collections."
            ) from err

        try:
            doc = fitz.open(str(path))
        except Exception as e:
            raise ValueError(f"Failed to parse PDF {pdf_path}: {e}") from e

        pages: List[PageContent] = []
        try:
            for page_idx in range(len(doc)):
                page = doc[page_idx]
                page_num = page_idx + 1
                text = page.get_text("text").strip()
                char_count = len(text)

                quality = "ok"
                if char_count == 0:
                    quality = "empty"
                elif char_count < self.min_char_threshold:
                    quality = "sparse"

                pages.append(
                    PageContent(
                        page_number=page_num,
                        text=text,
                        is_ocr=False,
                        extraction_quality=quality,
                        char_count=char_count,
                    )
                )
        finally:
            doc.close()

        return pages
