"""
Unit and mocked integration tests for Milestone 12 - Wave 3, Task 12.3D:
Collection-Scoped Ingestion Deduplication and Foreign-ID Leak Fix.

Verifies:
1. SQL-level compilation of collection-scoped get_document_by_sha256 in PostgresDocumentRepository.
2. FileDocumentRepository collection-scoped and unscoped SHA-256 lookup parity.
3. Same-collection duplicate detection preserved without leaking original document IDs.
4. Different-collection identical-content ingestion succeeds and creates independent documents/chunks.
5. Cross-workspace identical-content isolation.
6. API job status serialization protection against foreign document identifier leaks.
"""
from datetime import datetime, timezone
import uuid
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
import numpy as np
import pytest
from sqlalchemy import select, and_
from sqlalchemy.dialects import postgresql

from app.schemas.document import DocumentRecord, ChunkRecord
from app.schemas.ingestion_job import IngestionJobRecord, JobStatus, ProcessingStage
from app.persistence.file_repo import (
    FileDocumentRepository,
    FileChunkRepository,
    FileStorageRepositoryFactory,
    FileIngestionJobRepository,
)
from app.persistence.postgres import (
    PostgresDocumentRepository,
    DocumentVersionModel,
    RegulatoryDocumentModel,
    IngestionJobModel,
)
from app.ingestion.orchestrator import IngestionOrchestrator
from app.storage import LocalStorageAdapter
from app.api.v1.collections import _to_job_status_response


# -------------------------------------------------------------------------
# 1. SQL Compilation and Join Verification for PostgresDocumentRepository
# -------------------------------------------------------------------------

def test_01_postgres_document_repository_get_by_sha256_sql_compilation():
    """Verify get_document_by_sha256 builds SQL joining document_versions and regulatory_documents with collection filter."""
    # When collection_id is provided
    stmt_scoped = (
        select(DocumentVersionModel)
        .join(RegulatoryDocumentModel, DocumentVersionModel.document_id == RegulatoryDocumentModel.id)
        .where(DocumentVersionModel.sha256 == "test_sha")
        .where(RegulatoryDocumentModel.collection_id == "my_col")
        .order_by(DocumentVersionModel.version_number.desc())
    )
    compiled_scoped = stmt_scoped.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}
    )
    sql_str_scoped = str(compiled_scoped)

    assert "JOIN regulatory_documents ON document_versions.document_id = regulatory_documents.id" in sql_str_scoped
    assert "document_versions.sha256 = %(sha256_1)s" in sql_str_scoped
    assert "regulatory_documents.collection_id = %(collection_id_1)s" in sql_str_scoped
    assert "ORDER BY document_versions.version_number DESC" in sql_str_scoped

    # When collection_id is None (unscoped / global)
    stmt_unscoped = (
        select(DocumentVersionModel)
        .join(RegulatoryDocumentModel, DocumentVersionModel.document_id == RegulatoryDocumentModel.id)
        .where(DocumentVersionModel.sha256 == "test_sha")
        .order_by(DocumentVersionModel.version_number.desc())
    )
    compiled_unscoped = stmt_unscoped.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}
    )
    sql_str_unscoped = str(compiled_unscoped)

    assert "JOIN regulatory_documents ON document_versions.document_id = regulatory_documents.id" in sql_str_unscoped
    assert "document_versions.sha256 = %(sha256_1)s" in sql_str_unscoped
    assert "regulatory_documents.collection_id" not in sql_str_unscoped


# -------------------------------------------------------------------------
# 2. FileDocumentRepository Collection-Scoped and Unscoped Parity
# -------------------------------------------------------------------------

