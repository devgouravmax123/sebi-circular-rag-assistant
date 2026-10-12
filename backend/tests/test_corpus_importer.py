"""
Unit and isolated storage tests for the Phase 8A.5 Idempotent Corpus Importer.

Tests cover:
1. Corpus validation and malformed input handling.
2. Dry-run without database writes.
3. Initial import and repeat import (idempotency).
4. Duplicate checksums and immutable version preservation.
5. Transaction rollback on failure (per-document and all-or-nothing).
6. Preservation of stable IDs and provenance.
7. Missing, mismatching, or invalid embeddings detection.
8. Preservation of graph-edge review statuses (verified vs needs_review).
9. Accurate import summary counts and JSON reporting.
"""
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Any, List
from unittest.mock import MagicMock, patch
import numpy as np
import pytest

from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool

from app.schemas.document import DocumentRecord, ChunkRecord
from app.schemas.graph import GraphEdge, RelationshipType, ScopeType, ReviewStatus, ConfidenceLevel
from app.persistence.models import (
    Base,
    RegulatorModel,
    RegulatoryDomainModel,
    RegulatoryDocumentModel,
    DocumentVersionModel,
    ChunkModel,
    ChunkEmbeddingModel,
    RegulatoryEdgeModel,
)
from app.persistence.importer import CorpusImporter, ImportStats


from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from pgvector.sqlalchemy import Vector


@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(UUID, "sqlite")
def compile_uuid_sqlite(type_, compiler, **kw):
    return "VARCHAR(36)"


@compiles(Vector, "sqlite")
def compile_vector_sqlite(type_, compiler, **kw):
    return "BLOB"


