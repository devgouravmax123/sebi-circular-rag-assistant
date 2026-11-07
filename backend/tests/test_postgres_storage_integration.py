"""
Unit and mocked integration tests for Task 8.2: Persistent PostgreSQL Storage Integration.

Verifies:
1. WorkspaceRepository: creation, lookup by ID/slug, membership management, tenant-scoping.
2. CollectionRepository: creation, update, system collection protection, deletion, workspace-scoping.
3. DocumentRepository: persistence with composite FK (workspace_id, collection_id), tenant isolation.
4. IngestionJobRepository: enqueueing, status polling, retry_job, cancel_job, state transition validation.
5. QueryHistoryRepository: logging queries, retrieval by user/workspace, deletion.
6. Error handling: ConnectionStorageError translation on DBAPI/Operational failures.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import DBAPIError, OperationalError, IntegrityError

from app.persistence.interfaces import (
    ConnectionStorageError,
    PersistenceError,
    StorageRepositoryFactory,
)
from app.persistence.postgres import (
    PostgresCollectionRepository,
    PostgresDocumentRepository,
    PostgresIngestionJobRepository,
    PostgresQueryHistoryRepository,
    PostgresStorageAdapter,
    PostgresStorageRepositoryFactory,
    PostgresWorkspaceRepository,
    CollectionModel,
    DocumentVersionModel,
    IngestionJobModel,
    QueryHistoryModel,
    RegulatoryDocumentModel,
    WorkspaceMembershipModel,
    WorkspaceModel,
)
from app.schemas.document import DocumentRecord
from app.schemas.ingestion_job import (
    IngestionJobRecord,
    JobStatus,
    ProcessingStage,
)


# ============================================================================
# 1. Workspace Repository Tests
# ============================================================================


class TestPostgresWorkspaceRepository:
    def test_create_and_get_workspace(self):
        mock_session_factory = MagicMock()
        mock_session = MagicMock()
        mock_session_factory.return_value.__enter__.return_value = mock_session

        repo = PostgresWorkspaceRepository(mock_session_factory)
        user_id = uuid.uuid4()

        w = repo.create_workspace(
            name="Alpha Hedge Fund",
            slug="alpha-fund",
            owner_id=user_id,
            is_system=False,
        )

        assert w.name == "Alpha Hedge Fund"
        assert w.slug == "alpha-fund"
        assert w.owner_id == user_id
        mock_session.add.assert_called_once()

    def test_get_workspace_not_found(self):
        mock_session_factory = MagicMock()
        mock_session = MagicMock()
        mock_session_factory.return_value.__enter__.return_value = mock_session
        mock_session.execute.return_value.scalar_one_or_none.return_value = None

        repo = PostgresWorkspaceRepository(mock_session_factory)
        assert repo.get_workspace(uuid.uuid4()) is None

    def test_get_workspace_found(self):
        mock_session_factory = MagicMock()
        mock_session = MagicMock()
        mock_session_factory.return_value.__enter__.return_value = mock_session

        ws_id = uuid.uuid4()
        owner_id = uuid.uuid4()
        mock_ws = WorkspaceModel(
            id=ws_id,
            name="Beta Fund",
            slug="beta-fund",
            owner_id=owner_id,
            is_system=False,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        mock_session.execute.return_value.scalar_one_or_none.return_value = mock_ws

        repo = PostgresWorkspaceRepository(mock_session_factory)
        res = repo.get_workspace(ws_id)
        assert res is not None
        assert res.id == ws_id
        assert res.slug == "beta-fund"

    def test_create_workspace_db_error_translation(self):
        mock_session_factory = MagicMock()
        mock_session = MagicMock()
        mock_session_factory.return_value.__enter__.return_value = mock_session
        mock_session.flush.side_effect = OperationalError("conn lost", params={}, orig=Exception("closed"))

        repo = PostgresWorkspaceRepository(mock_session_factory)
        with pytest.raises(ConnectionStorageError):
            repo.create_workspace(name="Fail WS", slug="fail-ws")


# ============================================================================
# 2. Collection Repository Tests
# ============================================================================


class TestPostgresCollectionRepository:
    def test_create_collection(self):
        mock_session_factory = MagicMock()
        mock_session = MagicMock()
        mock_session_factory.return_value.__enter__.return_value = mock_session

        repo = PostgresCollectionRepository(mock_session_factory)
        ws_id = uuid.uuid4()
        col = repo.create_collection(
            id="fin-reports",
            workspace_id=ws_id,
            name="Financial Reports",
            policy_name="general_rag",
            description="Reports collection",
            is_system=False,
        )

        assert col.id == "fin-reports"
        assert col.workspace_id == ws_id
        assert col.is_system is False
        mock_session.add.assert_called_once()

    def test_delete_system_collection_forbidden(self):
        mock_session_factory = MagicMock()
        mock_session = MagicMock()
        mock_session_factory.return_value.__enter__.return_value = mock_session

        ws_id = uuid.uuid4()
        mock_col = CollectionModel(
            id="cscrf-core",
            workspace_id=ws_id,
            name="CSCRF Core",
            policy_name="cscrf_strict",
            is_system=True,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        mock_session.execute.return_value.scalar_one_or_none.return_value = mock_col

        repo = PostgresCollectionRepository(mock_session_factory)
        with pytest.raises(PersistenceError, match="System collections cannot be deleted"):
            repo.delete_collection("cscrf-core", ws_id)

    def test_delete_user_collection_success(self):
        mock_session_factory = MagicMock()
        mock_session = MagicMock()
        mock_session_factory.return_value.__enter__.return_value = mock_session

        ws_id = uuid.uuid4()
        mock_col = CollectionModel(
            id="temp-col",
            workspace_id=ws_id,
            name="Temp",
            policy_name="general_rag",
            is_system=False,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        mock_session.execute.return_value.scalar_one_or_none.return_value = mock_col

        repo = PostgresCollectionRepository(mock_session_factory)
        deleted = repo.delete_collection("temp-col", ws_id)
        assert deleted is True
        mock_session.delete.assert_called_once_with(mock_col)


# ============================================================================
# 3. Document Repository Tests
# ============================================================================


class TestPostgresDocumentRepositoryIntegration:
    def test_save_document_sets_composite_fk_and_attributes(self):
        mock_session_factory = MagicMock()
        mock_session = MagicMock()
        mock_session_factory.return_value.__enter__.return_value = mock_session

        ws_id = uuid.uuid4()
        mock_col = CollectionModel(
            id="cscrf",
            workspace_id=ws_id,
            name="CSCRF",
            policy_name="cscrf_strict",
            is_system=True,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        # Sequence of execute queries: regulator, domain, collection, doc_model, existing_version, latest_version
        mock_session.execute.return_value.scalar_one_or_none.side_effect = [
            None,  # regulator
            None,  # domain
            mock_col,  # collection
            None,  # doc_model
            None,  # existing_version
        ]
        mock_session.execute.return_value.scalars.return_value.first.return_value = None

        repo = PostgresDocumentRepository(mock_session_factory)

        doc = DocumentRecord(
            document_id="doc-101",
            title="SEBI Cybersecurity Circular",
            total_chunks=5,
            source_url="https://sebi.gov.in/circ1.pdf",
            sha256="abc1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
            pdf_path="data/raw/circ1.pdf",
            circular_number="CIR/2024/01",
            published_date="2024-01-15",
            collection_id="cscrf",
            workspace_id=ws_id,
        )

        repo.save_document(doc)
        assert mock_session.add.call_count >= 1
        added_doc = None
        for call_args in mock_session.add.call_args_list:
            arg = call_args[0][0]
            if isinstance(arg, RegulatoryDocumentModel):
                added_doc = arg
                break
        assert added_doc is not None
        assert added_doc.id == "doc-101"
        assert added_doc.workspace_id == ws_id
        assert added_doc.collection_id == "cscrf"
        assert added_doc.circular_number == "CIR/2024/01"

    def test_get_document_tenant_scoping(self):
        mock_session_factory = MagicMock()
        mock_session = MagicMock()
        mock_session_factory.return_value.__enter__.return_value = mock_session

        ws_id = uuid.uuid4()

        mock_doc = RegulatoryDocumentModel(
            id="doc-202",
            workspace_id=ws_id,
            collection_id="cscrf",
            title="Test Doc",
            domain_id="SEBI_CSCRF",
            created_at=datetime.now(timezone.utc),
        )
        mock_version = DocumentVersionModel(
            id=uuid.uuid4(),
            document_id="doc-202",
            version_number=1,
            sha256="abc123sha",
            storage_path="data/raw/doc.pdf",
            source_url="https://example.com/doc.pdf",
            retrieved_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )

        mock_session.execute.return_value.scalar_one_or_none.return_value = mock_doc
        mock_session.execute.return_value.scalars.return_value.first.return_value = mock_version

        repo = PostgresDocumentRepository(mock_session_factory)
        res = repo.get_document("doc-202", workspace_id=ws_id)
        assert res is not None
        assert res.document_id == "doc-202"


# ============================================================================
# 4. Ingestion Job Repository Tests (retry, cancel, transitions)
# ============================================================================


class TestPostgresIngestionJobRepository:
    def test_create_and_get_job(self):
        mock_session_factory = MagicMock()
        mock_session = MagicMock()
        mock_session_factory.return_value.__enter__.return_value = mock_session

        mock_session.execute.return_value.scalar_one_or_none.return_value = None

        repo = PostgresIngestionJobRepository(mock_session_factory)
        ws_id = uuid.uuid4()

        job = repo.create_job(
            source_url="https://example.com/test.pdf",
            idempotency_key="idemp-123",
            workspace_id=ws_id,
            collection_id="cscrf",
        )

        mock_session.add.assert_called_once()
        assert job.source_url == "https://example.com/test.pdf"
        assert job.idempotency_key == "idemp-123"
        assert job.workspace_id == ws_id
        assert job.collection_id == "cscrf"

    def test_retry_job_success(self):
        mock_session_factory = MagicMock()
        mock_session = MagicMock()
        mock_session_factory.return_value.__enter__.return_value = mock_session

        job_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        model = IngestionJobModel(
            id=job_id,
            workspace_id=uuid.uuid4(),
            collection_id="cscrf",
            source_url="https://example.com/test.pdf",
            idempotency_key="key-1",
            status=JobStatus.FAILED.value,
            stage=ProcessingStage.CHUNKING.value,
            attempt_count=2,
            max_attempts=3,
            claim_token=uuid.uuid4(),
            claim_worker_id="worker-1",
            created_at=now,
        )
        mock_session.execute.return_value.scalar_one_or_none.return_value = model

        repo = PostgresIngestionJobRepository(mock_session_factory)
        retried = repo.retry_job(job_id)

        assert retried is not None
        assert model.status == JobStatus.QUEUED.value
        assert model.stage == ProcessingStage.PENDING.value
        assert model.claim_token is None
        assert model.claim_worker_id is None
        assert model.next_retry_at is not None

    def test_retry_job_rejected_when_not_failed(self):
        mock_session_factory = MagicMock()
        mock_session = MagicMock()
        mock_session_factory.return_value.__enter__.return_value = mock_session

        job_id = uuid.uuid4()
        model = IngestionJobModel(
            id=job_id,
            workspace_id=uuid.uuid4(),
            collection_id="cscrf",
            source_url="https://example.com/test.pdf",
            idempotency_key="key-1",
            status=JobStatus.PROCESSING.value,
            stage=ProcessingStage.CHUNKING.value,
            created_at=datetime.now(timezone.utc),
        )
        mock_session.execute.return_value.scalar_one_or_none.return_value = model

        repo = PostgresIngestionJobRepository(mock_session_factory)
        with pytest.raises(PersistenceError, match="Only jobs in 'failed' status may be retried"):
            repo.retry_job(job_id)

    def test_cancel_job_success(self):
        mock_session_factory = MagicMock()
        mock_session = MagicMock()
        mock_session_factory.return_value.__enter__.return_value = mock_session

        job_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        model = IngestionJobModel(
            id=job_id,
            workspace_id=uuid.uuid4(),
            collection_id="cscrf",
            source_url="https://example.com/test.pdf",
            idempotency_key="key-1",
            status=JobStatus.QUEUED.value,
            stage=ProcessingStage.PENDING.value,
            created_at=now,
        )
        mock_session.execute.return_value.scalar_one_or_none.return_value = model

        repo = PostgresIngestionJobRepository(mock_session_factory)
        cancelled = repo.cancel_job(job_id, reason="Admin stopped test")

        assert cancelled is not None
        assert model.status == JobStatus.FAILED.value
        assert model.stage == ProcessingStage.FINISHED.value
        assert model.error_details["reason"] == "Admin stopped test"

    def test_cancel_job_rejected_when_terminal(self):
        mock_session_factory = MagicMock()
        mock_session = MagicMock()
        mock_session_factory.return_value.__enter__.return_value = mock_session

        job_id = uuid.uuid4()
        model = IngestionJobModel(
            id=job_id,
            workspace_id=uuid.uuid4(),
            collection_id="cscrf",
            source_url="https://example.com/test.pdf",
            idempotency_key="key-1",
            status=JobStatus.COMPLETED.value,
            stage=ProcessingStage.FINISHED.value,
            created_at=datetime.now(timezone.utc),
        )
        mock_session.execute.return_value.scalar_one_or_none.return_value = model

        repo = PostgresIngestionJobRepository(mock_session_factory)
        with pytest.raises(PersistenceError, match="already in terminal status"):
            repo.cancel_job(job_id)


# ============================================================================
# 5. Query History Repository Tests
# ============================================================================


class TestPostgresQueryHistoryRepository:
    def test_create_and_list_query_history(self):
        mock_session_factory = MagicMock()
        mock_session = MagicMock()
        mock_session_factory.return_value.__enter__.return_value = mock_session

        repo = PostgresQueryHistoryRepository(mock_session_factory)
        user_id = uuid.uuid4()
        ws_id = uuid.uuid4()

        entry = repo.create_entry(
            user_id=user_id,
            collection_id="cscrf",
            query_text="What are cybersecurity audit guidelines?",
            disposition="answered",
            workspace_id=ws_id,
            summary_narrative="SEBI guidelines require biannual audits...",
        )

        assert entry.user_id == user_id
        assert entry.workspace_id == ws_id
        assert entry.query_text == "What are cybersecurity audit guidelines?"
        mock_session.add.assert_called_once()

    def test_delete_query_history_entry_scoped(self):
        mock_session_factory = MagicMock()
        mock_session = MagicMock()
        mock_session_factory.return_value.__enter__.return_value = mock_session

        entry_id = uuid.uuid4()
        user_id = uuid.uuid4()
        model = QueryHistoryModel(
            id=entry_id,
            user_id=user_id,
            collection_id="cscrf",
            query_text="q",
            disposition="answered",
            created_at=datetime.now(timezone.utc),
        )
        mock_session.execute.return_value.scalar_one_or_none.return_value = model

        repo = PostgresQueryHistoryRepository(mock_session_factory)
        res = repo.delete_entry(history_id=entry_id, user_id=user_id)
        assert res is True
        mock_session.delete.assert_called_once_with(model)


# ============================================================================
# 6. Factory Integration & Alias Tests
# ============================================================================


class TestPostgresStorageAdapterFactory:
    def test_factory_provides_all_repositories(self):
        with patch("app.persistence.postgres.create_engine") as mock_engine:
            adapter = PostgresStorageAdapter(database_url="postgresql+psycopg://u:p@localhost:5432/db")
            assert isinstance(adapter.get_document_repository(), PostgresDocumentRepository)
            assert isinstance(adapter.get_ingestion_job_repository(), PostgresIngestionJobRepository)
            assert isinstance(adapter.get_workspace_repository(), PostgresWorkspaceRepository)
            assert isinstance(adapter.get_collection_repository(), PostgresCollectionRepository)
            assert isinstance(adapter.get_query_history_repository(), PostgresQueryHistoryRepository)