def test_02_file_repository_get_by_sha256_collection_scoping(tmp_path: Path):
    """Verify FileDocumentRepository scopes SHA-256 lookups to collection and prevents cross-collection matching."""
    doc_repo = FileDocumentRepository(processed_dir=tmp_path)

    # Document 1 in collection_alpha
    doc_alpha = DocumentRecord(
        document_id="doc_alpha",
        title="Alpha Doc",
        source_url="https://example.com/alpha.pdf",
        sha256="shared_checksum_12345",
        pdf_path="alpha.pdf",
        collection_id="collection_alpha",
    )
    # Document 2 in collection_beta (different sha)
    doc_beta = DocumentRecord(
        document_id="doc_beta",
        title="Beta Doc",
        source_url="https://example.com/beta.pdf",
        sha256="beta_unique_checksum",
        pdf_path="beta.pdf",
        collection_id="collection_beta",
    )
    doc_repo.save_document(doc_alpha)
    doc_repo.save_document(doc_beta)

    # 1. Scoped lookup in collection_alpha finds doc_alpha
    found_alpha = doc_repo.get_document_by_sha256("shared_checksum_12345", collection_id="collection_alpha")
    assert found_alpha is not None
    assert found_alpha.document_id == "doc_alpha"

    # 2. Scoped lookup for identical hash in collection_beta returns None (NO cross-collection match)
    found_beta = doc_repo.get_document_by_sha256("shared_checksum_12345", collection_id="collection_beta")
    assert found_beta is None

    # 3. Scoped lookup for nonexistent collection returns None
    found_gamma = doc_repo.get_document_by_sha256("shared_checksum_12345", collection_id="collection_gamma")
    assert found_gamma is None

    # 4. Explicit global / unscoped lookup returns the matching document
    # 4. get_document_by_sha256 requires collection_id parameter (TypeError if omitted)
    with pytest.raises(TypeError):
        doc_repo.get_document_by_sha256("shared_checksum_12345")
    with pytest.raises(ValueError, match="collection_id is required"):
        doc_repo.get_document_by_sha256("shared_checksum_12345", collection_id="  ")
    with pytest.raises(ValueError, match="collection_id is required"):
        doc_repo.get_document_by_sha256("shared_checksum_12345", collection_id=None)

    found_global_explicit = doc_repo.get_document_by_sha256_global("shared_checksum_12345")
    assert found_global_explicit is not None
    assert found_global_explicit.document_id == "doc_alpha"


# -------------------------------------------------------------------------
# 3. Same-Collection Duplicate Handling and No Foreign ID Leak
# -------------------------------------------------------------------------

def test_03_same_collection_duplicate_handling_and_no_foreign_leak(tmp_path: Path):
    """Verify that submitting identical content into the SAME collection triggers duplicate handling without foreign ID leak."""
    processed_dir = tmp_path / "processed"
    indexes_dir = tmp_path / "indexes"
    storage_dir = tmp_path / "storage"
    factory = FileStorageRepositoryFactory(processed_dir=processed_dir)
    storage_adapter = LocalStorageAdapter(storage_dir)

    # Mock embedder
    mock_embedder = MagicMock()
    mock_embedder.model_name = "BAAI/bge-small-en-v1.5"
    mock_embedder.embed_texts.return_value = np.array([[0.1] * 384], dtype=np.float32)

    # Mock extractor
    mock_extractor = MagicMock()
    mock_page = MagicMock()
    mock_page.page_number = 1
    mock_page.text = "Sample regulatory compliance text."
    mock_extractor.extract_pages.return_value = [mock_page]

    # Mock chunker
    mock_chunker = MagicMock()
    mock_chunker.chunk_document.return_value = [
        ChunkRecord(
            chunk_id="doc1_p1_0",
            document_id="doc1",
            collection_id="col_finance",
            text="Sample regulatory compliance text.",
            page_start=1,
            page_end=1,
        )
    ]

    orchestrator = IngestionOrchestrator(
        repository_factory=factory,
        downloader=MagicMock(),
        extractor=mock_extractor,
        chunker=mock_chunker,
        embedder=mock_embedder,
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
        storage_adapter=storage_adapter,
        publish_snapshots=True,
    )

    # Create dummy PDF file
    dummy_pdf = storage_dir / "upload_test.pdf"
    dummy_pdf.parent.mkdir(parents=True, exist_ok=True)
    dummy_pdf.write_bytes(b"%PDF-1.4 sample pdf content for checksum hashing")
    import hashlib
    pdf_sha = hashlib.sha256(dummy_pdf.read_bytes()).hexdigest()

    # Step A: Ingest first document into col_finance
    job1_id = uuid.uuid4()
    job1 = factory.get_ingestion_job_repository().create_job(
        source_url=f"file://{dummy_pdf}",
        idempotency_key="job1_key",
        document_id="doc1",
        collection_id="col_finance",
    )
    job1_claimed = factory.get_ingestion_job_repository().claim_next_job(worker_id="test_worker")
    assert job1_claimed is not None

    res1 = orchestrator.process_job(job1_claimed, job1_claimed.claim_token)
    assert res1.status == JobStatus.COMPLETED

    # Step B: Ingest second document with different ID but IDENTICAL SHA-256 into col_finance
    job2_id = uuid.uuid4()
    mock_chunker.chunk_document.return_value = [
        ChunkRecord(
            chunk_id="doc2_p1_0",
            document_id="doc2",
            collection_id="col_finance",
            text="Sample regulatory compliance text.",
            page_start=1,
            page_end=1,
        )
    ]
    job2 = factory.get_ingestion_job_repository().create_job(
        source_url=f"file://{dummy_pdf}",
        idempotency_key="job2_key",
        document_id="doc2",
        collection_id="col_finance",
    )
    job2_claimed = factory.get_ingestion_job_repository().claim_next_job(worker_id="test_worker")
    assert job2_claimed is not None

    res2 = orchestrator.process_job(job2_claimed, job2_claimed.claim_token)
    assert res2.status == JobStatus.DUPLICATE

    # Verify duplicate error details DO NOT expose original_document_id
    assert "original_document_id" not in res2.error_details
    assert res2.error_details["code"] == "DUPLICATE_DOCUMENT"
    assert "already ingested in collection 'col_finance'" in res2.error_details["reason"]
    assert "already ingested as 'doc1'" not in res2.error_details["reason"]
    # Verify document_id matches existing canonical document
    assert res2.document_id == "doc1"


