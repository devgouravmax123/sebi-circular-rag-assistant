"""
Unit and Contract Tests for Task 12.3E.2:
Collection-Scoped Repository Vector Search.

Validates that:
1. Missing collection_id is rejected by interface and implementations.
2. None, empty, and whitespace-only collection IDs are rejected with ValueError.
3. PostgreSQL query contains required joins to regulatory_documents and collection_id predicate.
4. Model-name filtering, pgvector cosine distance operator (<=>), ordering, and top_k limit are preserved.
5. File repository enforces the same collection_id contract.
6. Vector search for Collection A binds collection_id='A' and excludes Collection B embeddings.
"""
import inspect
import pytest
import numpy as np
from unittest.mock import MagicMock
from sqlalchemy import text

from app.persistence.interfaces import ChunkRepository
from app.persistence.postgres import PostgresChunkRepository
from app.persistence.file_repo import FileChunkRepository


class TestCollectionScopedVectorSearchContract:
    """Verifies that ChunkRepository.search_similar_chunks mandates collection_id."""

    def test_01_interface_signature_requires_collection_id(self):
        sig = inspect.signature(ChunkRepository.search_similar_chunks)
        params = list(sig.parameters.keys())
        assert "collection_id" in params, "collection_id must be in ChunkRepository.search_similar_chunks signature"
        assert params[1] == "collection_id", f"Expected collection_id as first non-self param, got: {params}"
        assert sig.parameters["collection_id"].default is inspect.Parameter.empty, "collection_id must not have a default value"

    def test_02_postgres_repo_rejects_missing_or_invalid_collection_id(self):
        mock_factory = MagicMock()
        repo = PostgresChunkRepository(mock_factory)
        vec = np.zeros(384, dtype=np.float32)

        # Missing collection_id (TypeError)
        with pytest.raises(TypeError):
            repo.search_similar_chunks(query_vector=vec)  # type: ignore[call-arg]

        # None collection_id
        with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
            repo.search_similar_chunks(None, vec)  # type: ignore[arg-type]

        # Empty string
        with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
            repo.search_similar_chunks("", vec)

        # Whitespace-only string
        with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
            repo.search_similar_chunks("   ", vec)

    def test_03_file_repo_rejects_missing_or_invalid_collection_id(self):
        repo = FileChunkRepository()
        vec = np.zeros(384, dtype=np.float32)

        # Missing collection_id (TypeError)
        with pytest.raises(TypeError):
            repo.search_similar_chunks(query_vector=vec)  # type: ignore[call-arg]

        # None collection_id
        with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
            repo.search_similar_chunks(None, vec)  # type: ignore[arg-type]

        # Empty string
        with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
            repo.search_similar_chunks("", vec)

        # Whitespace-only string
        with pytest.raises(ValueError, match="collection_id must be a non-empty string"):
            repo.search_similar_chunks("   ", vec)

        # Valid collection_id returns [] (stub behavior)
        assert repo.search_similar_chunks("sebi_cscrf", vec) == []

    def test_04_postgres_vector_search_sql_contains_joins_and_collection_predicate(self):
        mock_factory = MagicMock()
        mock_session = MagicMock()
        mock_factory.return_value.__enter__.return_value = mock_session
        mock_session.execute.return_value.all.return_value = [("c1", 0.98), ("c2", 0.85)]

        repo = PostgresChunkRepository(mock_factory)
        vec = np.zeros(384, dtype=np.float32)

        results = repo.search_similar_chunks("alpha_collection", vec, top_k=5, model_name="custom/model")

        assert len(results) == 2
        assert results[0] == ("c1", 0.98)
        assert results[1] == ("c2", 0.85)

        # Verify SQL statement and parameter bindings
        assert mock_session.execute.called
        call_args = mock_session.execute.call_args
        stmt, params = call_args[0][0], call_args[0][1]

        sql_str = str(stmt).strip()

        # Check required JOINs across chunk_embeddings -> chunks -> document_versions -> regulatory_documents
        assert "JOIN chunks c ON c.id = ce.chunk_id" in sql_str
        assert "JOIN document_versions dv ON dv.id = c.version_id" in sql_str
        assert "JOIN regulatory_documents rd ON rd.id = dv.document_id" in sql_str

        # Check WHERE predicates
        assert "WHERE rd.collection_id = :collection_id" in sql_str
        assert "ce.model_name = :model_name" in sql_str

        # Check pgvector cosine distance operator and ordering
        assert "ce.embedding <=> (:query_vec)::vector" in sql_str
        assert "ORDER BY ce.embedding <=> (:query_vec)::vector ASC" in sql_str
        assert "LIMIT :top_k" in sql_str

        # Check parameter binding values
        assert params["collection_id"] == "alpha_collection"
        assert params["model_name"] == "custom/model"
        assert params["top_k"] == 5
        assert params["query_vec"].startswith("[") and params["query_vec"].endswith("]")

    def test_05_cross_collection_queries_bind_distinct_scopes(self):
        """Verifies that two queries with identical vectors target distinct collection scopes."""
        mock_factory = MagicMock()
        mock_session = MagicMock()
        mock_factory.return_value.__enter__.return_value = mock_session
        mock_session.execute.return_value.all.return_value = []

        repo = PostgresChunkRepository(mock_factory)
        vec = np.ones(384, dtype=np.float32)

        repo.search_similar_chunks("collection_alpha", vec, top_k=10)
        first_call_params = mock_session.execute.call_args_list[0][0][1]

        repo.search_similar_chunks("collection_beta", vec, top_k=10)
        second_call_params = mock_session.execute.call_args_list[1][0][1]

        assert first_call_params["collection_id"] == "collection_alpha"
        assert second_call_params["collection_id"] == "collection_beta"
        assert first_call_params["collection_id"] != second_call_params["collection_id"]

