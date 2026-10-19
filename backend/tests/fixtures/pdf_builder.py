import os
from pathlib import Path
import fitz  # PyMuPDF


def create_synthetic_pdf(
    file_path: Path,
    pages_text: list[str]
) -> Path:
    """Helper to generate a clean synthetic PDF with exact text per page."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    for text in pages_text:
        page = doc.new_page()  # type: ignore[attr-defined]
        # insert text starting at top-left
        p = fitz.Point(50, 72)
        page.insert_text(p, text, fontsize=11)
    doc.save(str(file_path))
    doc.close()
    return file_path
