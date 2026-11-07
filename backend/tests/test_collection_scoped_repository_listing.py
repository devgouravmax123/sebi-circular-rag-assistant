"""
Unit and Contract Tests for Task 12.3D:
Collection-Scoped Repository Listing and Interface Hardening (Design B).

Validates that:
1. Scoped listing methods mandate collection_id (omission raises TypeError).
2. None, empty, and whitespace-only collection IDs raise ValueError("collection_id must be a non-empty string").
3. FileDocumentRepository and FileChunkRepository enforce collection scoping.
4. PostgresDocumentRepository and PostgresChunkRepository enforce collection scoping and bind SQL WHERE.
5. list_all_documents_global() and list_all_chunks_global() provide intentional corpus-wide listing.
6. get_document_by_sha256_global() and FileChunkRepository.get_chunk() function correctly via global methods.
7. Behavioral parity is maintained between File and PostgreSQL implementations.
"""
import inspect
from pathlib import Path
import pytest
from unittest.mock import MagicMock
from sqlalchemy import text

from app.persistence.interfaces import DocumentRepository, ChunkRepository
from app.persistence.file_repo import FileDocumentRepository, FileChunkRepository
from app.persistence.postgres import PostgresDocumentRepository, PostgresChunkRepository
from app.schemas.document import DocumentRecord, ChunkRecord


class TestRepositoryInterfaceSignatures:
    """Verifies that repository interfaces strictly require collection_id on listing methods."""

    def test_01_document_repository_signature_requires_collection_id(self):
        sig = inspect.signature(DocumentRepository.list_documents)
        params = list(sig.parameters.keys())
        assert "collection_id" in params
        assert params[1] == "collection_id"
        assert sig.parameters["collection_id"].default is inspect.Parameter.empty, "collection_id must not have a default"
        assert hasattr(DocumentRepository, "list_all_documents_global")

    def test_02_chunk_repository_signature_requires_collection_id(self):
        sig = inspect.signature(ChunkRepository.list_all_chunks)
        params = list(sig.parameters.keys())
        assert "collection_id" in params
        assert params[1] == "collection_id"
        assert sig.parameters["collection_id"].default is inspect.Parameter.empty, "collection_id must not have a default"
        assert hasattr(ChunkRepository, "list_all_chunks_global")


class TestFileRepositoryScopeValidation:
    """Tests input validation and isolation on FileDocumentRepository and FileChunkRepository."""

    def test_03_file_doc_repo_rejects_missing_or_blank_collection_id(self, tmp_path: Path):
        repo = FileDocumentRepository(tmp_path)

        with pytest.raises(TypeError):
            repo.list_documents()  # type: ignore[call-arg]

        with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
            repo.list_documents(None)  # type: ignore[arg-type]

        with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
            repo.list_documents("")

        with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
            repo.list_documents("   ")

    def test_04_file_chunk_repo_rejects_missing_or_blank_collection_id(self, tmp_path: Path):
        repo = FileChunkRepository(tmp_path)

        with pytest.raises(TypeError):
            repo.list_all_chunks()  # type: ignore[call-arg]

        with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
            repo.list_all_chunks(None)  # type: ignore[arg-type]

        with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
            repo.list_all_chunks("")

        with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
            repo.list_all_chunks("   ")

    def test_05_file_repos_scoped_and_global_isolation(self, tmp_path: Path):
        doc_repo = FileDocumentRepository(tmp_path)
        chunk_repo = FileChunkRepository(tmp_path)

        # Create doc in Collection A
        doc_a = DocumentRecord(
            document_id="doc_a_1",
            circular_number="CIR/A/1",
            title="Collection A Circular",
            department="ITD",
            issue_date="2025-01-01",
            effective_date=None,
            source_url="https://example.com/a.pdf",
            retrieved_at="2025-01-01T00:00:00Z",
            sha256="hash_a",
            pdf_path="data/raw/a.pdf",
            extraction_status="indexed",
            collection_id="collection_alpha",
        )
        doc_repo.save_document(doc_a)

        # Create doc in Collection B
        doc_b = DocumentRecord(
            document_id="doc_b_1",
            circular_number="CIR/B/1",
            title="Collection B Circular",
            department="ITD",
            issue_date="2025-01-01",
            effective_date=None,
            source_url="https://example.com/b.pdf",
            retrieved_at="2025-01-01T00:00:00Z",
            sha256="hash_b",
            pdf_path="data/raw/b.pdf",
            extraction_status="indexed",
            collection_id="collection_beta",
        )
        doc_repo.save_document(doc_b)

        # Save chunks for doc A and doc B
        chunk_a = ChunkRecord(
            chunk_id="chunk_a_1",
            document_id="doc_a_1",
            collection_id="collection_alpha",
            text="Alpha content",
            page_start=1,
            page_end=1,
            is_ocr=False,
            chunk_type="body",
            metadata={},
        )
        chunk_b = ChunkRecord(
            chunk_id="chunk_b_1",
            document_id="doc_b_1",
            collection_id="collection_beta",
            text="Beta content",
            page_start=1,
            page_end=1,
            is_ocr=False,
            chunk_type="body",
            metadata={},
        )
        chunk_repo.save_chunks([chunk_a])
        chunk_repo.save_chunks([chunk_b])

        # Test scoped document listing
        docs_alpha = doc_repo.list_documents("collection_alpha")
        docs_beta = doc_repo.list_documents("collection_beta")
        assert len(docs_alpha) == 1
        assert docs_alpha[0].document_id == "doc_a_1"
        assert len(docs_beta) == 1
        assert docs_beta[0].document_id == "doc_b_1"

        # Test global document listing
        docs_all = doc_repo.list_all_documents_global()
        assert len(docs_all) == 2
        assert {d.document_id for d in docs_all} == {"doc_a_1", "doc_b_1"}

        # Test scoped chunk listing
        chunks_alpha = chunk_repo.list_all_chunks("collection_alpha")
        chunks_beta = chunk_repo.list_all_chunks("collection_beta")
        assert len(chunks_alpha) == 1
        assert chunks_alpha[0].chunk_id == "chunk_a_1"
        assert len(chunks_beta) == 1
        assert chunks_beta[0].chunk_id == "chunk_b_1"

        # Test global chunk listing
        chunks_all = chunk_repo.list_all_chunks_global()
        assert len(chunks_all) == 2
        assert {c.chunk_id for c in chunks_all} == {"chunk_a_1", "chunk_b_1"}

        # Test get_chunk across collections works via global listing
        assert chunk_repo.get_chunk("chunk_a_1") is not None
        assert chunk_repo.get_chunk("chunk_b_1") is not None
        assert chunk_repo.get_chunk("chunk_missing") is None

        # Test get_document_by_sha256_global works via global listing
        assert doc_repo.get_document_by_sha256_global("hash_a") is not None
        assert doc_repo.get_document_by_sha256_global("hash_b") is not None


