"""Document ingestion module."""
from app.ingestion.extractor import PDFExtractor, compute_sha256
from app.ingestion.chunker import TextChunker
from app.ingestion.service import IngestionService, IngestionResult
from app.ingestion.corpus_registry import SEBI_CSCRF_CORPUS
from app.ingestion.downloader import (
    SecurePDFDownloader,
    DownloadResult,
    DownloadSecurityError,
    SSRFValidationError,
    InvalidPDFError,
    DownloadLimitExceededError,
    DownloadHTTPError,
)

from app.ingestion.orchestrator import (
    IngestionOrchestrator,
    IngestionWorker,
    JobExecutionResult,
    NonRetryableIngestionError,
    RetryableIngestionError,
)

__all__ = [
    "PDFExtractor",
    "TextChunker",
    "compute_sha256",
    "IngestionService",
    "IngestionResult",
    "SEBI_CSCRF_CORPUS",
    "SecurePDFDownloader",
    "DownloadResult",
    "DownloadSecurityError",
    "SSRFValidationError",
    "InvalidPDFError",
    "DownloadLimitExceededError",
    "DownloadHTTPError",
    "IngestionOrchestrator",
    "IngestionWorker",
    "JobExecutionResult",
    "NonRetryableIngestionError",
    "RetryableIngestionError",
]
