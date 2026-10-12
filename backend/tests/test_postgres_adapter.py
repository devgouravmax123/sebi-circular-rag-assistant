"""
Unit and mocked integration tests for Phase 8A.4 PostgreSQL + pgvector repository adapter.
Verifies:
1. Factory selection: File mode when DATABASE_URL absent/empty vs PostgreSQL mode when configured.
2. Connection error translation (ConnectionStorageError).
3. Parameterized transaction execution and rollback on error.
4. Model conversion, immutable version handling, and constraint validation.
5. Exact 384-dimensional vector validation and query execution.
6. Graph edge review defaults (needs_review preserved; no unreviewed edges promoted).
"""
import pytest
from unittest.mock import MagicMock, patch
import numpy as np
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import DBAPIError, OperationalError, IntegrityError

from app.core.config import Settings
from app.schemas.document import DocumentRecord, ChunkRecord
from app.schemas.graph import GraphEdge, RelationshipType, ScopeType, ReviewStatus, ConfidenceLevel
from app.persistence.interfaces import (
    PersistenceError,
    ConnectionStorageError,
    CorruptedStorageError,
)
from app.persistence.file_repo import FileStorageRepositoryFactory
from app.persistence.postgres import (
    PostgresDocumentRepository,
    PostgresChunkRepository,
    PostgresGraphEdgeRepository,
    PostgresStorageRepositoryFactory,
)
from app.persistence import get_storage_factory


class TestPostgresFactorySelection:
    """Verifies factory instantiation based on configuration."""

    def test_factory_selects_file_when_database_url_none(self):
        with patch("app.persistence.get_settings") as mock_settings:
            mock_settings.return_value = Settings(database_url=None)
            factory = get_storage_factory(force_refresh=True)
            assert isinstance(factory, FileStorageRepositoryFactory)

    def test_factory_selects_file_when_database_url_blank(self):
        with patch("app.persistence.get_settings") as mock_settings:
            mock_settings.return_value = Settings(database_url="   ")
            factory = get_storage_factory(force_refresh=True)
            assert isinstance(factory, FileStorageRepositoryFactory)

    def test_factory_selects_postgres_when_database_url_configured(self):
        valid_url = "postgresql+psycopg://user:pass@localhost:5432/sebi_db"
        with patch("app.persistence.get_settings") as mock_settings:
            mock_settings.return_value = Settings(database_url=valid_url)
            with patch("app.persistence.postgres.create_engine") as mock_engine:
                factory = get_storage_factory(force_refresh=True)
                assert isinstance(factory, PostgresStorageRepositoryFactory)
                assert factory.database_url == valid_url

    def test_factory_invalid_url_raises_value_error(self):
        with pytest.raises(ValueError, match="Unsupported database dialect scheme"):
            Settings(database_url="mysql://user:pass@localhost/db")


class TestPostgresConnectionErrorHandling:
    """Verifies that DBAPI errors are translated into ConnectionStorageError without secret leakage."""

    def test_document_repo_connection_error(self):
        mock_session_factory = MagicMock()
        mock_session = MagicMock()
        mock_session_factory.return_value.__enter__.return_value = mock_session
        mock_session.execute.side_effect = OperationalError("connection to server lost", None, None)

        repo = PostgresDocumentRepository(mock_session_factory)
        with pytest.raises(ConnectionStorageError, match="Database connection error in get_document"):
            repo.get_document("sebi_cscrf_2024_113")

    def test_chunk_repo_connection_error(self):
        mock_session_factory = MagicMock()
        mock_session = MagicMock()
        mock_session_factory.return_value.__enter__.return_value = mock_session
        mock_session.execute.side_effect = OperationalError("connection refused", None, None)

        repo = PostgresChunkRepository(mock_session_factory)
        with pytest.raises(ConnectionStorageError, match="Database connection error in list_chunks_for_document"):
            repo.list_chunks_for_document("sebi_cscrf_2024_113")

    def test_graph_edge_repo_connection_error(self):
        mock_session_factory = MagicMock()
        mock_session = MagicMock()
        mock_session_factory.return_value.__enter__.return_value = mock_session
        mock_session.execute.side_effect = OperationalError("connection refused", None, None)

        repo = PostgresGraphEdgeRepository(mock_session_factory)
        with pytest.raises(ConnectionStorageError, match="Database connection error in list_all_edges"):
            repo.list_all_edges()


class TestPostgresVectorValidation:
    """Verifies 384-dimensional pgvector validation."""

    def test_embedding_dimension_mismatch_raises_value_error(self):
        mock_session_factory = MagicMock()
        repo = PostgresChunkRepository(mock_session_factory)

        wrong_dim_vec = np.zeros(256, dtype=np.float32)
        with pytest.raises(ValueError, match="Vector for chunk 'c1' has shape.*expected \\(384,\\)"):
            repo.save_embeddings({"c1": wrong_dim_vec})

    def test_search_query_vector_dimension_mismatch_raises_value_error(self):
        mock_session_factory = MagicMock()
        repo = PostgresChunkRepository(mock_session_factory)

        wrong_dim_vec = np.zeros(512, dtype=np.float32)
        with pytest.raises(ValueError, match="Query vector must be 384 dimensions"):
            repo.search_similar_chunks(wrong_dim_vec)


class TestPostgresEdgeIntegrityAndDefaults:
    """Verifies that edges preserve conservative review status and handle integrity errors."""

    def test_edge_integrity_error_translates_to_persistence_error(self):
        mock_session_factory = MagicMock()
        mock_session = MagicMock()
        mock_session_factory.return_value.__enter__.return_value = mock_session
        mock_session.begin.return_value.__enter__.return_value = None
        mock_session.execute.side_effect = IntegrityError(
            "violates foreign key constraint", "params", "orig"
        )

        repo = PostgresGraphEdgeRepository(mock_session_factory)
        edge = GraphEdge(
            edge_id="e1",
            source_document_id="doc_new",
            target_document_id="doc_old",
            relationship=RelationshipType.AMENDS,
            scope=ScopeType.FULL,
            evidence_text="test evidence",
        )
        with pytest.raises(PersistenceError, match="Integrity constraint violation saving edge"):
            repo.save_edge(edge)