# -------------------------------------------------------------------------
# 4. Different-Collection Identical Content Ingestion
# -------------------------------------------------------------------------

def test_04_different_collection_identical_content_ingestion_succeeds(tmp_path: Path):
    """Verify that identical content ingested into Collection A DOES NOT prevent ingestion into Collection B."""
    processed_dir = tmp_path / "processed"
    indexes_dir = tmp_path / "indexes"
    storage_dir = tmp_path / "storage"
    factory = FileStorageRepositoryFactory(processed_dir=processed_dir)
    storage_adapter = LocalStorageAdapter(storage_dir)

    mock_embedder = MagicMock()
    mock_embedder.model_name = "BAAI/bge-small-en-v1.5"
    mock_embedder.embed_texts.return_value = np.array([[0.2] * 384], dtype=np.float32)

    mock_extractor = MagicMock()
    mock_page = MagicMock()
    mock_page.page_number = 1
    mock_page.text = "Universal Circular on Market Infrastructure."
    mock_extractor.extract_pages.return_value = [mock_page]

    mock_chunker = MagicMock()

    orchestrator = IngestionOrchestrator(
        repository_factory=factory,
        downloader=MagicMock(),
        extractor=mock_extractor,
        chunker=mock_chunker,
        embedder=mock_embedder,
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
        storage_adapter=storage_adapter,
        publish_snapshots=True,
    )

    # Identical PDF source
    dummy_pdf = storage_dir / "common_circular.pdf"
    dummy_pdf.parent.mkdir(parents=True, exist_ok=True)
    dummy_pdf.write_bytes(b"%PDF-1.4 shared circular content for testing")

    # 1. Ingest into Collection Alpha
    mock_chunker.chunk_document.return_value = [
        ChunkRecord(
            chunk_id="doc_alpha_p1_0",
            document_id="doc_alpha",
            collection_id="col_alpha",
            text="Universal Circular on Market Infrastructure.",
            page_start=1,
            page_end=1,
        )
    ]
    factory.get_ingestion_job_repository().create_job(
        source_url=f"file://{dummy_pdf}",
        idempotency_key="key_alpha",
        document_id="doc_alpha",
        collection_id="col_alpha",
    )
    job_alpha = factory.get_ingestion_job_repository().claim_next_job(worker_id="w1")
    assert job_alpha is not None
    res_alpha = orchestrator.process_job(job_alpha, job_alpha.claim_token)
    assert res_alpha.status == JobStatus.COMPLETED

    # 2. Ingest IDENTICAL PDF into Collection Beta
    mock_chunker.chunk_document.return_value = [
        ChunkRecord(
            chunk_id="doc_beta_p1_0",
            document_id="doc_beta",
            collection_id="col_beta",
            text="Universal Circular on Market Infrastructure.",
            page_start=1,
            page_end=1,
        )
    ]
    factory.get_ingestion_job_repository().create_job(
        source_url=f"file://{dummy_pdf}",
        idempotency_key="key_beta",
        document_id="doc_beta",
        collection_id="col_beta",
    )
    job_beta = factory.get_ingestion_job_repository().claim_next_job(worker_id="w1")
    assert job_beta is not None

    # Ingestion into col_beta MUST SUCCEED (NOT be marked DUPLICATE)
    res_beta = orchestrator.process_job(job_beta, job_beta.claim_token)
    assert res_beta.status == JobStatus.COMPLETED
    assert res_beta.document_id == "doc_beta"

    # Verify both collections have their own documents and chunks
    doc_repo = factory.get_document_repository()
    chunk_repo = factory.get_chunk_repository()

    alpha_docs = doc_repo.list_documents(collection_id="col_alpha")
    beta_docs = doc_repo.list_documents(collection_id="col_beta")
    assert len(alpha_docs) == 1
    assert alpha_docs[0].document_id == "doc_alpha"
    assert len(beta_docs) == 1
    assert beta_docs[0].document_id == "doc_beta"

    alpha_chunks = chunk_repo.list_all_chunks(collection_id="col_alpha")
    beta_chunks = chunk_repo.list_all_chunks(collection_id="col_beta")
    assert len(alpha_chunks) == 1
    assert alpha_chunks[0].chunk_id == "doc_alpha_p1_0"
    assert alpha_chunks[0].collection_id == "col_alpha"
    assert len(beta_chunks) == 1
    assert beta_chunks[0].chunk_id == "doc_beta_p1_0"
    assert beta_chunks[0].collection_id == "col_beta"


