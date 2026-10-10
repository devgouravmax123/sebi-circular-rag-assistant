import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Any
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
    """

    def __init__(
        self,
        processed_dir: str | Path = "data/processed",
        extractor: Optional[PDFExtractor] = None,
        chunker: Optional[TextChunker] = None,
    ):
        self.processed_dir = Path(processed_dir)
        self.extractor = extractor or PDFExtractor()
        self.chunker = chunker or TextChunker()
        self.manifest_path = self.processed_dir / "manifest.json"
        self._ensure_storage()

    def _ensure_storage(self) -> None:
        """Ensure the target processed storage directory exists."""
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        if not self.manifest_path.exists():
            self._write_atomic_json(self.manifest_path, {"documents": {}, "sha256_index": {}})

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
        """Write JSON safely using a temporary file to avoid partial/corrupted writes."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_file = target_path.with_suffix(".tmp")
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            temp_file.replace(target_path)
        except Exception:
            if temp_file.exists():
                temp_file.unlink()
            raise

    def ingest_pdf(
        self,
        pdf_path: str | Path,
        metadata_input: Dict[str, Any],
    ) -> IngestionResult:
        """
        Ingest a local PDF file, calculate checksum, extract pages, chunk, and persist records safely.
        """
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

        # 2. Check for duplicate SHA-256
        manifest = self._load_manifest()
        sha_index = manifest.get("sha256_index", {})
        doc_id = metadata_input.get("document_id")

        if not doc_id or not str(doc_id).strip():
            return IngestionResult(
                status="error",
                document_id="",
                message="Missing required 'document_id' in metadata",
                sha256=sha256_hash,
            )

        if sha256_hash in sha_index:
            existing_doc_id = sha_index[sha256_hash]
            if existing_doc_id == doc_id:
                # Deterministic re-ingestion of the exact same document
                return IngestionResult(
                    status="duplicate",
                    document_id=doc_id,
                    message=f"Document '{doc_id}' with identical SHA-256 hash is already ingested",
                    sha256=sha256_hash,
                )
            else:
                # Different document ID claiming identical PDF bytes
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
            )
        except Exception as e:
            return IngestionResult(
                status="error",
                document_id=doc_id,
                message=f"Document metadata validation failed: {e}",
                sha256=sha256_hash,
            )

        # 6. Atomic Persistence per document
        doc_dir = self.processed_dir / doc_id
        doc_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Save document metadata
            self._write_atomic_json(doc_dir / "document.json", doc_record.model_dump())
            # Save pages
            self._write_atomic_json(doc_dir / "pages.json", [p.model_dump() for p in pages])
            # Save chunks
            self._write_atomic_json(doc_dir / "chunks.json", [c.model_dump() for c in chunks])

            # Update manifest
            manifest["documents"][doc_id] = {
                "document_id": doc_id,
                "title": doc_record.title,
                "sha256": sha256_hash,
                "page_count": len(pages),
                "chunk_count": len(chunks),
                "processed_path": str(doc_dir),
            }
            manifest["sha256_index"][sha256_hash] = doc_id
            self._save_manifest(manifest)

        except Exception as e:
            # Clean up partial doc directory if write fails
            if doc_dir.exists():
                shutil.rmtree(doc_dir, ignore_errors=True)
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
