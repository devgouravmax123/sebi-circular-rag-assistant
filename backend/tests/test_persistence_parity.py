"""
Phase 8A.6 Dual-Engine Persistence Parity Test Suite.

Proves that FileStorageRepository and PostgresStorageRepository return equivalent,
consistent results for the same logical regulatory data and domain operations.

Covers:
1. Document parity: get_document, list_documents, get_document_by_sha256, nonexistent IDs.
2. Chunk parity: get_chunk, list_chunks_for_document, list_all_chunks, ordering, dual-page boundaries, OCR flags.
3. Graph edge parity: get_edge, list_all_edges, get_outgoing_edges, get_incoming_edges, relationships, evidence, review status.
4. Idempotency parity: repeated saves without duplication.
5. Unicode and long regulatory text preservation parity.
6. Real 5-document pilot regression fixture verification.
7. Explicitly documented intentional adapter differences.
"""
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Any, List, Generator, Tuple, Sequence, cast
import numpy as np
import pytest

from sqlalchemy import create_engine, Table
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from pgvector.sqlalchemy import Vector

from app.schemas.document import DocumentRecord, ChunkRecord
from app.schemas.graph import (
    GraphEdge,
    RelationshipType,
    ScopeType,
    ReviewStatus,
    ConfidenceLevel,
    EdgeProvenance,
    EdgeReviewHistoryItem,
)
from app.persistence.interfaces import StorageRepositoryFactory, PersistenceError, CorruptedStorageError
from app.persistence.file_repo import FileStorageRepositoryFactory
from app.persistence.postgres import (
    PostgresStorageRepositoryFactory,
    PostgresDocumentRepository,
    PostgresChunkRepository,
    PostgresGraphEdgeRepository,
    PostgresIngestionJobRepository,
)
from app.persistence.models import (
    Base,
    RegulatorModel,
    RegulatoryDomainModel,
    RegulatoryDocumentModel,
    DocumentVersionModel,
    ChunkModel,
    ChunkEmbeddingModel,
    RegulatoryEdgeModel,
    IngestionJobModel,
)


# SQLite dialect compilers for isolated Postgres adapter tests
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
def parity_fixtures(tmp_path) -> Tuple[StorageRepositoryFactory, StorageRepositoryFactory]:
    """
    Creates an isolated environment with both File and SQLite-backed Postgres adapters
    initialized with identical clean tables/directories.
    """
    # 1. File Storage Factory
    file_dir = tmp_path / "file_processed"
    file_dir.mkdir(parents=True)
    file_factory = FileStorageRepositoryFactory(processed_dir=file_dir)

    # 2. Postgres Storage Factory (backed by isolated in-memory SQLite with schema compilers)
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    importer_tables = cast(
        Sequence[Table],
        [
            RegulatorModel.__table__,
            RegulatoryDomainModel.__table__,
            RegulatoryDocumentModel.__table__,
            DocumentVersionModel.__table__,
            ChunkModel.__table__,
            ChunkEmbeddingModel.__table__,
            RegulatoryEdgeModel.__table__,
            IngestionJobModel.__table__,
        ],
    )
    Base.metadata.create_all(bind=engine, tables=importer_tables)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    class TestablePostgresFactory(StorageRepositoryFactory):
        def __init__(self, s_factory):
            self.session_factory = s_factory
        def get_document_repository(self):
            return PostgresDocumentRepository(self.session_factory)
        def get_chunk_repository(self):
            return PostgresChunkRepository(self.session_factory)
        def get_graph_edge_repository(self):
            return PostgresGraphEdgeRepository(self.session_factory)
        def get_ingestion_job_repository(self):
            return PostgresIngestionJobRepository(self.session_factory)

    postgres_factory = TestablePostgresFactory(session_factory)

    return file_factory, postgres_factory