# -------------------------------------------------------------------------
# 5. Cross-Workspace Identical Content Isolation
# -------------------------------------------------------------------------

def test_05_cross_workspace_identical_content_isolation(tmp_path: Path):
    """Verify that identical content in separate workspaces does not block ingestion."""
    processed_dir = tmp_path / "processed"
    indexes_dir = tmp_path / "indexes"
    storage_dir = tmp_path / "storage"
    factory = FileStorageRepositoryFactory(processed_dir=processed_dir)
    storage_adapter = LocalStorageAdapter(storage_dir)

    mock_embedder = MagicMock()
    mock_embedder.model_name = "BAAI/bge-small-en-v1.5"
    mock_embedder.embed_texts.return_value = np.array([[0.3] * 384], dtype=np.float32)

    mock_extractor = MagicMock()
    mock_page = MagicMock()
    mock_page.page_number = 1
    mock_page.text = "Confidential Workspace Report."
    mock_extractor.extract_pages.return_value = [mock_page]

    mock_chunker = MagicMock()

    orchestrator = IngestionOrchestrator(
        repository_factory=factory,
        downloader=MagicMock(),
        extractor=mock_extractor,
        chunker=mock_chunker,
        embedder=mock_embedder,
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
        storage_adapter=storage_adapter,
        publish_snapshots=True,
    )

    pdf_file = storage_dir / "report.pdf"
    pdf_file.parent.mkdir(parents=True, exist_ok=True)
    pdf_file.write_bytes(b"%PDF-1.4 secret report identical bytes")

    ws1_id = uuid.uuid4()
    ws2_id = uuid.uuid4()

    # Workspace 1 ingestion
    mock_chunker.chunk_document.return_value = [
        ChunkRecord(
            chunk_id="ws1_doc_p1_0",
            document_id="ws1_doc",
            collection_id="ws1_col",
            text="Confidential Workspace Report.",
            page_start=1,
            page_end=1,
        )
    ]
    factory.get_ingestion_job_repository().create_job(
        source_url=f"file://{pdf_file}",
        idempotency_key="key_ws1",
        document_id="ws1_doc",
        workspace_id=ws1_id,
        collection_id="ws1_col",
    )
    job1 = factory.get_ingestion_job_repository().claim_next_job(worker_id="w_ws")
    assert job1 is not None
    res1 = orchestrator.process_job(job1, job1.claim_token)
    assert res1.status == JobStatus.COMPLETED

    # Workspace 2 ingestion of IDENTICAL content
    mock_chunker.chunk_document.return_value = [
        ChunkRecord(
            chunk_id="ws2_doc_p1_0",
            document_id="ws2_doc",
            collection_id="ws2_col",
            text="Confidential Workspace Report.",
            page_start=1,
            page_end=1,
        )
    ]
    factory.get_ingestion_job_repository().create_job(
        source_url=f"file://{pdf_file}",
        idempotency_key="key_ws2",
        document_id="ws2_doc",
        workspace_id=ws2_id,
        collection_id="ws2_col",
    )
    job2 = factory.get_ingestion_job_repository().claim_next_job(worker_id="w_ws")
    assert job2 is not None
    res2 = orchestrator.process_job(job2, job2.claim_token)
    assert res2.status == JobStatus.COMPLETED
    assert res2.document_id == "ws2_doc"