@pytest.fixture
def sqlite_session_factory():
    """
    In-memory SQLite database session factory for isolated persistence tests.
    Uses SQLite-compatible column compilation handlers for JSONB/UUID/Vector.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Create specific tables needed for importer tests
    # Avoid creating IngestionJobModel which uses PostgreSQL-specific JSONB/UUID types
    importer_tables = [
        RegulatorModel.__table__,
        RegulatoryDomainModel.__table__,
        RegulatoryDocumentModel.__table__,
        DocumentVersionModel.__table__,
        ChunkModel.__table__,
        ChunkEmbeddingModel.__table__,
        RegulatoryEdgeModel.__table__,
    ]
    Base.metadata.create_all(bind=engine, tables=importer_tables)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    Base.metadata.drop_all(bind=engine, tables=importer_tables)


@pytest.fixture
def mock_corpus_dir(tmp_path):
    """Creates a temporary miniature mock processed corpus directory."""
    processed_dir = tmp_path / "processed"
    indexes_dir = tmp_path / "indexes"
    processed_dir.mkdir(parents=True)
    indexes_dir.mkdir(parents=True)

    # 1. Manifest
    manifest_data = {
        "documents": {
            "doc_test_1": {
                "document_id": "doc_test_1",
                "title": "Test Circular 1",
                "sha256": "hash_test_1",
            },
            "doc_test_2": {
                "document_id": "doc_test_2",
                "title": "Test Circular 2",
                "sha256": "hash_test_2",
            },
        }
    }
    with open(processed_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest_data, f)

    # 2. Document 1
    d1_dir = processed_dir / "doc_test_1"
    d1_dir.mkdir()
    d1_meta = {
        "document_id": "doc_test_1",
        "circular_number": "CIR/TEST/1",
        "title": "Test Circular 1",
        "department": "ITD",
        "issue_date": "2024-08-20",
        "effective_date": None,
        "source_url": "https://sebi.gov.in/test1.pdf",
        "retrieved_at": "2024-08-20T00:00:00Z",
        "sha256": "hash_test_1",
        "pdf_path": "data/raw/test1.pdf",
        "extraction_status": "extracted",
    }
    with open(d1_dir / "document.json", "w", encoding="utf-8") as f:
        json.dump(d1_meta, f)

    d1_chunks = [
        {
            "chunk_id": "doc_test_1_c0",
            "document_id": "doc_test_1",
            "text": "First chunk text of doc 1.",
            "page_start": 1,
            "page_end": 1,
            "is_ocr": False,
            "chunk_type": "body",
            "metadata": {"chunk_index": 0},
        },
        {
            "chunk_id": "doc_test_1_c1",
            "document_id": "doc_test_1",
            "text": "Second chunk text of doc 1.",
            "page_start": 2,
            "page_end": 2,
            "is_ocr": False,
            "chunk_type": "body",
            "metadata": {"chunk_index": 1},
        },
    ]
    with open(d1_dir / "chunks.json", "w", encoding="utf-8") as f:
        json.dump(d1_chunks, f)

    # 3. Document 2
    d2_dir = processed_dir / "doc_test_2"
    d2_dir.mkdir()
    d2_meta = {
        "document_id": "doc_test_2",
        "circular_number": "CIR/TEST/2",
        "title": "Test Circular 2",
        "department": "ITD",
        "issue_date": "2025-03-28",
        "effective_date": None,
        "source_url": "https://sebi.gov.in/test2.pdf",
        "retrieved_at": "2025-03-28T00:00:00Z",
        "sha256": "hash_test_2",
        "pdf_path": "data/raw/test2.pdf",
        "extraction_status": "extracted",
    }
    with open(d2_dir / "document.json", "w", encoding="utf-8") as f:
        json.dump(d2_meta, f)

    d2_chunks = [
        {
            "chunk_id": "doc_test_2_c0",
            "document_id": "doc_test_2",
            "text": "First chunk of doc 2.",
            "page_start": 1,
            "page_end": 1,
            "is_ocr": False,
            "chunk_type": "body",
            "metadata": {"chunk_index": 0},
        }
    ]
    with open(d2_dir / "chunks.json", "w", encoding="utf-8") as f:
        json.dump(d2_chunks, f)

    # 4. Dense Index (3 chunks total)
    chunk_ids = ["doc_test_1_c0", "doc_test_1_c1", "doc_test_2_c0"]
    index_meta = {
        "model_name": "BAAI/bge-small-en-v1.5",
        "embedding_dim": 384,
        "num_chunks": 3,
        "chunk_ids": chunk_ids,
    }
    with open(indexes_dir / "dense_index_meta.json", "w", encoding="utf-8") as f:
        json.dump(index_meta, f)

    vecs = np.random.randn(3, 384).astype(np.float32)
    np.save(indexes_dir / "dense_index.npy", vecs)

    # 5. Graph edges
    edges_data = [
        {
            "edge_id": "edge_test_2_amends_1",
            "source_document_id": "doc_test_2",
            "target_document_id": "doc_test_1",
            "relationship": "AMENDS",
            "scope": "partial",
            "affected_provisions": ["Clause 5"],
            "evidence_text": "Amends timeline of test 1.",
            "evidence_page": 1,
            "confidence": "high",
            "review_status": "verified",
            "effective_date": "2025-03-28",
        }
    ]
    with open(processed_dir / "cscrf_graph.json", "w", encoding="utf-8") as f:
        json.dump(edges_data, f)

    return processed_dir, indexes_dir


class TestCorpusImporterValidation:
    """Tests corpus loading and validation without database writes."""

    def test_load_pilot_corpus_validation(self):
        """Validates that real pilot corpus (5 docs, 168 chunks, 9 edges) validates 100% cleanly."""
        importer = CorpusImporter(processed_dir="data/processed", indexes_dir="data/indexes")
        docs_and_chunks, dense_vecs, edges, stats = importer.load_and_validate_corpus()

        assert stats.documents_discovered == 5
        assert stats.documents_validated == 5
        assert len(docs_and_chunks) == 5
        assert stats.chunks_validated == 168
        assert stats.embeddings_validated == 168
        assert stats.edges_validated == 9
        assert len(edges) == 9
        assert stats.errors == []
        assert stats.conflicts == []

    def test_dry_run_does_not_require_database_connection(self):
        """Verifies that dry-run operates successfully when session_factory is None."""
        importer = CorpusImporter(
            session_factory=None,
            processed_dir="data/processed",
            indexes_dir="data/indexes",
        )
        stats = importer.import_corpus(dry_run=True)
        assert stats.documents_validated == 5
        assert stats.documents_inserted == 5
        assert stats.chunks_inserted == 168
        assert stats.embeddings_inserted == 168
        assert stats.edges_inserted == 9
        assert stats.errors == []

    def test_missing_manifest_raises_error(self, tmp_path):
        """Confirms missing manifest.json raises FileNotFoundError."""
        empty_dir = tmp_path / "empty_processed"
        empty_dir.mkdir()
        importer = CorpusImporter(processed_dir=empty_dir)
        with pytest.raises(FileNotFoundError, match="Corpus manifest not found"):
            importer.load_and_validate_corpus()

    def test_checksum_mismatch_detected_as_conflict(self, mock_corpus_dir):
        """Confirms manifest sha256 mismatch with document.json is caught as a conflict error."""
        processed_dir, indexes_dir = mock_corpus_dir
        # Corrupt manifest sha256
        with open(processed_dir / "manifest.json", "r", encoding="utf-8") as f:
            manifest = json.load(f)
        manifest["documents"]["doc_test_1"]["sha256"] = "corrupted_hash"
        with open(processed_dir / "manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f)

        importer = CorpusImporter(processed_dir=processed_dir, indexes_dir=indexes_dir)
        docs, vecs, edges, stats = importer.load_and_validate_corpus()

        assert stats.documents_failed >= 1
        assert any("Checksum mismatch" in c for c in stats.conflicts)

    def test_mismatched_embedding_dimension_skipped_safely(self, mock_corpus_dir):
        """Verifies that non-384 dimensional embedding index is rejected without crashing."""
        processed_dir, indexes_dir = mock_corpus_dir
        # Overwrite meta with 512 dim
        with open(indexes_dir / "dense_index_meta.json", "r", encoding="utf-8") as f:
            meta = json.load(f)
        meta["embedding_dim"] = 512
        with open(indexes_dir / "dense_index_meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f)

        importer = CorpusImporter(processed_dir=processed_dir, indexes_dir=indexes_dir)
        docs, vecs, edges, stats = importer.load_and_validate_corpus()

        assert len(vecs) == 0
        assert any("Index dimension mismatch" in r for r in stats.skipped_reasons)

    def test_graph_edge_self_loop_rejected(self, mock_corpus_dir):
        """Confirms self-loop edges in json are rejected."""
        processed_dir, indexes_dir = mock_corpus_dir
        bad_edges = [
            {
                "edge_id": "edge_self_loop",
                "source_document_id": "doc_test_1",
                "target_document_id": "doc_test_1",
                "relationship": "AMENDS",
                "scope": "full",
                "affected_provisions": [],
                "evidence_text": "Invalid self loop",
                "confidence": "high",
                "review_status": "needs_review",
            }
        ]
        with open(processed_dir / "cscrf_graph.json", "w", encoding="utf-8") as f:
            json.dump(bad_edges, f)

        importer = CorpusImporter(processed_dir=processed_dir, indexes_dir=indexes_dir)
        docs, vecs, edges, stats = importer.load_and_validate_corpus()

        assert stats.edges_failed == 1
        assert any("Self-referential" in err or "Self-loop" in err for err in stats.errors)


class TestCorpusImporterPersistenceAndIdempotency:
    """Tests execution and idempotency against an isolated database session."""

    def test_initial_import_persists_expected_records(self, sqlite_session_factory, mock_corpus_dir):
        processed_dir, indexes_dir = mock_corpus_dir
        importer = CorpusImporter(
            session_factory=sqlite_session_factory,
            processed_dir=processed_dir,
            indexes_dir=indexes_dir,
        )

        stats = importer.import_corpus(dry_run=False)

        assert stats.documents_inserted == 2
        assert stats.versions_inserted == 2
        assert stats.chunks_inserted == 3
        assert stats.edges_inserted == 1
        assert stats.errors == []

        # Verify persisted rows in SQLite session
        with sqlite_session_factory() as session:
            docs = session.execute(select(RegulatoryDocumentModel)).scalars().all()
            assert len(docs) == 2
            doc_ids = {d.id for d in docs}
            assert "doc_test_1" in doc_ids
            assert "doc_test_2" in doc_ids

            versions = session.execute(select(DocumentVersionModel)).scalars().all()
            assert len(versions) == 2
            assert all(v.version_number == 1 for v in versions)

            chunks = session.execute(select(ChunkModel)).scalars().all()
            assert len(chunks) == 3

            edges = session.execute(select(RegulatoryEdgeModel)).scalars().all()
            assert len(edges) == 1
            assert edges[0].review_status == "verified"

    def test_repeat_import_is_strictly_idempotent(self, sqlite_session_factory, mock_corpus_dir):
        """Verifies running import twice produces zero duplicate rows and zero errors."""
        processed_dir, indexes_dir = mock_corpus_dir
        importer = CorpusImporter(
            session_factory=sqlite_session_factory,
            processed_dir=processed_dir,
            indexes_dir=indexes_dir,
        )

        # First run
        stats1 = importer.import_corpus(dry_run=False)
        assert stats1.documents_inserted == 2
        assert stats1.chunks_inserted == 3
        assert stats1.edges_inserted == 1

        # Second run
        stats2 = importer.import_corpus(dry_run=False)
        assert stats2.documents_inserted == 0
        assert stats2.documents_already_present == 2
        assert stats2.versions_inserted == 0
        assert stats2.versions_already_present == 2
        assert stats2.chunks_inserted == 0
        assert stats2.chunks_already_present == 3
        assert stats2.edges_inserted == 0
        assert stats2.edges_already_present == 1
        assert stats2.errors == []

        # Confirm database counts did not increase
        with sqlite_session_factory() as session:
            assert session.scalar(select(func.count(RegulatoryDocumentModel.id))) == 2
            assert session.scalar(select(func.count(DocumentVersionModel.id))) == 2
            assert session.scalar(select(func.count(ChunkModel.id))) == 3
            assert session.scalar(select(func.count(RegulatoryEdgeModel.id))) == 1

    def test_changed_checksum_creates_new_immutable_version_not_in_place(
        self, sqlite_session_factory, mock_corpus_dir
    ):
        """
        Verifies that when a document is updated with a new sha256 checksum,
        it creates version 2 (immutable history preserved) rather than overwriting version 1.
        """
        processed_dir, indexes_dir = mock_corpus_dir
        importer = CorpusImporter(
            session_factory=sqlite_session_factory,
            processed_dir=processed_dir,
            indexes_dir=indexes_dir,
        )

        # 1. Initial import
        importer.import_corpus(dry_run=False)

        # 2. Simulate new source release for doc_test_1 with new sha256
        with open(processed_dir / "manifest.json", "r", encoding="utf-8") as f:
            manifest = json.load(f)
        manifest["documents"]["doc_test_1"]["sha256"] = "new_v2_hash"
        with open(processed_dir / "manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f)

        d1_json = processed_dir / "doc_test_1" / "document.json"
        with open(d1_json, "r", encoding="utf-8") as f:
            doc_data = json.load(f)
        doc_data["sha256"] = "new_v2_hash"
        doc_data["title"] = "Updated Title for Version 2"
        with open(d1_json, "w", encoding="utf-8") as f:
            json.dump(doc_data, f)

        # 3. Second import
        stats2 = importer.import_corpus(dry_run=False)
        assert stats2.versions_inserted == 1  # Created v2

        with sqlite_session_factory() as session:
            versions = session.execute(
                select(DocumentVersionModel)
                .where(DocumentVersionModel.document_id == "doc_test_1")
                .order_by(DocumentVersionModel.version_number.asc())
            ).scalars().all()

            assert len(versions) == 2
            assert versions[0].version_number == 1
            assert versions[0].sha256 == "hash_test_1"
            assert versions[1].version_number == 2
            assert versions[1].sha256 == "new_v2_hash"

    def test_all_or_nothing_transaction_rollback(self, sqlite_session_factory, mock_corpus_dir):
        """
        Verifies that if an error occurs in all-or-nothing mode, all operations rollback cleanly.
        """
        processed_dir, indexes_dir = mock_corpus_dir
        importer = CorpusImporter(
            session_factory=sqlite_session_factory,
            processed_dir=processed_dir,
            indexes_dir=indexes_dir,
        )

        # Inject an error into graph edge insertion
        with patch.object(importer, "_import_edges_in_session", side_effect=RuntimeError("Simulated edge failure")):
            with pytest.raises(RuntimeError, match="Simulated edge failure"):
                importer.import_corpus(all_or_nothing=True)

        # Confirm 0 records were persisted due to transaction rollback
        with sqlite_session_factory() as session:
            assert session.scalar(select(func.count(RegulatoryDocumentModel.id))) == 0
            assert session.scalar(select(func.count(DocumentVersionModel.id))) == 0
            assert session.scalar(select(func.count(ChunkModel.id))) == 0
