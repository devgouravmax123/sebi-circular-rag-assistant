import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Optional, Any, ClassVar
from pydantic import BaseModel, Field

from app.schemas.document import DocumentRecord, PageContent, ChunkRecord
from app.ingestion.extractor import PDFExtractor, compute_sha256
from app.ingestion.chunker import TextChunker


class IngestionResult(BaseModel):
    """Structured result returned by the ingestion service."""
    status: str = Field(..., description="'success', 'duplicate', or 'error'")
    document_id: str
    message: str
    sha256: str
    page_count: int = 0
    chunk_count: int = 0
    document: Optional[DocumentRecord] = None
    pages: List[PageContent] = Field(default_factory=list)
    chunks: List[ChunkRecord] = Field(default_factory=list)


class DuplicateDocumentError(ValueError):
    """Raised when an ingested PDF sha256 already exists with conflicting metadata."""
    pass


class IngestionService:
    """
    Orchestrates local PDF extraction, deterministic chunking, schema validation,
    and atomic file-based persistence under data/processed/.

    Thread-safe across multiple instances targeting the same directory via a class-level
    lock registry. Explicitly designed for local, single-process execution.
    Note: Does not provide cross-process or multi-host distributed locking.
    """

    # Class-level lock registry mapping canonical Path -> threading.RLock
    _REGISTRY_MUTEX: ClassVar[threading.Lock] = threading.Lock()
    _DIR_LOCKS: ClassVar[Dict[Path, threading.RLock]] = {}

    @classmethod
    def _get_dir_lock(cls, canonical_path: Path) -> threading.RLock:
        """Obtain or initialize the thread lock for a given canonical directory path."""
        with cls._REGISTRY_MUTEX:
            if canonical_path not in cls._DIR_LOCKS:
                cls._DIR_LOCKS[canonical_path] = threading.RLock()
            return cls._DIR_LOCKS[canonical_path]

    def __init__(
        self,
        processed_dir: str | Path = "data/processed",
        extractor: Optional[PDFExtractor] = None,
        chunker: Optional[TextChunker] = None,
    ):
        self.processed_dir = Path(processed_dir).resolve()
        self.extractor = extractor or PDFExtractor()
        self.chunker = chunker or TextChunker()
        self.manifest_path = self.processed_dir / "manifest.json"
        self._lock = self._get_dir_lock(self.processed_dir)
        self._ensure_storage()

    def _ensure_storage(self) -> None:
        """Ensure storage directory exists and cleanup any abandoned staging artifacts."""
        with self._lock:
            self.processed_dir.mkdir(parents=True, exist_ok=True)
            if not self.manifest_path.exists():
                self._write_atomic_json(self.manifest_path, {"documents": {}, "sha256_index": {}})
            # Clean up any abandoned temporary staging directories from crashed runs
            self._cleanup_abandoned_staging()

    def _cleanup_abandoned_staging(self) -> None:
        """Clean up abandoned .staging_* directories left over from abrupt process terminations."""
        try:
            for item in self.processed_dir.glob(".staging_*"):
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
        except Exception:
            pass

    def _load_manifest(self) -> Dict[str, Any]:
        """Load manifest tracking ingested documents and sha256 hashes."""
        try:
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"documents": {}, "sha256_index": {}}

    def _save_manifest(self, manifest_data: Dict[str, Any]) -> None:
        """Atomically persist manifest data."""
        self._write_atomic_json(self.manifest_path, manifest_data)

    def _write_atomic_json(self, target_path: Path, data: Any) -> None:
        """Write JSON safely using a temporary file in the same directory to avoid partial/corrupted writes."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_file = target_path.with_suffix(f".tmp_{os.getpid()}_{threading.get_ident()}")
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(temp_file, target_path)
        except Exception:
            if temp_file.exists():
                temp_file.unlink()
            raise

    def check_for_orphans(self) -> List[str]:
        """
        Identify any document directories present on disk that are NOT tracked in manifest.json.
        Excludes manifest.json and temporary staging directories (.staging_*).
        """
        with self._lock:
            manifest = self._load_manifest()
            tracked_docs = set(manifest.get("documents", {}).keys())
            orphans: List[str] = []

            for child in self.processed_dir.iterdir():
                if child.is_dir() and not child.name.startswith(".staging_") and child.name not in tracked_docs:
                    orphans.append(child.name)

            return sorted(orphans)

    def ingest_pdf(
        self,
        pdf_path: str | Path,
        metadata_input: Dict[str, Any],
    ) -> IngestionResult:
        """
        Ingest a local PDF file, calculate checksum, extract pages, chunk, and persist records safely.
        Synchronized with thread lock per target directory.
        """
        with self._lock:
            path = Path(pdf_path)
            if not path.exists():
                return IngestionResult(
                    status="error",
                    document_id=metadata_input.get("document_id", "unknown"),
                    message=f"PDF file does not exist: {pdf_path}",
                    sha256="",
                )

            # 1. Compute SHA-256
            try:
                sha256_hash = compute_sha256(path)
            except Exception as e:
                return IngestionResult(
                    status="error",
                    document_id=metadata_input.get("document_id", "unknown"),
                    message=f"Failed to compute SHA-256 checksum: {e}",
                    sha256="",
                )

            # 2. Validate document_id and check manifest collisions
            manifest = self._load_manifest()
            sha_index = manifest.get("sha256_index", {})
            docs_dict = manifest.get("documents", {})
            doc_id = metadata_input.get("document_id")

            if not doc_id or not str(doc_id).strip():
                return IngestionResult(
                    status="error",
                    document_id="",
                    message="Missing required 'document_id' in metadata",
                    sha256=sha256_hash,
                )
            doc_id = str(doc_id).strip()

            target_doc_dir = self.processed_dir / doc_id

            # 2a. Orphan check: Document folder exists on disk but is untracked in manifest.json
            if doc_id not in docs_dict and target_doc_dir.exists():
                return IngestionResult(
                    status="error",
                    document_id=doc_id,
                    message=(
                        f"Integrity check failed: Directory '{doc_id}' already exists on disk but is not tracked "
                        f"in manifest.json (orphaned directory). Manual recovery or quarantine required before writing."
                    ),
                    sha256=sha256_hash,
                )

            # 2b. Check if document_id already exists in manifest with DIFFERENT content
            if doc_id in docs_dict:
                existing_sha = docs_dict[doc_id].get("sha256")
                if existing_sha != sha256_hash:
                    return IngestionResult(
                        status="error",
                        document_id=doc_id,
                        message=(
                            f"Document ID collision: document_id '{doc_id}' is already assigned to a different "
                            f"document with SHA-256 '{existing_sha}'"
                        ),
                        sha256=sha256_hash,
                    )
                else:
                    # Same document ID and same content -> idempotent duplicate
                    return IngestionResult(
                        status="duplicate",
                        document_id=doc_id,
                        message=f"Document '{doc_id}' with identical SHA-256 hash is already ingested",
                        sha256=sha256_hash,
                    )

            # 2c. Check if SHA-256 already exists under a DIFFERENT document_id
            if sha256_hash in sha_index:
                existing_doc_id = sha_index[sha256_hash]
                return IngestionResult(
                    status="error",
                    document_id=doc_id,
                    message=(
                        f"Duplicate content detected: SHA-256 matches existing document '{existing_doc_id}' "
                        f"with conflicting document_id '{doc_id}'"
                    ),
                    sha256=sha256_hash,
                )

            # 3. Extract text pages
            try:
                pages = self.extractor.extract_pages(path)
            except Exception as e:
                return IngestionResult(
                    status="error",
                    document_id=doc_id,
                    message=f"PDF extraction failed: {e}",
                    sha256=sha256_hash,
                )

            if not pages:
                return IngestionResult(
                    status="error",
                    document_id=doc_id,
                    message="PDF has 0 readable pages",
                    sha256=sha256_hash,
                )

            # 4. Generate deterministic chunks
            try:
                chunks = self.chunker.chunk_document(doc_id, pages)
            except Exception as e:
                return IngestionResult(
                    status="error",
                    document_id=doc_id,
                    message=f"Chunking failed: {e}",
                    sha256=sha256_hash,
                    page_count=len(pages),
                )

            # Resolve collection identity safely
            from app.schemas.document import resolve_collection_identity
            coll_id = resolve_collection_identity(doc_id, metadata_input.get("collection_id"))
            if coll_id:
                for chunk in chunks:
                    chunk.collection_id = coll_id

            # 5. Build and validate DocumentRecord
            try:
                doc_record = DocumentRecord(
                    document_id=doc_id,
                    circular_number=metadata_input.get("circular_number"),
                    title=metadata_input.get("title", path.stem),
                    department=metadata_input.get("department"),
                    issue_date=metadata_input.get("issue_date"),
                    effective_date=metadata_input.get("effective_date"),
                    source_url=metadata_input.get("source_url", f"file://{path.resolve().as_posix()}"),
                    sha256=sha256_hash,
                    pdf_path=str(path),
                    extraction_status="extracted",
                    regulator=metadata_input.get("regulator", "SEBI"),
                    domain=metadata_input.get("domain", "CSCRF"),
                    collection_id=coll_id,
                )
            except Exception as e:
                return IngestionResult(
                    status="error",
                    document_id=doc_id,
                    message=f"Document metadata validation failed: {e}",
                    sha256=sha256_hash,
                )

            # 6. Atomic Staging and Persistence
            staging_dir = self.processed_dir / f".staging_{doc_id}_{os.getpid()}_{threading.get_ident()}"
            staging_dir.mkdir(parents=True, exist_ok=True)

            try:
                # Save document metadata, pages, chunks in staging
                self._write_atomic_json(staging_dir / "document.json", doc_record.model_dump())
                self._write_atomic_json(staging_dir / "pages.json", [p.model_dump() for p in pages])
                self._write_atomic_json(staging_dir / "chunks.json", [c.model_dump() for c in chunks])

                # Promote staging directory to target
                if target_doc_dir.exists():
                    shutil.rmtree(target_doc_dir, ignore_errors=True)
                staging_dir.rename(target_doc_dir)

                # Update and persist manifest
                manifest["documents"][doc_id] = {
                    "document_id": doc_id,
                    "title": doc_record.title,
                    "sha256": sha256_hash,
                    "page_count": len(pages),
                    "chunk_count": len(chunks),
                    "processed_path": str(target_doc_dir),
                }
                manifest["sha256_index"][sha256_hash] = doc_id
                self._save_manifest(manifest)

            except Exception as e:
                # Clean up staging directory and target document directory to prevent partial state
                if staging_dir.exists():
                    shutil.rmtree(staging_dir, ignore_errors=True)
                if target_doc_dir.exists():
                    shutil.rmtree(target_doc_dir, ignore_errors=True)
                return IngestionResult(
                    status="error",
                    document_id=doc_id,
                    message=f"Failed to persist ingested artifacts: {e}",
                    sha256=sha256_hash,
                )

            return IngestionResult(
                status="success",
                document_id=doc_id,
                message=f"Successfully ingested {len(pages)} pages and {len(chunks)} chunks",
                sha256=sha256_hash,
                page_count=len(pages),
                chunk_count=len(chunks),
                document=doc_record,
                pages=pages,
                chunks=chunks,
            )