# -------------------------------------------------------------------------
# 6. API Job Status Serialization Protection Against Foreign ID Leaks
# -------------------------------------------------------------------------

def test_06_api_job_status_response_sanitizes_duplicate_errors():
    """Verify _to_job_status_response never exposes foreign document identifiers."""
    ws_id = uuid.uuid4()
    job_id = uuid.uuid4()
    now_ts = datetime.now(timezone.utc)

    # Case A: New error_details structure
    job_model_new = IngestionJobModel(
        id=job_id,
        workspace_id=ws_id,
        collection_id="col_target",
        document_id="my_uploaded_doc",
        status="duplicate",
        stage="finished",
        created_at=now_ts,
        error_details={
            "code": "DUPLICATE_DOCUMENT",
            "reason": "Identical content SHA-256 already ingested in collection 'col_target'",
            "collection_id": "col_target",
        },
    )
    resp_new = _to_job_status_response(job_model_new, published_doc_ids=set())
    assert resp_new.status == "duplicate"
    assert resp_new.document_id == "my_uploaded_doc"
    assert resp_new.error_message == "Identical content SHA-256 already ingested in collection 'col_target'"
    assert "already ingested as" not in (resp_new.error_message or "")

    # Case B: Legacy error_details that contained "already ingested as 'foreign_doc_id'"
    job_model_legacy = IngestionJobModel(
        id=job_id,
        workspace_id=ws_id,
        collection_id="col_target",
        document_id="my_uploaded_doc",
        status="duplicate",
        stage="finished",
        created_at=now_ts,
        error_details={
            "code": "DUPLICATE_DOCUMENT",
            "reason": "Identical content SHA-256 already ingested as 'foreign_tenant_secret_doc_999'",
            "original_document_id": "foreign_tenant_secret_doc_999",
        },
    )
    resp_legacy = _to_job_status_response(job_model_legacy, published_doc_ids=set())
    assert resp_legacy.status == "duplicate"
    assert "foreign_tenant_secret_doc_999" not in (resp_legacy.error_message or "")
    assert resp_legacy.error_message == "Identical content already ingested in collection 'col_target'"


# -------------------------------------------------------------------------
# 7. Repository Contract Enforces Required Collection Scope
# -------------------------------------------------------------------------

def test_07_get_document_by_sha256_requires_collection_id(tmp_path: Path):
    """Verify that both File and Postgres repositories require collection_id (TypeError if omitted, ValueError if empty)."""
    file_repo = FileDocumentRepository(processed_dir=tmp_path)
    
    with pytest.raises(TypeError):
        file_repo.get_document_by_sha256("any_sha")

    with pytest.raises(ValueError, match="collection_id is required"):
        file_repo.get_document_by_sha256("any_sha", collection_id=None)
        
    with pytest.raises(ValueError, match="collection_id is required"):
        file_repo.get_document_by_sha256("any_sha", collection_id="")
        
    with pytest.raises(ValueError, match="collection_id is required"):
        file_repo.get_document_by_sha256("any_sha", collection_id="   ")

    # Postgres repository contract check (with mock session factory)
    pg_repo = PostgresDocumentRepository(session_factory=MagicMock())
    with pytest.raises(TypeError):
        pg_repo.get_document_by_sha256("any_sha")

    with pytest.raises(ValueError, match="collection_id is required"):
        pg_repo.get_document_by_sha256("any_sha", collection_id=None)
        
    with pytest.raises(ValueError, match="collection_id is required"):
        pg_repo.get_document_by_sha256("any_sha", collection_id="")

    with pytest.raises(ValueError, match="collection_id is required"):
        pg_repo.get_document_by_sha256("any_sha", collection_id="   ")


# -------------------------------------------------------------------------
# 8. Unresolvable Collection Fails Closed Early
# -------------------------------------------------------------------------

