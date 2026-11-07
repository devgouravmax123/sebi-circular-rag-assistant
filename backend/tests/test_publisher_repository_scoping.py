"""
Focused Unit Tests for Milestone 12 - Wave 3, Task 12.3B:
Explicit Collection Scoping for Publisher Repository Reads.

Validates:
1. PostgresDocumentRepository applies SQL-level WHERE clause when collection_id is provided.
2. PostgresDocumentRepository maintains unscoped query when collection_id is None.
3. PostgresChunkRepository joins through DocumentVersionModel to RegulatoryDocumentModel and applies SQL WHERE clause.
4. PostgresChunkRepository maintains unscoped query when collection_id is None.
5. FileDocumentRepository and FileChunkRepository correctly scope by collection_id while preserving unscoped behavior when None.
6. IndexPublisher propagates collection_id to both doc_repo.list_documents() and chunk_repo.list_all_chunks().
7. Snapshot publication contents remain strictly confined to the targeted collection.
"""
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock
import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

from app.persistence.interfaces import StorageRepositoryFactory
from app.persistence.file_repo import FileDocumentRepository, FileChunkRepository, FileStorageRepositoryFactory
from app.persistence.postgres import PostgresDocumentRepository, PostgresChunkRepository
from app.retrieval.publisher import IndexPublisher
from app.schemas.document import DocumentRecord, ChunkRecord
from app.storage import LocalStorageAdapter


# -------------------------------------------------------------------------
# 1. SQL Compilation & Predicate Verification (Unit / Offline)
# -------------------------------------------------------------------------

