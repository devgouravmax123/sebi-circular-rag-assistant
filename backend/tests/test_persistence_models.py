"""
Focused tests for Phase 8A.2 persistence models, schemas, and configurations.
Executes completely offline without requiring a running PostgreSQL server.
"""
import pytest
from datetime import date, datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.persistence.models import (
    Base,
    RegulatorModel,
    RegulatoryDomainModel,
    RegulatoryDocumentModel,
    DocumentVersionModel,
    ProvisionModel,
    ChunkModel,
    ChunkEmbeddingModel,
    RegulatoryEdgeModel,
    IngestionJobModel,
)
from app.persistence.file_repo import (
    FileDocumentRepository,
    FileChunkRepository,
    FileGraphEdgeRepository,
    FileStorageRepositoryFactory,
)
from app.persistence import get_storage_factory


class TestSettingsConfiguration:
    """Verifies environment-based configuration and safe secret handling."""

    def test_default_file_mode(self):
        settings = Settings(database_url=None)
        assert not settings.is_database_enabled
        assert settings.sanitized_database_url is None

    def test_database_enabled_with_valid_url(self):
        settings = Settings(database_url="postgresql+asyncpg://app_user:secret_pass@localhost:5432/sebi_rag")
        assert settings.is_database_enabled
        sanitized = settings.sanitized_database_url
        assert sanitized is not None
        assert "secret_pass" not in sanitized
        assert "app_user" in sanitized
        assert "***" in sanitized

    def test_invalid_scheme_raises_error(self):
        with pytest.raises(ValueError, match="Unsupported database dialect scheme"):
            Settings(database_url="mysql://user:pass@localhost/db")

    def test_blank_string_database_url_is_disabled(self):
        settings = Settings(database_url="   ")
        assert not settings.is_database_enabled
        assert settings.database_url is None

    def test_neon_postgresql_url_normalized_to_psycopg(self):
        raw_url = "postgresql://alex:my_secret_pass@ep-cool-fog-123.us-east-2.aws.neon.tech/neondb?sslmode=require"
        settings = Settings(database_url=raw_url)
        assert settings.is_database_enabled
        assert settings.database_url.startswith("postgresql+psycopg://")
        assert "sslmode=require" in settings.database_url
        assert "my_secret_pass" not in settings.sanitized_database_url
        assert "***" in settings.sanitized_database_url

    def test_neon_postgres_scheme_normalized_to_psycopg(self):
        raw_url = "postgres://alex:my_secret_pass@ep-cool-fog-123-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require"
        settings = Settings(database_url=raw_url)
        assert settings.is_database_enabled
        assert settings.database_url.startswith("postgresql+psycopg://")
        assert "sslmode=require" in settings.database_url

    def test_direct_database_url_support_and_sanitization(self):
        direct_url = "postgresql://alex:my_migration_secret@ep-cool-fog-123.us-east-2.aws.neon.tech/neondb?sslmode=require"
        settings = Settings(database_url=None, direct_database_url=direct_url)
        assert not settings.is_database_enabled
        assert settings.is_direct_database_enabled
        assert settings.direct_database_url.startswith("postgresql+psycopg://")
        sanitized = settings.sanitized_direct_database_url
        assert sanitized is not None
        assert "my_migration_secret" not in sanitized
        assert "***" in sanitized

    def test_alembic_env_prioritizes_direct_database_url(self, monkeypatch):
        from backend.alembic.env import get_url
        direct = "postgresql://alex:direct_pass@ep-direct.neon.tech/neondb?sslmode=require"
        pooled = "postgresql://alex:pooled_pass@ep-pooled.neon.tech/neondb?sslmode=require"
        monkeypatch.setenv("DIRECT_DATABASE_URL", direct)
        monkeypatch.setenv("DATABASE_URL", pooled)
        from app.core.config import get_settings
        get_settings(force_reload=True)
        url = get_url()
        assert "ep-direct.neon.tech" in url
        assert url.startswith("postgresql+psycopg://")

        # Fallback when direct URL is unset
        monkeypatch.delenv("DIRECT_DATABASE_URL", raising=False)
        monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)
        get_settings(force_reload=True)
        fallback_url = get_url()
        assert "ep-pooled.neon.tech" in fallback_url
        assert fallback_url.startswith("postgresql+psycopg://")


