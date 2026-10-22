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

from app.ingestion.manifest_runner import (
    ManifestIngestionRunner,
    ManifestSecurityError,
    ManifestValidationError,
    resolve_and_validate_pdf_path,
    load_and_validate_manifest,
)
from app.schemas.manifest import (
    CollectionManifest,
    ManifestDocumentItem,
    ManifestIngestionOutcome,
    ManifestIngestionReport,
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
    "ManifestIngestionRunner",
    "ManifestSecurityError",
    "ManifestValidationError",
    "resolve_and_validate_pdf_path",
    "load_and_validate_manifest",
    "CollectionManifest",
    "ManifestDocumentItem",
    "ManifestIngestionOutcome",
    "ManifestIngestionReport",
    "CollectionRegistry",
    "CollectionRegistryError",
    "UnknownCollectionError",
    "DuplicateCollectionError",
    "InvalidCollectionError",
    "CollectionStatus",
    "GroundingPolicyDeclaration",
    "GroundingPolicyType",
]

from app.ingestion.collection_registry import (
    CollectionRegistry,
    CollectionRegistryError,
    UnknownCollectionError,
    DuplicateCollectionError,
    InvalidCollectionError,
)
from app.schemas.manifest import (
    CollectionStatus,
    GroundingPolicyDeclaration,
    GroundingPolicyType,
)