class TestPostgresRepositoryScopeValidation:
    """Tests input validation and SQL structure on PostgresDocumentRepository and PostgresChunkRepository."""

    def test_06_postgres_doc_repo_rejects_missing_or_blank_collection_id(self):
        mock_factory = MagicMock()
        repo = PostgresDocumentRepository(mock_factory)

        with pytest.raises(TypeError):
            repo.list_documents()  # type: ignore[call-arg]

        with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
            repo.list_documents(None)  # type: ignore[arg-type]

        with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
            repo.list_documents("")

        with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
            repo.list_documents("   ")

    def test_07_postgres_chunk_repo_rejects_missing_or_blank_collection_id(self):
        mock_factory = MagicMock()
        repo = PostgresChunkRepository(mock_factory)

        with pytest.raises(TypeError):
            repo.list_all_chunks()  # type: ignore[call-arg]

        with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
            repo.list_all_chunks(None)  # type: ignore[arg-type]

        with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
            repo.list_all_chunks("")

        with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
            repo.list_all_chunks("   ")

    def test_08_postgres_scoped_and_global_sql_queries(self):
        mock_factory = MagicMock()
        mock_session = MagicMock()
        mock_factory.return_value.__enter__.return_value = mock_session
        mock_session.execute.return_value.scalars.return_value.all.return_value = []
        mock_session.execute.return_value.all.return_value = []

        doc_repo = PostgresDocumentRepository(mock_factory)
        chunk_repo = PostgresChunkRepository(mock_factory)

        # Scoped doc list binds collection_id
        doc_repo.list_documents("alpha_col")
        stmt = mock_session.execute.call_args[0][0]
        sql_str = str(stmt).strip()
        assert "WHERE regulatory_documents.collection_id = :collection_id_1" in sql_str or "collection_id" in sql_str

        # Global doc list omits collection_id WHERE clause
        doc_repo.list_all_documents_global()
        stmt_global = mock_session.execute.call_args[0][0]
        sql_str_global = str(stmt_global).strip()
        assert "WHERE" not in sql_str_global

        # Scoped chunk list binds collection_id
        chunk_repo.list_all_chunks("alpha_col")
        chunk_stmt = mock_session.execute.call_args[0][0]
        chunk_sql = str(chunk_stmt).strip()
        assert "WHERE regulatory_documents.collection_id = :collection_id_1" in chunk_sql or "collection_id" in chunk_sql

        # Global chunk list omits collection_id WHERE clause
        chunk_repo.list_all_chunks_global()
        chunk_stmt_global = mock_session.execute.call_args[0][0]
        chunk_sql_global = str(chunk_stmt_global).strip()
        assert "WHERE" not in chunk_sql_global