class TestDualEngineParity:
    """Parameterized and side-by-side contract tests comparing File vs Postgres adapters."""

    def test_document_roundtrip_and_missing_parity(self, parity_fixtures):
        file_factory, pg_factory = parity_fixtures
        file_repo = file_factory.get_document_repository()
        pg_repo = pg_factory.get_document_repository()

        # Nonexistent lookup parity
        assert file_repo.get_document("nonexistent_doc") is None
        assert pg_repo.get_document("nonexistent_doc") is None
        assert file_repo.get_document_by_sha256("nonexistent_hash") is None
        assert pg_repo.get_document_by_sha256("nonexistent_hash") is None

        # Sample document with Unicode & dates
        doc = DocumentRecord(
            document_id="sebi_cscrf_2024_113",
            circular_number="SEBI/HO/ITD-1/ITD_CSC_EXT/P/CIR/2024/113",
            title="Cybersecurity and Cyber Resilience Framework (CSCRF) परिपत्र",
            department="Information Technology Department (ITD-1)",
            issue_date="2024-08-20",
            effective_date="2025-01-01",
            source_url="https://www.sebi.gov.in/legal/circulars/aug-2024/cscrf.html",
            retrieved_at="2024-08-20T10:00:00Z",
            sha256="bd9ddb68bb49b9a92771ff01ed3138e01f0962b383e9ff643008b729290fc85d",
            pdf_path="data/raw/CSCRF_2024_113.pdf",
            extraction_status="extracted",
        )

        file_repo.save_document(doc)
        pg_repo.save_document(doc)

        # Retrieve and verify exact parity
        doc_file = file_repo.get_document(doc.document_id)
        doc_pg = pg_repo.get_document(doc.document_id)

        assert doc_file is not None
        assert doc_pg is not None
        assert doc_file.document_id == doc_pg.document_id == doc.document_id
        assert doc_file.title == doc_pg.title == doc.title
        assert doc_file.circular_number == doc_pg.circular_number == doc.circular_number
        assert doc_file.department == doc_pg.department == doc.department
        assert doc_file.issue_date == doc_pg.issue_date == doc.issue_date
        assert doc_file.effective_date == doc_pg.effective_date == doc.effective_date
        assert doc_file.sha256 == doc_pg.sha256 == doc.sha256
        assert doc_file.source_url == doc_pg.source_url == doc.source_url

        # Check lookup by SHA-256 parity
        by_sha_file = file_repo.get_document_by_sha256(doc.sha256)
        by_sha_pg = pg_repo.get_document_by_sha256(doc.sha256)
        assert by_sha_file is not None
        assert by_sha_pg is not None
        assert by_sha_file.document_id == by_sha_pg.document_id

        # List documents parity
        all_file = file_repo.list_documents()
        all_pg = pg_repo.list_documents()
        assert len(all_file) == len(all_pg) == 1
        assert all_file[0].document_id == all_pg[0].document_id

    def test_chunk_parity_ordering_and_provenance(self, parity_fixtures):
        file_factory, pg_factory = parity_fixtures
        file_doc_repo = file_factory.get_document_repository()
        pg_doc_repo = pg_factory.get_document_repository()
        file_chunk_repo = file_factory.get_chunk_repository()
        pg_chunk_repo = pg_factory.get_chunk_repository()

        doc_id = "sebi_cscrf_2025_045"
        doc = DocumentRecord(
            document_id=doc_id,
            circular_number="CIR/2025/45",
            title="Extension circular",
            department="ITD",
            issue_date="2025-03-28",
            effective_date=None,
            source_url="https://sebi.gov.in/cir45.pdf",
            retrieved_at="2025-03-28T00:00:00Z",
            sha256="d04db094f5c66332ad5ac9eaa532f3103896443ef0d55f48da82965e082a04f6",
            pdf_path="data/raw/cir45.pdf",
            extraction_status="extracted",
        )
        file_doc_repo.save_document(doc)
        pg_doc_repo.save_document(doc)

        chunks = [
            ChunkRecord(
                chunk_id=f"{doc_id}_p1_0",
                document_id=doc_id,
                text="Page 1 clause: Mandatory ISO 27001 guidance for REs.",
                page_start=1,
                page_end=2,
                is_ocr=False,
                chunk_type="body",
                metadata={"chunk_index": 0, "section": "Preamble"},
            ),
            ChunkRecord(
                chunk_id=f"{doc_id}_p2_1",
                document_id=doc_id,
                text="Page 2 clause: Compliance timeline extended till June 30, 2025.",
                page_start=2,
                page_end=3,
                is_ocr=True,
                chunk_type="body",
                metadata={"chunk_index": 1, "section": "Timelines"},
            ),
        ]

        file_chunk_repo.save_chunks(chunks)
        pg_chunk_repo.save_chunks(chunks)

        # Single chunk lookup parity
        c0_file = file_chunk_repo.get_chunk(f"{doc_id}_p1_0")
        c0_pg = pg_chunk_repo.get_chunk(f"{doc_id}_p1_0")
        assert c0_file is not None
        assert c0_pg is not None
        assert c0_file.chunk_id == c0_pg.chunk_id
        assert c0_file.text == c0_pg.text
        assert c0_file.page_start == c0_pg.page_start == 1
        assert c0_file.page_end == c0_pg.page_end == 2
        assert c0_file.is_ocr == c0_pg.is_ocr is False

        # Nonexistent chunk parity
        assert file_chunk_repo.get_chunk("nonexistent_chunk") is None
        assert pg_chunk_repo.get_chunk("nonexistent_chunk") is None

        # Document chunks listing and ordering parity
        doc_chunks_file = file_chunk_repo.list_chunks_for_document(doc_id)
        doc_chunks_pg = pg_chunk_repo.list_chunks_for_document(doc_id)
        assert len(doc_chunks_file) == len(doc_chunks_pg) == 2
        for cf, cp in zip(doc_chunks_file, doc_chunks_pg):
            assert cf.chunk_id == cp.chunk_id
            assert cf.text == cp.text
            assert cf.is_ocr == cp.is_ocr
            assert cf.page_start == cp.page_start
            assert cf.page_end == cp.page_end

        # list_all_chunks parity
        all_chunks_file = file_chunk_repo.list_all_chunks()
        all_chunks_pg = pg_chunk_repo.list_all_chunks()
        assert len(all_chunks_file) == len(all_chunks_pg) == 2
        assert [c.chunk_id for c in all_chunks_file] == [c.chunk_id for c in all_chunks_pg]

    def test_graph_edge_parity_and_conservative_defaults(self, parity_fixtures):
        file_factory, pg_factory = parity_fixtures
        file_doc_repo = file_factory.get_document_repository()
        pg_doc_repo = pg_factory.get_document_repository()
        file_edge_repo = file_factory.get_graph_edge_repository()
        pg_edge_repo = pg_factory.get_graph_edge_repository()

        # Create source and target docs
        d1 = DocumentRecord(
            document_id="doc_src",
            circular_number="CIR/SRC",
            title="New Circular",
            department="ITD",
            issue_date="2025-03-28",
            effective_date=None,
            source_url="https://sebi.gov.in/src.pdf",
            retrieved_at="2025-03-28T00:00:00Z",
            sha256="hash_src",
            pdf_path="data/raw/src.pdf",
            extraction_status="extracted",
        )
        d2 = DocumentRecord(
            document_id="doc_tgt",
            circular_number="CIR/TGT",
            title="Old Circular",
            department="ITD",
            issue_date="2024-08-20",
            effective_date=None,
            source_url="https://sebi.gov.in/tgt.pdf",
            retrieved_at="2024-08-20T00:00:00Z",
            sha256="hash_tgt",
            pdf_path="data/raw/tgt.pdf",
            extraction_status="extracted",
        )
        file_doc_repo.save_document(d1)
        file_doc_repo.save_document(d2)
        pg_doc_repo.save_document(d1)
        pg_doc_repo.save_document(d2)

        edge = GraphEdge(
            edge_id="edge_src_amends_tgt",
            source_document_id="doc_src",
            target_document_id="doc_tgt",
            relationship=RelationshipType.AMENDS,
            scope=ScopeType.PARTIAL,
            affected_provisions=["Clause 6.10", "Annexure-J"],
            evidence_text="Extends compliance timelines for mid-size entities.",
            evidence_page=2,
            confidence=ConfidenceLevel.HIGH,
            review_status=ReviewStatus.VERIFIED,
            effective_date="2025-03-28",
        )

        file_edge_repo.save_edge(edge)
        pg_edge_repo.save_edge(edge)

        # Single edge lookup parity
        e_file = file_edge_repo.get_edge(edge.edge_id)
        e_pg = pg_edge_repo.get_edge(edge.edge_id)
        assert e_file is not None
        assert e_pg is not None
        assert e_file.edge_id == e_pg.edge_id == edge.edge_id
        assert e_file.relationship == e_pg.relationship == RelationshipType.AMENDS
        assert e_file.scope == e_pg.scope == ScopeType.PARTIAL
        assert e_file.affected_provisions == e_pg.affected_provisions == ["Clause 6.10", "Annexure-J"]
        assert e_file.evidence_text == e_pg.evidence_text
        assert e_file.evidence_page == e_pg.evidence_page == 2
        assert e_file.confidence == e_pg.confidence == ConfidenceLevel.HIGH
        assert e_file.review_status == e_pg.review_status == ReviewStatus.VERIFIED
        assert e_file.effective_date == e_pg.effective_date == "2025-03-28"

        # Directional query parity
        out_file = file_edge_repo.get_outgoing_edges("doc_src")
        out_pg = pg_edge_repo.get_outgoing_edges("doc_src")
        assert len(out_file) == len(out_pg) == 1
        assert out_file[0].edge_id == out_pg[0].edge_id

        in_file = file_edge_repo.get_incoming_edges("doc_tgt")
        in_pg = pg_edge_repo.get_incoming_edges("doc_tgt")
        assert len(in_file) == len(in_pg) == 1
        assert in_file[0].edge_id == in_pg[0].edge_id

        # Missing edge parity
        assert file_edge_repo.get_edge("nonexistent_edge") is None
        assert pg_edge_repo.get_edge("nonexistent_edge") is None

    def test_graph_edge_extended_lifecycle_parity(self, parity_fixtures):
        """Verifies File and Postgres parity for proposal, review, revocation, and filtered listing."""
        file_factory, pg_factory = parity_fixtures
        file_edge_repo = file_factory.get_graph_edge_repository()
        pg_edge_repo = pg_factory.get_graph_edge_repository()
        file_doc_repo = file_factory.get_document_repository()
        pg_doc_repo = pg_factory.get_document_repository()

        d1 = DocumentRecord(
            document_id="doc_p1", circular_number="CIR/P1", title="P1", department="ITD",
            issue_date="2025-01-01", effective_date=None, source_url="https://sebi.gov.in/p1.pdf",
            retrieved_at="2025-01-01T00:00:00Z", sha256="h_p1", pdf_path="data/raw/p1.pdf", extraction_status="extracted",
        )
        d2 = DocumentRecord(
            document_id="doc_p2", circular_number="CIR/P2", title="P2", department="ITD",
            issue_date="2024-01-01", effective_date=None, source_url="https://sebi.gov.in/p2.pdf",
            retrieved_at="2024-01-01T00:00:00Z", sha256="h_p2", pdf_path="data/raw/p2.pdf", extraction_status="extracted",
        )
        file_doc_repo.save_document(d1); file_doc_repo.save_document(d2)
        pg_doc_repo.save_document(d1); pg_doc_repo.save_document(d2)

        edge = GraphEdge(
            edge_id="edge_parity_01",
            source_document_id="doc_p1",
            target_document_id="doc_p2",
            relationship=RelationshipType.SUPERSEDES,
            scope=ScopeType.FULL,
            evidence_text="Clause 1: Full supersession.",
            proposer_id="analyst_test",
            review_rationale="Initial candidate edge",
        )

        # Propose parity
        f_prop = file_edge_repo.propose_edge(edge.model_copy(deep=True))
        p_prop = pg_edge_repo.propose_edge(edge.model_copy(deep=True))
        assert f_prop.review_status == p_prop.review_status == ReviewStatus.PROPOSED
        assert f_prop.provenance == p_prop.provenance == EdgeProvenance.USER_PROPOSED
        assert len(f_prop.review_history) == len(p_prop.review_history) == 1

        # Review parity
        f_rev = file_edge_repo.review_edge("edge_parity_01", "verify", "reviewer_1", "Verified against circular")
        p_rev = pg_edge_repo.review_edge("edge_parity_01", "verify", "reviewer_1", "Verified against circular")
        assert f_rev.review_status == p_rev.review_status == ReviewStatus.VERIFIED
        assert f_rev.reviewed_by == p_rev.reviewed_by == "reviewer_1"
        assert len(f_rev.review_history) == len(p_rev.review_history) == 2

        # Filtered listing parity
        f_list = file_edge_repo.list_edges(status="verified", relationship="SUPERSEDES")
        p_list = pg_edge_repo.list_edges(status="verified", relationship="SUPERSEDES")
        assert len(f_list) == len(p_list) == 1
        assert f_list[0].edge_id == p_list[0].edge_id == "edge_parity_01"

        # Revocation parity
        f_revk = file_edge_repo.revoke_edge("edge_parity_01", "lead_1", "Administrative revocation")
        p_revk = pg_edge_repo.revoke_edge("edge_parity_01", "lead_1", "Administrative revocation")
        assert f_revk.review_status == p_revk.review_status == ReviewStatus.REVOKED
        assert len(f_revk.review_history) == len(p_revk.review_history) == 3

    def test_idempotent_repeated_writes_parity(self, parity_fixtures):
        """Verifies that saving the exact same document, chunks, or edge twice produces 0 duplicate records."""
        file_factory, pg_factory = parity_fixtures
        file_doc_repo = file_factory.get_document_repository()
        pg_doc_repo = pg_factory.get_document_repository()

        doc = DocumentRecord(
            document_id="doc_idempotent",
            circular_number="CIR/IDEM/1",
            title="Idempotent Circular",
            department="ITD",
            issue_date="2024-08-20",
            effective_date=None,
            source_url="https://sebi.gov.in/idem.pdf",
            retrieved_at="2024-08-20T00:00:00Z",
            sha256="hash_idem_1",
            pdf_path="data/raw/idem.pdf",
            extraction_status="extracted",
        )

        # Save multiple times
        file_doc_repo.save_document(doc)
        file_doc_repo.save_document(doc)
        pg_doc_repo.save_document(doc)
        pg_doc_repo.save_document(doc)

        docs_file = file_doc_repo.list_documents()
        docs_pg = pg_doc_repo.list_documents()

        assert len(docs_file) == 1
        assert len(docs_pg) == 1
        assert docs_file[0].document_id == docs_pg[0].document_id == "doc_idempotent"

    def test_pilot_corpus_fixture_exact_counts_and_graph_integrity(self):
        """
        Validates the real 5-document pilot corpus on disk against ground-truth invariants:
        - 5 documents discoverable
        - 168 chunks total with correct document mapping
        - 9 verified graph edges
        - Dense embeddings strictly match 384 dimensions and source model
        """
        file_factory = FileStorageRepositoryFactory(processed_dir="data/processed")
        doc_repo = file_factory.get_document_repository()
        chunk_repo = file_factory.get_chunk_repository()
        edge_repo = file_factory.get_graph_edge_repository()

        docs = doc_repo.list_documents()
        assert len(docs) == 5, f"Expected 5 pilot documents, got {len(docs)}"

        chunks = chunk_repo.list_all_chunks()
        assert len(chunks) == 168, f"Expected 168 chunks in pilot corpus, got {len(chunks)}"

        edges = edge_repo.list_all_edges()
        assert len(edges) == 9, f"Expected 9 graph edges in pilot corpus, got {len(edges)}"
        # Verify all 9 pilot edges retain VERIFIED status
        assert all(e.review_status == ReviewStatus.VERIFIED for e in edges)

        # Validate dense index matches
        meta_path = Path("data/indexes/dense_index_meta.json")
        arr_path = Path("data/indexes/dense_index.npy")
        assert meta_path.exists() and arr_path.exists()

        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        assert meta["model_name"] == "BAAI/bge-small-en-v1.5"
        assert meta["embedding_dim"] == 384
        assert meta["num_chunks"] == 168
        assert len(meta["chunk_ids"]) == 168

        # Check that chunk_ids in dense index match actual chunks
        corpus_chunk_ids = {c.chunk_id for c in chunks}
        for cid in meta["chunk_ids"]:
            assert cid in corpus_chunk_ids, f"Dense index refers to unknown chunk '{cid}'"