class TestSQLAlchemyModelMetadata:
    """Verifies table metadata, constraints, relationships, and pgvector type mappings."""

    def test_table_names_registered(self):
        tables = Base.metadata.tables.keys()
        expected = {
            "regulators",
            "regulatory_domains",
            "regulatory_documents",
            "document_versions",
            "provisions",
            "chunks",
            "chunk_embeddings",
            "regulatory_edges",
            "ingestion_jobs",
        }
        for name in expected:
            assert name in tables, f"Missing expected table {name}"

    def test_regulatory_edge_constraints(self):
        edge_table = Base.metadata.tables["regulatory_edges"]
        constraint_names = {c.name for c in edge_table.constraints}
        assert "chk_edge_no_self_loop" in constraint_names
        assert "chk_edge_relationship_type" in constraint_names
        assert "chk_edge_scope_type" in constraint_names
        assert "chk_edge_review_status" in constraint_names

        # Check default review_status column default is 'proposed' (Phase 8C canonical lifecycle)
        review_col = edge_table.columns["review_status"]
        assert review_col.default is not None
        assert getattr(review_col.default, "arg", None) == "proposed"

    def test_chunk_embedding_vector_dim_constraint(self):
        embed_table = Base.metadata.tables["chunk_embeddings"]
        constraint_names = {c.name for c in embed_table.constraints}
        assert "chk_embedding_dim_384" in constraint_names
        dim_col = embed_table.columns["embedding_dim"]
        assert dim_col.default is not None
        assert getattr(dim_col.default, "arg", None) == 384

    def test_document_version_uniqueness_constraints(self):
        ver_table = Base.metadata.tables["document_versions"]
        constraint_names = {c.name for c in ver_table.constraints}
        assert "uq_doc_version_number" in constraint_names
        assert "uq_doc_version_sha256" in constraint_names


class TestFileRepositoryIntegration:
    """Verifies that FileStorageRepositoryFactory cleanly loads the 5-document pilot data."""

    def test_factory_defaults_to_file_repository(self, monkeypatch):
        # Ensure DATABASE_URL is unset to test file-mode default
        monkeypatch.delenv("DATABASE_URL", raising=False)
        factory = get_storage_factory(force_refresh=True)
        assert isinstance(factory, FileStorageRepositoryFactory)

    def test_file_document_repository_reads_pilot_corpus(self):
        factory = FileStorageRepositoryFactory(processed_dir="data/processed")
        doc_repo = factory.get_document_repository()
        docs = doc_repo.list_documents(collection_id="sebi_cscrf")
        assert len(doc_repo.list_all_documents_global()) >= 5
        assert len(docs) == 5
        doc_ids = {d.document_id for d in docs}
        assert "sebi_cscrf_2024_113" in doc_ids
        assert "sebi_cscrf_2025_119" in doc_ids

    def test_file_chunk_repository_reads_pilot_chunks(self):
        factory = FileStorageRepositoryFactory(processed_dir="data/processed")
        chunk_repo = factory.get_chunk_repository()
        chunks = chunk_repo.list_all_chunks(collection_id="sebi_cscrf")
        assert len(chunk_repo.list_all_chunks_global()) >= 168
        assert len(chunks) == 168
        c1 = chunk_repo.get_chunk("sebi_cscrf_2024_113_p1_0")
        assert c1 is not None
        assert c1.document_id == "sebi_cscrf_2024_113"
        assert c1.page_start == 1

    def test_file_graph_edge_repository_reads_pilot_edges(self):
        factory = FileStorageRepositoryFactory(processed_dir="data/processed")
        edge_repo = factory.get_graph_edge_repository()
        edges = edge_repo.list_all_edges()
        assert len(edges) == 9
        # Check verified status for pilot edges
        assert all(e.review_status.value == "verified" for e in edges)
        # Check incoming edges to master circular
        incoming = edge_repo.get_incoming_edges("sebi_cscrf_2024_113")
        assert len(incoming) >= 4


class TestAlembicMigrationConfiguration:
    """Verifies that Alembic migration revisions and version table configuration prevent StringDataRightTruncation."""

    def test_initial_revision_id_format(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config("alembic.ini")
        script = ScriptDirectory.from_config(cfg)
        revisions = list(script.walk_revisions())
        assert len(revisions) >= 2
        rev_ids = {r.revision for r in revisions}
        assert "0001_initial_persistence_foundation" in rev_ids
        assert "0002_add_ingestion_job_queue_fields" in rev_ids
        import re
        for rev in revisions:
            assert re.match(r"^\d{4}_[a-z0-9_]+$", rev.revision)
            assert len(rev.revision) >= 20
            assert len(rev.revision) <= 64

    def test_version_table_capacity_helper(self):
        from backend.alembic.env import _ensure_version_table_capacity
        from unittest.mock import MagicMock
        from sqlalchemy import text

        mock_conn = MagicMock()
        _ensure_version_table_capacity(mock_conn)

        # Confirm execute was called with CREATE TABLE and ALTER TABLE statements specifying VARCHAR(64)
        assert mock_conn.execute.call_count == 2
        calls = [str(c.args[0]) for c in mock_conn.execute.call_args_list]
        assert any("VARCHAR(64)" in c and "CREATE TABLE IF NOT EXISTS alembic_version" in c for c in calls)
        assert any("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64)" in c for c in calls)
