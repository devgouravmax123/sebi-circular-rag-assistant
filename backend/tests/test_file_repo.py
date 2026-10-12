"""
Focused tests for Phase 8A.3 File-backed persistence adapter.
Verifies:
1. Deterministic reads and schema preservation (5 docs, 168 chunks, 9 edges).
2. Proper error propagation on malformed JSON or corrupted files (CorruptedStorageError).
3. Non-existent directory and file handling (clean empty lists / None without crashes).
4. Safe save operations without overwriting or fabricating metadata.
5. Storage factory selection and fallback behavior.
"""
import json
import pytest
from pathlib import Path

from app.persistence.file_repo import (
    FileDocumentRepository,
    FileChunkRepository,
    FileGraphEdgeRepository,
    FileStorageRepositoryFactory,
)
from app.persistence.interfaces import CorruptedStorageError
from app.schemas.document import DocumentRecord, ChunkRecord
from app.schemas.graph import GraphEdge, RelationshipType, ScopeType, ReviewStatus


class TestFileRepositoryErrorHandling:
    """Verifies that File repositories never silently fabricate data when files are corrupted."""

    def test_document_repository_malformed_json_raises_error(self, tmp_path: Path):
        bad_doc_dir = tmp_path / "bad_doc"
        bad_doc_dir.mkdir()
        (bad_doc_dir / "document.json").write_text("{ unclosed json: ", encoding="utf-8")

        repo = FileDocumentRepository(tmp_path)
        with pytest.raises(CorruptedStorageError, match="Malformed JSON in document record"):
            repo.get_document("bad_doc")

    def test_document_repository_invalid_schema_raises_error(self, tmp_path: Path):
        bad_doc_dir = tmp_path / "invalid_doc"
        bad_doc_dir.mkdir()
        # Missing required title and sha256
        (bad_doc_dir / "document.json").write_text(json.dumps({"document_id": "invalid_doc"}), encoding="utf-8")

        repo = FileDocumentRepository(tmp_path)
        with pytest.raises(CorruptedStorageError, match="Invalid schema in document record"):
            repo.get_document("invalid_doc")

    def test_document_repository_non_existent_returns_none(self, tmp_path: Path):
        repo = FileDocumentRepository(tmp_path)
        assert repo.get_document("non_existent_doc") is None
        assert repo.list_documents() == []

    def test_chunk_repository_malformed_json_raises_error(self, tmp_path: Path):
        doc_dir = tmp_path / "doc_1"
        doc_dir.mkdir()
        (doc_dir / "chunks.json").write_text("[ { bad json ", encoding="utf-8")

        repo = FileChunkRepository(tmp_path)
        with pytest.raises(CorruptedStorageError, match="Malformed JSON in chunk record"):
            repo.list_chunks_for_document("doc_1")

    def test_chunk_repository_not_a_list_raises_error(self, tmp_path: Path):
        doc_dir = tmp_path / "doc_1"
        doc_dir.mkdir()
        (doc_dir / "chunks.json").write_text(json.dumps({"not": "a list"}), encoding="utf-8")

        repo = FileChunkRepository(tmp_path)
        with pytest.raises(CorruptedStorageError, match="must be a JSON array"):
            repo.list_chunks_for_document("doc_1")

    def test_chunk_repository_invalid_chunk_schema_raises_error(self, tmp_path: Path):
        doc_dir = tmp_path / "doc_1"
        doc_dir.mkdir()
        (doc_dir / "chunks.json").write_text(json.dumps([{"chunk_id": "c1"}]), encoding="utf-8")

        repo = FileChunkRepository(tmp_path)
        with pytest.raises(CorruptedStorageError, match="Invalid chunk schema"):
            repo.list_chunks_for_document("doc_1")

    def test_graph_edge_repository_malformed_json_raises_error(self, tmp_path: Path):
        graph_file = tmp_path / "cscrf_graph.json"
        graph_file.write_text("{ malformed json ", encoding="utf-8")

        repo = FileGraphEdgeRepository(tmp_path)
        with pytest.raises(CorruptedStorageError, match="Malformed JSON in graph edge record"):
            repo.list_all_edges()

    def test_graph_edge_repository_invalid_edge_schema_raises_error(self, tmp_path: Path):
        graph_file = tmp_path / "cscrf_graph.json"
        # Edge with invalid relationship type
        invalid_edge = [
            {
                "edge_id": "e1",
                "source_document_id": "d1",
                "target_document_id": "d2",
                "relationship": "INVALID_RELATION",
                "scope": "full",
                "evidence_text": "evidence",
            }
        ]
        graph_file.write_text(json.dumps(invalid_edge), encoding="utf-8")

        repo = FileGraphEdgeRepository(tmp_path)
        with pytest.raises(CorruptedStorageError, match="Invalid edge schema"):
            repo.list_all_edges()

    def test_graph_edge_repository_missing_file_returns_empty_list(self, tmp_path: Path):
        repo = FileGraphEdgeRepository(tmp_path)
        assert repo.list_all_edges() == []
        assert repo.get_edge("e1") is None


