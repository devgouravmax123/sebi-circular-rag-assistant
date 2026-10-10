"""Document ingestion module."""
from app.ingestion.extractor import PDFExtractor, compute_sha256
from app.ingestion.chunker import TextChunker
from app.ingestion.service import IngestionService, IngestionResult
from app.ingestion.corpus_registry import SEBI_CSCRF_CORPUS

__all__ = [
    "PDFExtractor",
    "TextChunker",
    "compute_sha256",
    "IngestionService",
    "IngestionResult",
    "SEBI_CSCRF_CORPUS",
]