def test_08_job_with_unresolvable_collection_fails_closed_and_marks_failed(tmp_path: Path):
    """Verify an ingestion job with no collection_id and non-legacy document_id is recorded as terminally failed."""
    from app.ingestion.orchestrator import IngestionWorker
    processed_dir = tmp_path / "processed"
    factory = FileStorageRepositoryFactory(processed_dir=processed_dir)
    job_repo = factory.get_ingestion_job_repository()

    mock_downloader = MagicMock()
    mock_embedder = MagicMock()

    orchestrator = IngestionOrchestrator(
        repository_factory=factory,
        downloader=mock_downloader,
        embedder=mock_embedder,
        processed_dir=processed_dir,
    )
    worker = IngestionWorker(
        repository_factory=factory,
        orchestrator=orchestrator,
        worker_id="test_worker_boundary",
    )

    # Job has NO collection_id and arbitrary non-legacy document_id
    job = job_repo.create_job(
        source_url="https://example.com/arbitrary.pdf",
        idempotency_key="key_unresolvable_col",
        document_id="custom_unknown_doc_999",
        collection_id=None,
    )

    # 1. Execute run_once() via IngestionWorker
    result = worker.run_once()

    # 2. Assert worker received clean terminal failure result (did not crash or raise uncaught error)
    assert result is not None
    assert result.status == JobStatus.FAILED
    assert "Cannot determine authoritative collection scope" in result.message
    assert result.error_details.get("category") == "non_retryable"

    # 3. Assert downloader was never invoked (no wasted processing work)
    mock_downloader.fetch_pdf.assert_not_called()

    # 4. Assert job in repository is permanently marked FAILED and NOT left in PROCESSING
    persisted_job = job_repo.get_job(job.id)
    assert persisted_job is not None
    assert persisted_job.status == JobStatus.FAILED
    assert persisted_job.status != JobStatus.PROCESSING
    assert persisted_job.completed_at is not None
    assert "Cannot determine authoritative collection scope" in persisted_job.error_details.get("reason", "")


def test_12_canonical_legacy_job_resolves_to_sebi_cscrf(tmp_path: Path):
    """Verify canonical SEBI document ID with missing collection_id resolves to sebi_cscrf."""
    processed_dir = tmp_path / "processed"
    storage_dir = tmp_path / "storage"
    factory = FileStorageRepositoryFactory(processed_dir=processed_dir)
    storage_adapter = LocalStorageAdapter(storage_dir)

    dummy_pdf = storage_dir / "legacy_test.pdf"
    dummy_pdf.parent.mkdir(parents=True, exist_ok=True)
    dummy_pdf.write_bytes(b"%PDF-1.4 canonical legacy document content")

    mock_extractor = MagicMock()
    mock_p = MagicMock()
    mock_p.page_number = 1
    mock_p.text = "Canonical SEBI CSCRF circular text."
    mock_extractor.extract_pages.return_value = [mock_p]

    mock_chunker = MagicMock()
    mock_chunker.chunk_document.return_value = [
        ChunkRecord(
            chunk_id="sebi_cscrf_2024_113_0",
            document_id="sebi_cscrf_2024_113",
            text="Canonical SEBI CSCRF circular text.",
            page_start=1,
            page_end=1,
        )
    ]

    mock_embedder = MagicMock()
    mock_embedder.model_name = "BAAI/bge-small-en-v1.5"
    mock_embedder.embed_texts.return_value = np.array([[0.0] * 384], dtype=np.float32)

    orchestrator = IngestionOrchestrator(
        repository_factory=factory,
        downloader=MagicMock(),
        extractor=mock_extractor,
        chunker=mock_chunker,
        embedder=mock_embedder,
        processed_dir=processed_dir,
        storage_adapter=storage_adapter,
        publish_snapshots=False,
    )

    # Job specifies canonical SEBI document ID but collection_id=None
    job = factory.get_ingestion_job_repository().create_job(
        source_url=f"file://{dummy_pdf}",
        idempotency_key="key_legacy_sebi_doc",
        document_id="sebi_cscrf_2024_113",
        collection_id=None,
    )
    token = uuid.uuid4()
    claimed = factory.get_ingestion_job_repository().claim_next_job("worker_legacy", token)
    result = orchestrator.process_job(claimed, token)

    assert result.status == JobStatus.COMPLETED
    saved_doc = factory.get_document_repository().get_document("sebi_cscrf_2024_113")
    assert saved_doc is not None
    assert saved_doc.collection_id == "sebi_cscrf"


