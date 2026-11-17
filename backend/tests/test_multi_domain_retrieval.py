"""
Tests for Generalized Multi-Domain Retrieval.

Validates:
1. Dynamic multi-domain retrieval across distinct document domains (e.g. SECURITY and FINOPS).
2. Pre-fusion filtering precision: domain filters applied consistently before fusion.
3. Zero leakage: FINOPS query with domain="SECURITY" filter does NOT return FINOPS chunks.
4. Date range filtering (start_date, end_date) on document issue dates.
5. Document ID targeted filtering.
6. Provenance and metadata completeness: retrieved chunks contain domain, effective_date, sha256.
7. Version-aware searchability: older published version vs newer pending version distinction.
8. Validation error rejection: invalid parameters (e.g. top_k=0) produce clear HTTP 422 error.
9. Backward compatibility: default queries with no filter preserve behavior and return results across published domains.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import app
from app.persistence.file_repo import FileStorageRepositoryFactory
from app.retrieval.publisher import IndexPublisher
from app.retrieval.service import RetrievalService
from app.schemas.document import DocumentRecord, ChunkRecord
from app.schemas.retrieval import RetrievalFilter
from app.schemas.query import QueryRequest


TEST_ADMIN_KEY = "test-secret-multi-domain-key-888"


@pytest.fixture
def multi_domain_env(tmp_path, monkeypatch):
    """Fixture providing isolated environment with both SECURITY and FINOPS documents."""
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    indexes_dir = tmp_path / "indexes"
    indexes_dir.mkdir(parents=True, exist_ok=True)

    factory = FileStorageRepositoryFactory(processed_dir=processed_dir)
    doc_repo = factory.get_document_repository()
    chunk_repo = factory.get_chunk_repository()

    # Document 1: Security Domain
    sec_doc_id = "acme_sec_2024_001"
    sec_doc = DocumentRecord(
        document_id=sec_doc_id,
        circular_number="POL-2024-001",
        title="Acme Security Operations Policy 2024",
        department="Security",
        issue_date="2024-05-10",
        effective_date="2024-06-01",
        source_url="https://example.com/sec_2024.pdf",
        sha256="1111111111111111111111111111111111111111111111111111111111111111",
        pdf_path="raw_documents/sec_2024.pdf",
        extraction_status="extracted",
        domain="SECURITY",
        collection_id="general_synthetic",
    )
    doc_repo.save_document(sec_doc)

    sec_chunks = [
        ChunkRecord(
            chunk_id=f"{sec_doc_id}_p1_001",
            document_id=sec_doc_id,
            collection_id="general_synthetic",
            text=(
                "Acme security operations policy requires all personnel to use hardware MFA keys "
                "and 16-character complex passwords for all systems."
            ),
            page_start=1,
            page_end=1,
            chunk_type="body",
            metadata={"provisions": ["Section 1 - Authentication"]},
        ),
        ChunkRecord(
            chunk_id=f"{sec_doc_id}_p2_002",
            document_id=sec_doc_id,
            collection_id="general_synthetic",
            text=(
                "Access deprovisioning for terminated staff must occur within 60 seconds of notification."
            ),
            page_start=2,
            page_end=2,
            chunk_type="body",
            metadata={"provisions": ["Section 2 - Deprovisioning"]},
        ),
    ]
    chunk_repo.save_chunks(sec_chunks)

    # Document 2: FinOps Domain
    finops_doc_id = "acme_finops_2025_002"
    finops_doc = DocumentRecord(
        document_id=finops_doc_id,
        circular_number="GUIDE-2025-002",
        title="Acme Cloud FinOps and Budgeting Guide 2025",
        department="Finance",
        issue_date="2025-02-15",
        effective_date="2025-04-01",
        source_url="https://example.com/finops_2025.pdf",
        sha256="2222222222222222222222222222222222222222222222222222222222222222",
        pdf_path="raw_documents/finops_2025.pdf",
        extraction_status="extracted",
        domain="FINOPS",
        collection_id="general_synthetic",
    )
    doc_repo.save_document(finops_doc)

    finops_chunks = [
        ChunkRecord(
            chunk_id=f"{finops_doc_id}_p1_001",
            document_id=finops_doc_id,
            collection_id="general_synthetic",
            text=(
                "Cloud spending thresholds require department leads to review quarterly expenditure against budget."
            ),
            page_start=1,
            page_end=1,
            chunk_type="body",
            metadata={"provisions": ["Guideline 30 - Expenditure Review"]},
        ),
        ChunkRecord(
            chunk_id=f"{finops_doc_id}_p2_002",
            document_id=finops_doc_id,
            collection_id="general_synthetic",
            text=(
                "Under section 33 of FinOps guide, quarterly cloud financial reports must be submitted "
                "within forty-five days from the end of each quarter."
            ),
            page_start=2,
            page_end=2,
            chunk_type="body",
            metadata={"provisions": ["Guideline 33 - Quarterly Reporting"]},
        ),
    ]
    chunk_repo.save_chunks(finops_chunks)

    test_settings = Settings(
        admin_api_key=TEST_ADMIN_KEY,
        processed_dir=str(processed_dir),
        indexes_dir=str(indexes_dir),
        database_url=None,
    )

    publisher = IndexPublisher(
        repository_factory=factory,
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
        collection_id="general_synthetic",
    )

    # Publish unified snapshot containing both SECURITY and FINOPS
    pub_res = publisher.build_and_publish_snapshot()
    assert pub_res.success is True
    assert pub_res.total_documents == 2
    assert pub_res.total_chunks == 4

    retrieval_service = RetrievalService(processed_dir=processed_dir, indexes_dir=indexes_dir)
    retrieval_service.load_and_index_corpus("general_synthetic")

    monkeypatch.setenv("ADMIN_API_KEY", TEST_ADMIN_KEY)
    monkeypatch.setenv("PROCESSED_DIR", str(processed_dir))
    monkeypatch.setenv("INDEXES_DIR", str(indexes_dir))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings(force_reload=True)

    app.dependency_overrides[get_settings] = lambda: test_settings
    from app.api.v1.query import get_retrieval_service, get_safety_gate
    from app.grounding.safety_gate import AnswerSafetyGate
    safety_gate = AnswerSafetyGate(processed_dir=processed_dir)
    app.dependency_overrides[get_retrieval_service] = lambda: retrieval_service
    app.dependency_overrides[get_safety_gate] = lambda: safety_gate

    client = TestClient(app)

    yield {
        "client": client,
        "service": retrieval_service,
        "publisher": publisher,
        "factory": factory,
        "finops_doc_id": finops_doc_id,
        "sec_doc_id": sec_doc_id,
    }

    app.dependency_overrides.clear()


def test_multi_domain_retrieval_without_filters(multi_domain_env):
    """Test 1: Unfiltered hybrid search retrieves chunks across both SECURITY and FINOPS domains."""
    service = multi_domain_env["service"]

    # 1. Search SECURITY topic
    res_sec = service.search_hybrid("hardware MFA keys passwords", top_k=3, collection_id="general_synthetic")
    assert res_sec.total_results > 0
    assert any(r.domain == "SECURITY" for r in res_sec.results)

    # 2. Search FINOPS topic
    res_fin = service.search_hybrid("quarterly cloud financial reports forty-five days", top_k=3, collection_id="general_synthetic")
    assert res_fin.total_results > 0
    assert any(r.domain == "FINOPS" for r in res_fin.results)
    fin_match = next(r for r in res_fin.results if r.domain == "FINOPS")
    assert fin_match.sha256 is not None


def test_domain_filter_precision_and_zero_leakage(multi_domain_env):
    """Test 2: Pre-fusion domain filter strictly isolates candidate pool with zero out-of-filter leakage."""
    service = multi_domain_env["service"]

    filter_fin = RetrievalFilter(domain="FINOPS")
    filter_sec = RetrievalFilter(domain="SECURITY")

    # Search FINOPS topic with domain="FINOPS" -> must return only FINOPS chunks
    res_fin = service.search_hybrid(
        "quarterly cloud financial reports forty-five days",
        top_k=5,
        filters=filter_fin,
        collection_id="general_synthetic",
    )
    assert res_fin.total_results > 0
    assert all(r.domain == "FINOPS" for r in res_fin.results)
    assert all(r.document_id == "acme_finops_2025_002" for r in res_fin.results)

    # Search FINOPS topic with domain="SECURITY" -> must NOT return FINOPS chunks (zero leakage)
    res_leakage_check = service.search_hybrid(
        "quarterly cloud financial reports forty-five days",
        top_k=5,
        filters=filter_sec,
        collection_id="general_synthetic",
    )
    assert all(r.domain == "SECURITY" for r in res_leakage_check.results)
    assert all(r.document_id != "acme_finops_2025_002" for r in res_leakage_check.results)


def test_document_id_filter(multi_domain_env):
    """Test 3: Targeted document_id filter restricts retrieval to a single document."""
    service = multi_domain_env["service"]

    filter_doc = RetrievalFilter(document_id="acme_sec_2024_001")
    res = service.search_hybrid("security operations policy", top_k=5, filters=filter_doc, collection_id="general_synthetic")
    assert res.total_results > 0
    assert all(r.document_id == "acme_sec_2024_001" for r in res.results)


def test_date_range_filter(multi_domain_env):
    """Test 4: Issue date filters (start_date, end_date) prune out-of-range documents."""
    service = multi_domain_env["service"]

    # Filter for documents issued in 2024 only
    filter_2024 = RetrievalFilter(start_date="2024-01-01", end_date="2024-12-31")
    res_2024 = service.search_hybrid("policy guide", top_k=5, filters=filter_2024, collection_id="general_synthetic")
    assert res_2024.total_results > 0
    assert all(r.issue_date is not None and r.issue_date.startswith("2024") for r in res_2024.results)

    # Filter for documents issued in 2025 only
    filter_2025 = RetrievalFilter(start_date="2025-01-01", end_date="2025-12-31")
    res_2025 = service.search_hybrid("policy guide", top_k=5, filters=filter_2025, collection_id="general_synthetic")
    assert res_2025.total_results > 0
    assert all(r.issue_date is not None and r.issue_date.startswith("2025") for r in res_2025.results)


def test_query_api_domain_filtering_and_abstention(multi_domain_env):
    """Test 5: POST /api/v1/query handles multi-domain filters and performs controlled abstention."""
    client = multi_domain_env["client"]

    # 1. Query FINOPS domain successfully
    payload_fin = {
        "query": "What are the quarterly financial report timelines under FinOps?",
        "domain": "FINOPS",
        "top_k": 3,
    }
    resp_fin = client.post("/api/v1/query", json=payload_fin)
    assert resp_fin.status_code == 200
    data_fin = resp_fin.json()
    assert data_fin["query"] == payload_fin["query"]

    # 2. Query unrelated topic with domain="FINOPS" -> should abstain controlled
    payload_mismatch = {
        "query": "What are the margin trading rules for equity derivatives?",
        "domain": "FINOPS",
        "top_k": 3,
    }
    resp_mismatch = client.post("/api/v1/query", json=payload_mismatch)
    assert resp_mismatch.status_code == 200
    data_mismatch = resp_mismatch.json()
    assert data_mismatch["disposition"].upper() == "CONTROLLED_ABSTENTION"


def test_query_api_invalid_parameter_rejection(multi_domain_env):
    """Test 6: Query with invalid parameters (e.g. top_k=0) produces HTTP 422 validation error."""
    client = multi_domain_env["client"]

    payload = {
        "query": "What are the capital requirements?",
        "top_k": 0,
    }
    resp = client.post("/api/v1/query", json=payload)
    assert resp.status_code == 422


def test_version_aware_searchability_distinction(multi_domain_env):
    """
    Test 7:
    Verify that document searchability checks distinguish between published and unpublished versions.
    If a document has a published version sha256_A, but a newly ingested version sha256_B is pending,
    is_document_searchable(doc_id, sha256=sha256_B) must return False.
    """
    publisher = multi_domain_env["publisher"]
    finops_doc_id = multi_domain_env["finops_doc_id"]

    published_sha = "2222222222222222222222222222222222222222222222222222222222222222"
    new_pending_sha = "9999991234567890abcdef1234567890abcdef1234567890abcdef1234567890"

    # Document-only check returns True
    assert publisher.is_document_searchable(finops_doc_id) is True

    # Published version check returns True
    assert publisher.is_document_searchable(finops_doc_id, sha256=published_sha) is True

    # Un-published new version check returns False (prevents falsely reporting new version searchable)
    assert publisher.is_document_searchable(finops_doc_id, sha256=new_pending_sha) is False

    # Verify actual Admin Job Status API endpoint behavior
    client = multi_domain_env["client"]
    factory = multi_domain_env["factory"]
    job_repo = factory.get_ingestion_job_repository()

    # Job 1: Completed with published SHA -> searchable: True
    job_pub = job_repo.create_job(
        source_url="https://example.com/finops_pub.pdf",
        idempotency_key="finops-pub-test-01",
        document_id=finops_doc_id,
    )
    claimed_pub = job_repo.claim_next_job(worker_id="test-worker-1")
    assert claimed_pub is not None
    job_repo.mark_completed(
        job_id=claimed_pub.id,
        claim_token=claimed_pub.claim_token,
        document_id=finops_doc_id,
        sha256=published_sha,
    )

    # Job 2: Completed with new pending SHA -> searchable: False, publication_status: pending_publication
    job_pending = job_repo.create_job(
        source_url="https://example.com/finops_pending.pdf",
        idempotency_key="finops-pending-test-02",
        document_id=finops_doc_id,
    )
    claimed_pending = job_repo.claim_next_job(worker_id="test-worker-1")
    assert claimed_pending is not None
    job_repo.mark_completed(
        job_id=claimed_pending.id,
        claim_token=claimed_pending.claim_token,
        document_id=finops_doc_id,
        sha256=new_pending_sha,
    )

    headers = {"X-Admin-Key": TEST_ADMIN_KEY}

    resp_pub = client.get(f"/api/v1/admin/jobs/{job_pub.id}/status", headers=headers)
    assert resp_pub.status_code == 200
    data_pub = resp_pub.json()
    assert data_pub["searchable"] is True
    assert data_pub["publication_status"] == "published"

    resp_pending = client.get(f"/api/v1/admin/jobs/{job_pending.id}/status", headers=headers)
    assert resp_pending.status_code == 200
    data_pending = resp_pending.json()
    assert data_pending["searchable"] is False
    assert data_pending["publication_status"] == "pending_publication"
