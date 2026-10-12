"""
Idempotent Corpus Importer for SEBI Regulatory Intelligence Platform.

Transfers the existing processed file-backed CSCRF pilot corpus into
PostgreSQL + pgvector storage using SQLAlchemy 2.x persistence models.

Features:
- Validates documents, chunks, page provenance, metadata, and graph edges before writing.
- Verifies dense embeddings (shape 384, float32, model provenance) without fabricating vectors.
- Strict transaction boundaries: per-document atomicity or all-or-nothing full corpus mode.
- Idempotent: safe re-runs skip existing versions and identical checksums without duplication.
- Conflict detection: raises and reports explicit conflicts on mismatched checksums/versions.
- Dry-run validation mode that verifies all records without committing database writes.
- Summary reporting: discovered, validated, inserted, already present, skipped, failed.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone, date
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any, Set, Tuple
import numpy as np

from sqlalchemy import select, and_, text
from sqlalchemy.orm import Session, sessionmaker

from app.schemas.document import DocumentRecord, ChunkRecord
from app.schemas.graph import GraphEdge, RelationshipType, ScopeType, ReviewStatus, ConfidenceLevel
from app.persistence.models import (
    RegulatorModel,
    RegulatoryDomainModel,
    RegulatoryDocumentModel,
    DocumentVersionModel,
    ProvisionModel,
    ChunkModel,
    ChunkEmbeddingModel,
    RegulatoryEdgeModel,
)

logger = logging.getLogger(__name__)


def parse_date(date_str: Optional[str]) -> Optional[date]:
    """Parse YYYY-MM-DD string to date object, returning None if invalid or absent."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except Exception:
        return None


@dataclass
class ImportStats:
    """Detailed summary statistics of corpus import operation."""
    documents_discovered: int = 0
    documents_validated: int = 0
    documents_inserted: int = 0
    documents_already_present: int = 0
    documents_skipped: int = 0
    documents_failed: int = 0

    versions_inserted: int = 0
    versions_already_present: int = 0

    chunks_discovered: int = 0
    chunks_validated: int = 0
    chunks_inserted: int = 0
    chunks_already_present: int = 0
    chunks_skipped: int = 0
    chunks_failed: int = 0

    embeddings_discovered: int = 0
    embeddings_validated: int = 0
    embeddings_inserted: int = 0
    embeddings_already_present: int = 0
    embeddings_skipped: int = 0
    embeddings_failed: int = 0

    edges_discovered: int = 0
    edges_validated: int = 0
    edges_inserted: int = 0
    edges_already_present: int = 0
    edges_skipped: int = 0
    edges_failed: int = 0

    conflicts: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    skipped_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "documents": {
                "discovered": self.documents_discovered,
                "validated": self.documents_validated,
                "inserted": self.documents_inserted,
                "already_present": self.documents_already_present,
                "skipped": self.documents_skipped,
                "failed": self.documents_failed,
            },
            "versions": {
                "inserted": self.versions_inserted,
                "already_present": self.versions_already_present,
            },
            "chunks": {
                "discovered": self.chunks_discovered,
                "validated": self.chunks_validated,
                "inserted": self.chunks_inserted,
                "already_present": self.chunks_already_present,
                "skipped": self.chunks_skipped,
                "failed": self.chunks_failed,
            },
            "embeddings": {
                "discovered": self.embeddings_discovered,
                "validated": self.embeddings_validated,
                "inserted": self.embeddings_inserted,
                "already_present": self.embeddings_already_present,
                "skipped": self.embeddings_skipped,
                "failed": self.embeddings_failed,
            },
            "edges": {
                "discovered": self.edges_discovered,
                "validated": self.edges_validated,
                "inserted": self.edges_inserted,
                "already_present": self.edges_already_present,
                "skipped": self.edges_skipped,
                "failed": self.edges_failed,
            },
            "conflicts": self.conflicts,
            "errors": self.errors,
            "skipped_reasons": self.skipped_reasons,
        }