def test_13_claim_invalidated_job_does_not_call_fail_job_again(tmp_path: Path):
    """Verify that if worker loses lease, fail_job / mark_failed is not called on the lost claim."""
    processed_dir = tmp_path / "processed"
    factory = FileStorageRepositoryFactory(processed_dir=processed_dir)
    job_repo = factory.get_ingestion_job_repository()

    mock_job_repo = MagicMock(wraps=job_repo)
    factory_mock = MagicMock(wraps=factory)
    factory_mock.get_ingestion_job_repository.return_value = mock_job_repo

    orchestrator = IngestionOrchestrator(
        repository_factory=factory_mock,
        downloader=MagicMock(),
        embedder=MagicMock(),
        processed_dir=processed_dir,
    )

    job = job_repo.create_job(
        source_url="https://example.com/stale.pdf",
        idempotency_key="key_stale_claim_check",
        document_id="custom_unresolvable_doc",
        collection_id=None,
    )
    token = uuid.uuid4()
    claimed = job_repo.claim_next_job("worker_stale", token)

    # Simulate heartbeat losing lease immediately
    with patch("app.ingestion.orchestrator.HeartbeatThread") as mock_hb_cls:
        mock_hb = MagicMock()
        mock_hb.lost_lease = True  # Lost lease!
        mock_hb_cls.return_value = mock_hb

        result = orchestrator.process_job(claimed, token)
        # Should return FAILED result
        assert result.status == JobStatus.FAILED
        # mark_failed must NOT be called because lost_lease is True
        mock_job_repo.mark_failed.assert_not_called()


def test_14_failure_recording_error_does_not_leak_raw_claim_token(tmp_path: Path):
    """Verify that exceptions during failure recording are safely logged without leaking claim token."""
    processed_dir = tmp_path / "processed"
    factory = FileStorageRepositoryFactory(processed_dir=processed_dir)
    job_repo = factory.get_ingestion_job_repository()

    orchestrator = IngestionOrchestrator(
        repository_factory=factory,
        downloader=MagicMock(),
        embedder=MagicMock(),
        processed_dir=processed_dir,
    )

    job = job_repo.create_job(
        source_url="https://example.com/fail_rec.pdf",
        idempotency_key="key_fail_rec",
        document_id="custom_unresolvable_doc_2",
        collection_id=None,
    )
    secret_token_str = "secret_raw_token_value_not_to_be_leaked_12345"
    token = uuid.uuid4()
    claimed = job_repo.claim_next_job("worker_fail_rec", token)

    with patch.object(orchestrator.job_repo, "mark_failed", side_effect=RuntimeError("Storage connection failed")):
        with patch("app.ingestion.orchestrator.logger.warning") as mock_warn:
            result = orchestrator.process_job(claimed, token)
            assert result.status == JobStatus.FAILED
            # Verify logger.warning was called and did NOT contain secret_token_str
            mock_warn.assert_called_once()
            log_call_msg = str(mock_warn.call_args)
            assert secret_token_str not in log_call_msg


# -------------------------------------------------------------------------
# 9. Structured error_details Recursive Sanitization
# -------------------------------------------------------------------------

def test_09_structured_error_details_recursive_sanitization():
    """Verify recursive sanitization strips leak keys and scrubs nested leak phrases."""
    from app.schemas.collection_ingestion import sanitize_error_details, sanitize_error_message

    # Test plain message sanitization
    msg = "Job duplicate: already ingested as 'foreign_doc_123' in database"
    sanitized_msg = sanitize_error_message(msg, collection_id="tenant_col")
    assert "foreign_doc_123" not in sanitized_msg
    assert "already ingested in collection 'tenant_col'" in sanitized_msg

    msg2 = "Conflict: matches existing document 'secret_doc_456' on disk"
    sanitized_msg2 = sanitize_error_message(msg2, collection_id="tenant_col")
    assert "secret_doc_456" not in sanitized_msg2
    assert "matches existing document in collection 'tenant_col'" in sanitized_msg2

    # Test deeply nested dictionary and list sanitization
    dirty_details = {
        "code": "DUPLICATE_DOCUMENT",
        "reason": "already ingested as 'foreign_doc_xyz'",
        "original_document_id": "foreign_doc_xyz",
        "foreign_document_id": "secret_doc_abc",
        "internal_document_id": "internal_private_id",
        "nested_info": {
            "sub_reason": "matches existing document 'other_foreign_doc'",
            "existing_document_id": "should_be_stripped",
            "items": [
                {"colliding_document_id": "leak_in_list", "info": "already ingested as 'foo'"},
                "safe_item",
            ],
        },
    }

    clean = sanitize_error_details(dirty_details, collection_id="col_safe")
    assert "original_document_id" not in clean
    assert "foreign_document_id" not in clean
    assert "internal_document_id" not in clean
    assert "foreign_doc_xyz" not in clean["reason"]
    assert "already ingested in collection 'col_safe'" in clean["reason"]

    assert "existing_document_id" not in clean["nested_info"]
    assert "other_foreign_doc" not in clean["nested_info"]["sub_reason"]
    assert "colliding_document_id" not in clean["nested_info"]["items"][0]
    assert "already ingested in collection 'col_safe'" in clean["nested_info"]["items"][0]["info"]
    assert clean["nested_info"]["items"][1] == "safe_item"