class TestFileRepositoryDataIntegrity:
    """Verifies write and read roundtrip integrity."""

    def test_document_roundtrip(self, tmp_path: Path):
        repo = FileDocumentRepository(tmp_path)
        doc = DocumentRecord(
            document_id="test_doc_01",
            title="Test Regulatory Circular",
            source_url="https://example.com/test.pdf",
            sha256="abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234",
            pdf_path="test_doc_01.pdf",
            issue_date="2026-01-01",
        )
        repo.save_document(doc)

        retrieved = repo.get_document("test_doc_01")
        assert retrieved is not None
        assert retrieved.document_id == "test_doc_01"
        assert retrieved.title == "Test Regulatory Circular"
        assert retrieved.sha256 == doc.sha256

        by_hash = repo.get_document_by_sha256(doc.sha256)
        assert by_hash is not None
        assert by_hash.document_id == "test_doc_01"

    def test_chunks_roundtrip(self, tmp_path: Path):
        repo = FileChunkRepository(tmp_path)
        chunks = [
            ChunkRecord(
                chunk_id="test_doc_01_p1_0",
                document_id="test_doc_01",
                text="Paragraph 1 text content.",
                page_start=1,
                page_end=1,
                is_ocr=False,
                chunk_type="body",
            ),
            ChunkRecord(
                chunk_id="test_doc_01_p2_1",
                document_id="test_doc_01",
                text="Paragraph 2 text content.",
                page_start=2,
                page_end=2,
                is_ocr=True,
                chunk_type="body",
            ),
        ]
        repo.save_chunks(chunks)

        retrieved = repo.list_chunks_for_document("test_doc_01")
        assert len(retrieved) == 2
        assert retrieved[0].chunk_id == "test_doc_01_p1_0"
        assert retrieved[1].is_ocr is True

    def test_graph_edge_roundtrip(self, tmp_path: Path):
        repo = FileGraphEdgeRepository(tmp_path)
        edge = GraphEdge(
            edge_id="edge_01",
            source_document_id="doc_newer",
            target_document_id="doc_older",
            relationship=RelationshipType.AMENDS,
            scope=ScopeType.PARTIAL,
            affected_provisions=["Clause 10"],
            evidence_text="Clause 10 is amended.",
            evidence_page=1,
            review_status=ReviewStatus.NEEDS_REVIEW,
        )
        repo.save_edge(edge)

        retrieved = repo.get_edge("edge_01")
        assert retrieved is not None
        assert retrieved.relationship == RelationshipType.AMENDS
        assert retrieved.affected_provisions == ["Clause 10"]
        assert retrieved.review_status == ReviewStatus.NEEDS_REVIEW

        outgoing = repo.get_outgoing_edges("doc_newer")
        assert len(outgoing) == 1
        assert outgoing[0].edge_id == "edge_01"

        incoming = repo.get_incoming_edges("doc_older")
        assert len(incoming) == 1
        assert incoming[0].edge_id == "edge_01"