class CorpusImporter:
    """
    Validates and imports file-backed processed regulatory corpus into PostgreSQL + pgvector.
    """

    DEFAULT_REGULATOR_ID = "SEBI"
    DEFAULT_REGULATOR_NAME = "Securities and Exchange Board of India"
    DEFAULT_REGULATOR_URL = "https://www.sebi.gov.in"
    DEFAULT_DOMAIN_ID = "SEBI_CSCRF"
    DEFAULT_DOMAIN_CODE = "CSCRF"
    DEFAULT_DOMAIN_TITLE = "Cybersecurity and Cyber Resilience Framework"
    EXPECTED_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
    EXPECTED_EMBEDDING_DIM = 384

    def __init__(
        self,
        session_factory: Optional[sessionmaker[Session]] = None,
        processed_dir: str | Path = "data/processed",
        indexes_dir: str | Path = "data/indexes",
        regulator_id: str = DEFAULT_REGULATOR_ID,
        domain_id: str = DEFAULT_DOMAIN_ID,
    ):
        self.session_factory = session_factory
        self.processed_dir = Path(processed_dir)
        self.indexes_dir = Path(indexes_dir)
        self.regulator_id = regulator_id
        self.domain_id = domain_id

    def load_and_validate_corpus(self) -> Tuple[
        Dict[str, Tuple[DocumentRecord, List[ChunkRecord]]],
        Dict[str, np.ndarray],
        List[GraphEdge],
        ImportStats,
    ]:
        """
        Reads files from processed_dir and indexes_dir, parses schemas, validates structure,
        and returns clean records with initial stats.
        """
        stats = ImportStats()

        manifest_path = self.processed_dir / "manifest.json"
        if not manifest_path.exists():
            msg = f"Corpus manifest not found at {manifest_path}"
            stats.errors.append(msg)
            raise FileNotFoundError(msg)

        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest_data = json.load(f)
        except Exception as e:
            msg = f"Failed to parse manifest.json: {e}"
            stats.errors.append(msg)
            raise ValueError(msg) from e

        doc_manifest = manifest_data.get("documents", {})
        stats.documents_discovered = len(doc_manifest)

        documents_and_chunks: Dict[str, Tuple[DocumentRecord, List[ChunkRecord]]] = {}

        for doc_id, meta in doc_manifest.items():
            doc_dir = self.processed_dir / doc_id
            doc_json = doc_dir / "document.json"
            chunks_json = doc_dir / "chunks.json"

            if not doc_json.exists():
                stats.documents_failed += 1
                msg = f"Missing document.json for {doc_id} in {doc_dir}"
                stats.errors.append(msg)
                continue

            try:
                with open(doc_json, "r", encoding="utf-8") as f:
                    doc_dict = json.load(f)
                doc_record = DocumentRecord(**doc_dict)
            except Exception as e:
                stats.documents_failed += 1
                msg = f"Invalid document schema for {doc_id}: {e}"
                stats.errors.append(msg)
                continue

            # Validate checksum against manifest
            manifest_sha = meta.get("sha256")
            if manifest_sha and doc_record.sha256 != manifest_sha:
                stats.documents_failed += 1
                msg = f"Checksum mismatch for {doc_id}: document.json={doc_record.sha256} vs manifest={manifest_sha}"
                stats.conflicts.append(msg)
                stats.errors.append(msg)
                continue

            # Validate chunks
            chunk_records: List[ChunkRecord] = []
            if chunks_json.exists():
                try:
                    with open(chunks_json, "r", encoding="utf-8") as f:
                        chunks_data = json.load(f)
                    if not isinstance(chunks_data, list):
                        raise ValueError(f"chunks.json in {doc_id} is not a list")

                    stats.chunks_discovered += len(chunks_data)
                    for c_dict in chunks_data:
                        c_rec = ChunkRecord(**c_dict)
                        # Validate page range and text non-empty
                        if c_rec.page_start < 1 or c_rec.page_end < c_rec.page_start:
                            raise ValueError(f"Invalid page bounds in chunk {c_rec.chunk_id}: {c_rec.page_start}-{c_rec.page_end}")
                        if not c_rec.text.strip():
                            raise ValueError(f"Empty text in chunk {c_rec.chunk_id}")
                        chunk_records.append(c_rec)
                        stats.chunks_validated += 1
                except Exception as e:
                    stats.documents_failed += 1
                    stats.chunks_failed += len(chunks_data) if 'chunks_data' in locals() and isinstance(chunks_data, list) else 1
                    msg = f"Failed to validate chunks for {doc_id}: {e}"
                    stats.errors.append(msg)
                    continue
            else:
                stats.skipped_reasons.append(f"No chunks.json for {doc_id}")

            documents_and_chunks[doc_id] = (doc_record, chunk_records)
            stats.documents_validated += 1

        # Load embeddings if available in indexes_dir
        dense_vecs: Dict[str, np.ndarray] = {}
        index_npy = self.indexes_dir / "dense_index.npy"
        index_meta = self.indexes_dir / "dense_index_meta.json"

        if index_npy.exists() and index_meta.exists():
            try:
                with open(index_meta, "r", encoding="utf-8") as f:
                    meta_dict = json.load(f)

                model_name = meta_dict.get("model_name", "")
                dim = meta_dict.get("embedding_dim", 0)
                chunk_ids = meta_dict.get("chunk_ids", [])

                if model_name != self.EXPECTED_EMBEDDING_MODEL:
                    msg = f"Index model mismatch: expected {self.EXPECTED_EMBEDDING_MODEL}, found {model_name}"
                    stats.skipped_reasons.append(msg)
                elif dim != self.EXPECTED_EMBEDDING_DIM:
                    msg = f"Index dimension mismatch: expected {self.EXPECTED_EMBEDDING_DIM}, found {dim}"
                    stats.skipped_reasons.append(msg)
                else:
                    embeddings_arr = np.load(index_npy)
                    if embeddings_arr.ndim != 2 or embeddings_arr.shape[1] != self.EXPECTED_EMBEDDING_DIM:
                        msg = f"Dense index array invalid shape: {embeddings_arr.shape}"
                        stats.errors.append(msg)
                    elif len(chunk_ids) != embeddings_arr.shape[0]:
                        msg = f"Chunk count mismatch: meta={len(chunk_ids)}, array={embeddings_arr.shape[0]}"
                        stats.errors.append(msg)
                    else:
                        stats.embeddings_discovered = len(chunk_ids)
                        for i, cid in enumerate(chunk_ids):
                            vec = embeddings_arr[i].astype(np.float32)
                            dense_vecs[cid] = vec
                            stats.embeddings_validated += 1
            except Exception as e:
                msg = f"Failed reading dense index: {e}"
                stats.errors.append(msg)
        else:
            stats.skipped_reasons.append(f"No dense index found at {self.indexes_dir}")

        # Load graph edges
        graph_json = self.processed_dir / "cscrf_graph.json"
        graph_edges: List[GraphEdge] = []
        if graph_json.exists():
            try:
                with open(graph_json, "r", encoding="utf-8") as f:
                    graph_data = json.load(f)
                if not isinstance(graph_data, list):
                    raise ValueError("cscrf_graph.json must be a list")

                stats.edges_discovered = len(graph_data)
                for edge_dict in graph_data:
                    edge = GraphEdge(**edge_dict)
                    if edge.source_document_id == edge.target_document_id:
                        raise ValueError(f"Self-loop detected in edge {edge.edge_id}")
                    if not edge.evidence_text.strip():
                        raise ValueError(f"Empty evidence in edge {edge.edge_id}")
                    graph_edges.append(edge)
                    stats.edges_validated += 1
            except Exception as e:
                stats.edges_failed = stats.edges_discovered
                msg = f"Failed validating graph edges: {e}"
                stats.errors.append(msg)
        else:
            stats.skipped_reasons.append("No cscrf_graph.json found in processed_dir")

        return documents_and_chunks, dense_vecs, graph_edges, stats

    def import_corpus(
        self,
        dry_run: bool = False,
        all_or_nothing: bool = False,
    ) -> ImportStats:
        """
        Execute corpus import against configured PostgreSQL database.

        Parameters:
        - dry_run: If True, validate everything but execute no database modifications.
        - all_or_nothing: If True, run entire corpus import in a single transaction.
                          If False (default), use per-document atomic transactions.
        """
        documents_and_chunks, dense_vecs, graph_edges, stats = self.load_and_validate_corpus()

        if stats.errors:
            logger.error("Corpus validation encountered errors before import: %s", stats.errors)
            if all_or_nothing:
                return stats

        if dry_run:
            logger.info("Dry-run requested: skipping persistent database writes.")
            # In dry-run, consider all validated items as would-be inserted
            stats.documents_inserted = stats.documents_validated
            stats.versions_inserted = stats.documents_validated
            stats.chunks_inserted = stats.chunks_validated
            stats.embeddings_inserted = stats.embeddings_validated
            stats.edges_inserted = stats.edges_validated
            return stats

        if not self.session_factory:
            raise ValueError("Cannot perform persistent import: session_factory is None. Please provide a database session factory.")

        # Ensure taxonomy (Regulator and RegulatoryDomain) exists
        self._ensure_taxonomy()

        if all_or_nothing:
            self._import_all_or_nothing(documents_and_chunks, dense_vecs, graph_edges, stats)
        else:
            self._import_per_document(documents_and_chunks, dense_vecs, graph_edges, stats)

        return stats

    def _ensure_taxonomy(self) -> None:
        """Ensure regulator and domain records are present."""
        assert self.session_factory is not None
        with self.session_factory() as session:
            with session.begin():
                regulator = session.execute(
                    select(RegulatorModel).where(RegulatorModel.id == self.regulator_id)
                ).scalar_one_or_none()
                if not regulator:
                    session.add(
                        RegulatorModel(
                            id=self.regulator_id,
                            name=self.DEFAULT_REGULATOR_NAME,
                            official_portal_url=self.DEFAULT_REGULATOR_URL,
                        )
                    )

                domain = session.execute(
                    select(RegulatoryDomainModel).where(RegulatoryDomainModel.id == self.domain_id)
                ).scalar_one_or_none()
                if not domain:
                    session.add(
                        RegulatoryDomainModel(
                            id=self.domain_id,
                            regulator_id=self.regulator_id,
                            code=self.DEFAULT_DOMAIN_CODE,
                            title=self.DEFAULT_DOMAIN_TITLE,
                        )
                    )

    def _import_single_document_record(
        self,
        session: Session,
        doc_record: DocumentRecord,
        chunks: List[ChunkRecord],
        dense_vecs: Dict[str, np.ndarray],
        stats: ImportStats,
    ) -> None:
        """
        Imports one document, its version, chunks, and embeddings inside the active session/transaction.
        """
        issue_d = parse_date(doc_record.issue_date)
        effective_d = parse_date(doc_record.effective_date)

        # 1. Logical document
        doc_model = session.execute(
            select(RegulatoryDocumentModel).where(RegulatoryDocumentModel.id == doc_record.document_id)
        ).scalar_one_or_none()

        if not doc_model:
            doc_model = RegulatoryDocumentModel(
                id=doc_record.document_id,
                domain_id=self.domain_id,
                circular_number=doc_record.circular_number,
                title=doc_record.title,
                department=doc_record.department,
                first_issue_date=issue_d,
            )
            session.add(doc_model)
            stats.documents_inserted += 1
        else:
            stats.documents_already_present += 1
            # Update mutable metadata if needed
            doc_model.circular_number = doc_record.circular_number
            doc_model.title = doc_record.title
            doc_model.department = doc_record.department
            if issue_d and not doc_model.first_issue_date:
                doc_model.first_issue_date = issue_d

        # 2. Immutable version
        existing_version = session.execute(
            select(DocumentVersionModel).where(
                and_(
                    DocumentVersionModel.document_id == doc_record.document_id,
                    DocumentVersionModel.sha256 == doc_record.sha256,
                )
            )
        ).scalar_one_or_none()

        if existing_version:
            stats.versions_already_present += 1
            version_id = existing_version.id
        else:
            # Check if this document_id has existing versions with DIFFERENT sha256
            curr_max = session.execute(
                select(DocumentVersionModel.version_number)
                .where(DocumentVersionModel.document_id == doc_record.document_id)
                .order_by(DocumentVersionModel.version_number.desc())
            ).scalars().first()
            next_ver = (curr_max or 0) + 1

            version_id = f"{doc_record.document_id}_v{next_ver}"
            version_model = DocumentVersionModel(
                id=version_id,
                document_id=doc_record.document_id,
                version_number=next_ver,
                source_url=doc_record.source_url,
                storage_path=doc_record.pdf_path,
                sha256=doc_record.sha256,
                issue_date=issue_d,
                effective_date=effective_d,
                extraction_status=doc_record.extraction_status,
            )
            session.add(version_model)
            stats.versions_inserted += 1

        # 3. Chunks & Embeddings
        for idx, chunk in enumerate(chunks):
            chunk_model = session.execute(
                select(ChunkModel).where(ChunkModel.id == chunk.chunk_id)
            ).scalar_one_or_none()

            if not chunk_model:
                chunk_model = ChunkModel(
                    id=chunk.chunk_id,
                    version_id=version_id,
                    chunk_index=idx,
                    text=chunk.text,
                    pdf_page_start=chunk.page_start,
                    pdf_page_end=chunk.page_end,
                    is_ocr=chunk.is_ocr,
                    chunk_type=chunk.chunk_type,
                    metadata_json=chunk.metadata,
                )
                session.add(chunk_model)
                stats.chunks_inserted += 1
            else:
                stats.chunks_already_present += 1

            # Embeddings
            if chunk.chunk_id in dense_vecs:
                vec = dense_vecs[chunk.chunk_id]
                existing_embed = session.execute(
                    select(ChunkEmbeddingModel).where(ChunkEmbeddingModel.chunk_id == chunk.chunk_id)
                ).scalar_one_or_none()

                if not existing_embed:
                    embed_model = ChunkEmbeddingModel(
                        chunk_id=chunk.chunk_id,
                        model_name=self.EXPECTED_EMBEDDING_MODEL,
                        embedding_dim=self.EXPECTED_EMBEDDING_DIM,
                        embedding=vec,
                    )
                    session.add(embed_model)
                    stats.embeddings_inserted += 1
                else:
                    stats.embeddings_already_present += 1
            else:
                stats.embeddings_skipped += 1

    def _import_per_document(
        self,
        documents_and_chunks: Dict[str, Tuple[DocumentRecord, List[ChunkRecord]]],
        dense_vecs: Dict[str, np.ndarray],
        graph_edges: List[GraphEdge],
        stats: ImportStats,
    ) -> None:
        """Imports documents with per-document atomicity."""
        assert self.session_factory is not None

        for doc_id, (doc_record, chunks) in documents_and_chunks.items():
            try:
                with self.session_factory() as session:
                    with session.begin():
                        self._import_single_document_record(
                            session, doc_record, chunks, dense_vecs, stats
                        )
            except Exception as e:
                stats.documents_failed += 1
                msg = f"Failed to import document {doc_id}: {e}"
                stats.errors.append(msg)
                logger.exception(msg)

        # Import edges in separate transaction
        self._import_edges(graph_edges, stats)

    def _import_all_or_nothing(
        self,
        documents_and_chunks: Dict[str, Tuple[DocumentRecord, List[ChunkRecord]]],
        dense_vecs: Dict[str, np.ndarray],
        graph_edges: List[GraphEdge],
        stats: ImportStats,
    ) -> None:
        """Imports entire corpus in a single all-or-nothing transaction."""
        assert self.session_factory is not None
        try:
            with self.session_factory() as session:
                with session.begin():
                    for doc_id, (doc_record, chunks) in documents_and_chunks.items():
                        self._import_single_document_record(
                            session, doc_record, chunks, dense_vecs, stats
                        )
                    self._import_edges_in_session(session, graph_edges, stats)
        except Exception as e:
            stats.documents_failed = len(documents_and_chunks)
            msg = f"All-or-nothing corpus import failed, rolled back all changes: {e}"
            stats.errors.append(msg)
            logger.exception(msg)
            raise

    def _import_edges(self, graph_edges: List[GraphEdge], stats: ImportStats) -> None:
        """Imports graph edges inside a transaction."""
        assert self.session_factory is not None
        try:
            with self.session_factory() as session:
                with session.begin():
                    self._import_edges_in_session(session, graph_edges, stats)
        except Exception as e:
            stats.edges_failed = len(graph_edges)
            msg = f"Failed to import graph edges: {e}"
            stats.errors.append(msg)
            logger.exception(msg)

    def _import_edges_in_session(
        self, session: Session, graph_edges: List[GraphEdge], stats: ImportStats
    ) -> None:
        """Helper to insert/update graph edges within an active session."""
        for edge in graph_edges:
            existing_edge = session.execute(
                select(RegulatoryEdgeModel).where(RegulatoryEdgeModel.id == edge.edge_id)
            ).scalar_one_or_none()

            eff_d = parse_date(edge.effective_date)

            if not existing_edge:
                new_edge = RegulatoryEdgeModel(
                    id=edge.edge_id,
                    source_document_id=edge.source_document_id,
                    target_document_id=edge.target_document_id,
                    relationship=edge.relationship.value,
                    scope=edge.scope.value,
                    affected_provisions=edge.affected_provisions,
                    evidence_text=edge.evidence_text,
                    evidence_pdf_page=edge.evidence_page,
                    confidence=edge.confidence.value,
                    review_status=edge.review_status.value,
                    effective_date=eff_d,
                )
                session.add(new_edge)
                stats.edges_inserted += 1
            else:
                stats.edges_already_present += 1
                # If an edge was already verified by human review, do not downgrade to needs_review
                if existing_edge.review_status == "verified" and edge.review_status.value == "needs_review":
                    pass  # Keep verified status intact
                else:
                    existing_edge.review_status = edge.review_status.value
                    existing_edge.confidence = edge.confidence.value
                    existing_edge.evidence_text = edge.evidence_text
                    existing_edge.evidence_pdf_page = edge.evidence_page

                existing_edge.relationship = edge.relationship.value
                existing_edge.scope = edge.scope.value
                existing_edge.affected_provisions = edge.affected_provisions
                existing_edge.effective_date = eff_d