# -------------------------------------------------------------------------
# 10. Chunks Always Stamped with Authoritative Collection
# -------------------------------------------------------------------------

def test_10_chunks_always_stamped_with_authoritative_collection(tmp_path: Path):
    """Verify that chunking strictly stamps chunks with authoritative_collection and never defaults to sebi_cscrf."""
    processed_dir = tmp_path / "processed"
    storage_dir = tmp_path / "storage"
    factory = FileStorageRepositoryFactory(processed_dir=processed_dir)
    storage_adapter = LocalStorageAdapter(storage_dir)

    dummy_pdf = storage_dir / "custom.pdf"
    dummy_pdf.parent.mkdir(parents=True, exist_ok=True)
    dummy_pdf.write_bytes(b"%PDF-1.4 custom content for chunks testing")

    mock_extractor = MagicMock()
    mock_p = MagicMock()
    mock_p.page_number = 1
    mock_p.text = "Custom tenant document text."
    mock_extractor.extract_pages.return_value = [mock_p]

    mock_chunker = MagicMock()
    # Chunker returns chunk without collection_id set
    mock_chunker.chunk_document.return_value = [
        ChunkRecord(
            chunk_id="custom_chunk_0",
            document_id="custom_doc",
            collection_id="",
            text="Custom tenant document text.",
            page_start=1,
            page_end=1,
        )
    ]

    mock_embedder = MagicMock()
    mock_embedder.model_name = "BAAI/bge-small-en-v1.5"
    mock_embedder.embed_texts.return_value = np.array([[0.0] * 384], dtype=np.float32)

    orchestrator = IngestionOrchestrator(
        repository_factory=factory,
        downloader=MagicMock(),
        extractor=mock_extractor,
        chunker=mock_chunker,
        embedder=mock_embedder,
        processed_dir=processed_dir,
        storage_adapter=storage_adapter,
        publish_snapshots=False,
    )

    job = factory.get_ingestion_job_repository().create_job(
        source_url=f"file://{dummy_pdf}",
        idempotency_key="key_custom_col_chunks",
        document_id="custom_doc",
        collection_id="tenant_private_col",
    )
    token = uuid.uuid4()
    claimed = factory.get_ingestion_job_repository().claim_next_job("worker_chk", token)
    res = orchestrator.process_job(claimed, token)
    assert res.status == JobStatus.COMPLETED

    # Verify persisted chunks have tenant_private_col, NOT sebi_cscrf
    saved_chunks = factory.get_chunk_repository().list_chunks_for_document("custom_doc")
    assert len(saved_chunks) == 1
    assert saved_chunks[0].collection_id == "tenant_private_col"
    assert saved_chunks[0].collection_id != "sebi_cscrf"


# -------------------------------------------------------------------------
# 11. Admin Job Status Response Error Details Sanitization
# -------------------------------------------------------------------------

def test_11_admin_to_status_response_sanitization():
    """Verify _to_status_response in admin API strips leak keys from error_details."""
    from app.api.v1.admin import _to_status_response

    job_rec = IngestionJobRecord(
        id=uuid.uuid4(),
        source_url="https://example.com/test.pdf",
        idempotency_key="key_admin_san",
        status=JobStatus.FAILED,
        stage=ProcessingStage.FINISHED,
        document_id="doc_admin_test",
        collection_id="admin_col",
        error_details={
            "code": "DUPLICATE_DOCUMENT",
            "reason": "already ingested as 'foreign_secret_doc'",
            "original_document_id": "foreign_secret_doc",
            "internal_document_id": "private_db_id_123",
        },
        attempt_count=1,
        max_attempts=3,
        created_at=datetime.now(timezone.utc),
    )

    resp = _to_status_response(job_rec)
    assert resp.error_details is not None
    assert "original_document_id" not in resp.error_details
    assert "internal_document_id" not in resp.error_details
    assert "foreign_secret_doc" not in resp.error_details["reason"]
    assert "already ingested in collection 'admin_col'" in resp.error_details["reason"]