def test_01_postgres_document_repository_sql_collection_filter():
    """Verify that PostgresDocumentRepository produces SQL with WHERE collection_id when passed."""
    captured_stmts = []

    mock_session = MagicMock()
    def fake_execute(stmt, *args, **kwargs):
        captured_stmts.append(stmt)
        res = MagicMock()
        res.scalars.return_value.all.return_value = []
        return res
    mock_session.execute = fake_execute

    session_factory = MagicMock(return_value=mock_session)
    mock_session.__enter__.return_value = mock_session

    repo = PostgresDocumentRepository(session_factory)

    # 1. Scoped query
    repo.list_documents(collection_id="test_col_123")
    assert len(captured_stmts) == 1
    compiled_sql = str(captured_stmts[0].compile(dialect=postgresql.dialect()))
    assert "WHERE regulatory_documents.collection_id =" in compiled_sql
    assert "ORDER BY regulatory_documents.id" in compiled_sql

    # 2. Missing or invalid collection_id raises
    with pytest.raises(TypeError):
        repo.list_documents()  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
        repo.list_documents(collection_id=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
        repo.list_documents(collection_id="")
    with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
        repo.list_documents(collection_id="   ")

    # 3. Explicit unscoped global query
    captured_stmts.clear()
    repo.list_all_documents_global()
    assert len(captured_stmts) == 1
    unscoped_sql = str(captured_stmts[0].compile(dialect=postgresql.dialect()))
    assert "WHERE" not in unscoped_sql
    assert "ORDER BY regulatory_documents.id" in unscoped_sql


def test_02_postgres_chunk_repository_sql_collection_filter_and_joins():
    """Verify that PostgresChunkRepository joins through DocumentVersionModel to RegulatoryDocumentModel with collection filter."""
    captured_stmts = []

    mock_session = MagicMock()
    def fake_execute(stmt, *args, **kwargs):
        captured_stmts.append(stmt)
        res = MagicMock()
        res.all.return_value = []
        return res
    mock_session.execute = fake_execute

    session_factory = MagicMock(return_value=mock_session)
    mock_session.__enter__.return_value = mock_session

    repo = PostgresChunkRepository(session_factory)

    # 1. Scoped query
    repo.list_all_chunks(collection_id="test_col_456")
    assert len(captured_stmts) == 1
    compiled_sql = str(captured_stmts[0].compile(dialect=postgresql.dialect()))
    assert "JOIN document_versions" in compiled_sql
    assert "JOIN regulatory_documents" in compiled_sql
    assert "WHERE regulatory_documents.collection_id =" in compiled_sql
    assert "ORDER BY document_versions.document_id, chunks.chunk_index" in compiled_sql

    # 2. Missing or invalid collection_id raises
    with pytest.raises(TypeError):
        repo.list_all_chunks()  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
        repo.list_all_chunks(collection_id=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
        repo.list_all_chunks(collection_id="")
    with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
        repo.list_all_chunks(collection_id="   ")

    # 3. Explicit unscoped global query
    captured_stmts.clear()
    repo.list_all_chunks_global()
    assert len(captured_stmts) == 1
    unscoped_sql = str(captured_stmts[0].compile(dialect=postgresql.dialect()))
    assert "WHERE" not in unscoped_sql
    assert "ORDER BY document_versions.document_id, chunks.chunk_index" in unscoped_sql


# -------------------------------------------------------------------------
# 2. File Repository Behavior (Multi-Collection Scoping & Unscoped Fallback)
# -------------------------------------------------------------------------

def test_03_file_repository_collection_scoping(tmp_path: Path):
    """Verify FileDocumentRepository and FileChunkRepository correctly filter by collection_id while preserving unscoped."""
    doc_repo = FileDocumentRepository(processed_dir=tmp_path)
    chunk_repo = FileChunkRepository(processed_dir=tmp_path)

    # Populate documents in two distinct collections
    doc_a = DocumentRecord(
        document_id="doc_alpha",
        title="Doc Alpha",
        source_url="https://example.com/alpha.pdf",
        sha256="alpha_sha",
        pdf_path="alpha.pdf",
        collection_id="collection_alpha",
    )
    doc_b = DocumentRecord(
        document_id="doc_beta",
        title="Doc Beta",
        source_url="https://example.com/beta.pdf",
        sha256="beta_sha",
        pdf_path="beta.pdf",
        collection_id="collection_beta",
    )
    doc_repo.save_document(doc_a)
    doc_repo.save_document(doc_b)

    # Populate chunks
    chunk_a = ChunkRecord(
        chunk_id="doc_alpha_p1_0",
        document_id="doc_alpha",
        collection_id="collection_alpha",
        text="Alpha text",
        page_start=1,
        page_end=1,
    )
    chunk_b = ChunkRecord(
        chunk_id="doc_beta_p1_0",
        document_id="doc_beta",
        collection_id="collection_beta",
        text="Beta text",
        page_start=1,
        page_end=1,
    )
    chunk_repo.save_chunks([chunk_a])
    chunk_repo.save_chunks([chunk_b])

    # 1. Missing or invalid collection_id raises
    with pytest.raises(TypeError):
        doc_repo.list_documents()  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
        doc_repo.list_documents(collection_id=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
        doc_repo.list_documents(collection_id="")
    with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
        doc_repo.list_documents(collection_id="   ")

    with pytest.raises(TypeError):
        chunk_repo.list_all_chunks()  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
        chunk_repo.list_all_chunks(collection_id=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
        chunk_repo.list_all_chunks(collection_id="")
    with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
        chunk_repo.list_all_chunks(collection_id="   ")

    # 2. Explicit global listing returns all records across collections
    all_docs = doc_repo.list_all_documents_global()
    assert len(all_docs) == 2

    # 3. Scoped list_documents returns only requested collection
    alpha_docs = doc_repo.list_documents(collection_id="collection_alpha")
    assert len(alpha_docs) == 1
    assert alpha_docs[0].document_id == "doc_alpha"

    beta_docs = doc_repo.list_documents(collection_id="collection_beta")
    assert len(beta_docs) == 1
    assert beta_docs[0].document_id == "doc_beta"

    gamma_docs = doc_repo.list_documents(collection_id="collection_gamma")
    assert len(gamma_docs) == 0

    # 4. Explicit global listing returns all chunks across collections
    all_chunks = chunk_repo.list_all_chunks_global()
    assert len(all_chunks) == 2

    # 5. Scoped list_all_chunks returns only requested collection
    alpha_chunks = chunk_repo.list_all_chunks(collection_id="collection_alpha")
    assert len(alpha_chunks) == 1
    assert alpha_chunks[0].chunk_id == "doc_alpha_p1_0"

    beta_chunks = chunk_repo.list_all_chunks(collection_id="collection_beta")
    assert len(beta_chunks) == 1
    assert beta_chunks[0].chunk_id == "doc_beta_p1_0"

    gamma_chunks = chunk_repo.list_all_chunks(collection_id="collection_gamma")
    assert len(gamma_chunks) == 0


# -------------------------------------------------------------------------
# 3. Publisher Propagation Verification
# -------------------------------------------------------------------------

def test_04_publisher_propagates_collection_id_to_repositories(tmp_path: Path):
    """Verify IndexPublisher calls doc_repo.list_documents and chunk_repo.list_all_chunks with exact collection_id."""
    mock_doc_repo = MagicMock()
    mock_chunk_repo = MagicMock()

    mock_doc_repo.list_documents.return_value = [
        DocumentRecord(
            document_id="doc_alpha",
            title="Doc Alpha",
            source_url="https://example.com/alpha.pdf",
            sha256="alpha_sha",
            pdf_path="alpha.pdf",
            collection_id="my_custom_collection",
        )
    ]
    mock_chunk_repo.list_all_chunks.return_value = [
        ChunkRecord(
            chunk_id="doc_alpha_p1_0",
            document_id="doc_alpha",
            collection_id="my_custom_collection",
            text="Alpha content for indexing.",
            page_start=1,
            page_end=1,
        )
    ]

    mock_factory = MagicMock(spec=StorageRepositoryFactory)
    mock_factory.get_document_repository.return_value = mock_doc_repo
    mock_factory.get_chunk_repository.return_value = mock_chunk_repo

    mock_embedder = MagicMock()
    mock_embedder.model_name = "BAAI/bge-small-en-v1.5"
    mock_embedder.embed_texts.return_value = np.array([[0.1] * 384], dtype=np.float32)

    storage = LocalStorageAdapter(tmp_path / "remote")
    publisher = IndexPublisher(
        repository_factory=mock_factory,
        indexes_dir=tmp_path / "indexes",
        processed_dir=tmp_path / "processed",
        embedder=mock_embedder,
        collection_id="my_custom_collection",
        storage_adapter=storage,
    )

    # 1. get_index_status propagation
    publisher.get_index_status()
    mock_doc_repo.list_documents.assert_called_with(collection_id="my_custom_collection")

    # 2. build_and_publish_snapshot propagation
    res = publisher.build_and_publish_snapshot()
    assert res.success is True
    mock_doc_repo.list_documents.assert_called_with(collection_id="my_custom_collection")
    mock_chunk_repo.list_all_chunks.assert_called_with(collection_id="my_custom_collection")


def test_05_publisher_snapshot_excludes_unrelated_collection_data(tmp_path: Path):
    """End-to-end publisher test verifying snapshot bundle contains ONLY targeted collection data."""
    processed_dir = tmp_path / "processed"
    indexes_dir = tmp_path / "indexes"
    storage_root = tmp_path / "storage"

    doc_repo = FileDocumentRepository(processed_dir=processed_dir)
    chunk_repo = FileChunkRepository(processed_dir=processed_dir)

    # Add Target Collection (col_target) document & chunk
    doc_repo.save_document(
        DocumentRecord(
            document_id="doc_target",
            title="Target Document",
            source_url="https://example.com/target.pdf",
            sha256="target_sha",
            pdf_path="target.pdf",
            collection_id="col_target",
        )
    )
    chunk_repo.save_chunks([
        ChunkRecord(
            chunk_id="doc_target_p1_0",
            document_id="doc_target",
            collection_id="col_target",
            text="Target collection specific cybersecurity guidance.",
            page_start=1,
            page_end=1,
        )
    ])

    # Add Other Collection (col_other) document & chunk
    doc_repo.save_document(
        DocumentRecord(
            document_id="doc_other",
            title="Other Document",
            source_url="https://example.com/other.pdf",
            sha256="other_sha",
            pdf_path="other.pdf",
            collection_id="col_other",
        )
    )
    chunk_repo.save_chunks([
        ChunkRecord(
            chunk_id="doc_other_p1_0",
            document_id="doc_other",
            collection_id="col_other",
            text="Other collection unrelated data.",
            page_start=1,
            page_end=1,
        )
    ])

    factory = FileStorageRepositoryFactory(processed_dir=processed_dir)
    storage = LocalStorageAdapter(storage_root)

    mock_embedder = MagicMock()
    mock_embedder.model_name = "BAAI/bge-small-en-v1.5"
    mock_embedder.embed_texts.return_value = np.array([[0.05] * 384], dtype=np.float32)

    publisher = IndexPublisher(
        repository_factory=factory,
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
        embedder=mock_embedder,
        collection_id="col_target",
        storage_adapter=storage,
    )

    result = publisher.build_and_publish_snapshot()
    assert result.success is True
    assert result.total_documents == 1
    assert result.total_chunks == 1

    # Inspect the published manifest
    manifest = publisher.get_active_manifest()
    assert manifest is not None
    assert manifest.collection_id == "col_target"
    assert manifest.document_ids == ["doc_target"]
    assert "doc_other" not in manifest.document_ids
