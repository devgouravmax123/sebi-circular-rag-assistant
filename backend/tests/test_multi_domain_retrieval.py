"""
Tests for Phase 8C — Generalized Multi-Domain SEBI Retrieval.

Validates:
1. Dynamic multi-domain retrieval across distinct regulatory frameworks (CSCRF and LODR).
2. Pre-fusion filtering precision: domain filters applied consistently to BM25 and dense retrieval before fusion.
3. Zero leakage: LODR query with domain="CSCRF" filter does NOT return LODR chunks; CSCRF query with domain="LODR" abstains safely.
4. Date range filtering (start_date, end_date) on circular issue dates.
5. Document ID targeted filtering.
6. Provenance and metadata completeness: retrieved chunks contain regulator, domain, effective_date, sha256.
7. Version-aware searchability: older published version vs newer pending version distinction.
8. Validation error rejection: invalid regulator (e.g. 'RBI') produces clear HTTP 422 error.
9. Backward compatibility: default queries with no filter preserve identical behavior and return results across published domains.
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
    """Fixture providing isolated environment with both CSCRF and LODR documents."""
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    indexes_dir = tmp_path / "indexes"
    indexes_dir.mkdir(parents=True, exist_ok=True)

    # 1. Copy 5 CSCRF pilot documents from data/processed
    real_processed = Path("data/processed")
    for doc_folder in real_processed.iterdir():
        if doc_folder.is_dir() and not doc_folder.name.startswith((".", "_")):
            target_folder = processed_dir / doc_folder.name
            target_folder.mkdir(parents=True, exist_ok=True)
            for f in doc_folder.iterdir():
                if f.is_file():
                    target_folder.joinpath(f.name).write_bytes(f.read_bytes())

    factory = FileStorageRepositoryFactory(processed_dir=processed_dir)
    doc_repo = factory.get_document_repository()
    chunk_repo = factory.get_chunk_repository()

    # 2. Add an authoritative SEBI LODR regulatory document (Listing Obligations and Disclosure Requirements)
    lodr_doc_id = "sebi_lodr_2025_021"
    lodr_doc = DocumentRecord(
        document_id=lodr_doc_id,
        circular_number="SEBI/HO/CFD/CFD-PoD-1/P/CIR/2025/21",
        title="Amendments to SEBI (Listing Obligations and Disclosure Requirements) Regulations, 2015",
        department="Corporation Finance Department (CFD)",
        issue_date="2025-02-15",
        effective_date="2025-04-01",
        source_url="https://www.sebi.gov.in/legal/circulars/feb-2025/lodr_amendments_21.html",
        sha256="c0ffee1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
        pdf_path="raw_documents/lodr_2025_21.pdf",
        extraction_status="extracted",
        regulator="SEBI",
        domain="LODR",
    )
    doc_repo.save_document(lodr_doc)

    lodr_chunks = [
        ChunkRecord(
            chunk_id=f"{lodr_doc_id}_p1_001",
            document_id=lodr_doc_id,
            text=(
                "Regulation 30 of LODR Regulations requires listed entities to disclose material events "
                "within thirty minutes of closure of the board meeting or twenty-four hours of occurrence."
            ),
            page_start=1,
            page_end=1,
            chunk_type="body",
            metadata={"provisions": ["Regulation 30"]},
        ),
        ChunkRecord(
            chunk_id=f"{lodr_doc_id}_p2_002",
            document_id=lodr_doc_id,
            text=(
                "Under Regulation 33 of LODR Regulations, quarterly financial results must be submitted "
                "within forty-five days from the end of each quarter with limited review report."
            ),
            page_start=2,
            page_end=2,
            chunk_type="body",
            metadata={"provisions": ["Regulation 33"]},
        ),
    ]
    chunk_repo.save_chunks(lodr_chunks)

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
    )

    # Publish unified snapshot containing both CSCRF and LODR
    pub_res = publisher.build_and_publish_snapshot()
    assert pub_res.success is True
    assert pub_res.total_documents == 6
    assert pub_res.total_chunks == 170

    retrieval_service = RetrievalService(processed_dir=processed_dir, indexes_dir=indexes_dir)
    retrieval_service.load_and_index_corpus()
    retrieval_service.load_and_index_dense()

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
        "lodr_doc_id": lodr_doc_id,
    }

    app.dependency_overrides.clear()


def test_multi_domain_retrieval_without_filters(multi_domain_env):
    """Test 1: Unfiltered hybrid search retrieves chunks across both CSCRF and LODR domains."""
    service = multi_domain_env["service"]

    # 1. Search CSCRF topic
    res_cscrf = service.search_hybrid("ISO 27001 Qualified REs", top_k=3)
    assert res_cscrf.total_results > 0
    assert any(r.domain == "CSCRF" for r in res_cscrf.results)

    # 2. Search LODR topic
    res_lodr = service.search_hybrid("quarterly financial results forty-five days Regulation 33", top_k=3)
    assert res_lodr.total_results > 0
    assert any(r.domain == "LODR" for r in res_lodr.results)
    lodr_match = next(r for r in res_lodr.results if r.domain == "LODR")
    assert lodr_match.regulator == "SEBI"
    assert lodr_match.circular_number == "SEBI/HO/CFD/CFD-PoD-1/P/CIR/2025/21"
    assert lodr_match.sha256 is not None


def test_domain_filter_precision_and_zero_leakage(multi_domain_env):
    """Test 2: Pre-fusion domain filter strictly isolates candidate pool with zero out-of-filter leakage."""
    service = multi_domain_env["service"]

    filter_lodr = RetrievalFilter(domain="LODR")
    filter_cscrf = RetrievalFilter(domain="CSCRF")

    # Search LODR topic with domain="LODR" -> must return only LODR chunks
    res_lodr = service.search_hybrid(
        "Regulation 30 material events disclosure board meeting",
        top_k=5,
        filters=filter_lodr,
    )
    assert res_lodr.total_results > 0
    assert all(r.domain == "LODR" for r in res_lodr.results)
    assert all(r.document_id == "sebi_lodr_2025_021" for r in res_lodr.results)

    # Search LODR topic with domain="CSCRF" -> must NOT return LODR chunks (zero leakage)
    res_leakage_check = service.search_hybrid(
        "Regulation 30 material events disclosure board meeting",
        top_k=5,
        filters=filter_cscrf,
    )
    # None of the returned chunks can be LODR
    assert all(r.domain == "CSCRF" for r in res_leakage_check.results)
    assert all(r.document_id != "sebi_lodr_2025_021" for r in res_leakage_check.results)


def test_document_id_filter(multi_domain_env):
    """Test 3: Targeted document_id filter restricts retrieval to a single circular."""
    service = multi_domain_env["service"]

    filter_doc = RetrievalFilter(document_id="sebi_cscrf_2025_119")
    res = service.search_hybrid("cyber resilience technical clarifications", top_k=5, filters=filter_doc)
    assert res.total_results > 0
    assert all(r.document_id == "sebi_cscrf_2025_119" for r in res.results)


def test_date_range_filter(multi_domain_env):
    """Test 4: Issue date filters (start_date, end_date) prune out-of-range circulars."""
    service = multi_domain_env["service"]

    # Filter for circulars issued in 2024 only
    filter_2024 = RetrievalFilter(start_date="2024-01-01", end_date="2024-12-31")
    res_2024 = service.search_hybrid("cybersecurity and cyber resilience framework", top_k=5, filters=filter_2024)
    assert res_2024.total_results > 0
    assert all(r.issue_date is not None and r.issue_date.startswith("2024") for r in res_2024.results)

    # Filter for circulars issued in 2025 only
    filter_2025 = RetrievalFilter(start_date="2025-01-01", end_date="2025-12-31")
    res_2025 = service.search_hybrid("cybersecurity framework", top_k=5, filters=filter_2025)
    assert res_2025.total_results > 0
    assert all(r.issue_date is not None and r.issue_date.startswith("2025") for r in res_2025.results)


def test_query_api_domain_filtering_and_abstention(multi_domain_env):
    """Test 5: POST /api/v1/query handles multi-domain filters and performs controlled abstention."""
    client = multi_domain_env["client"]

    # 1. Query LODR domain successfully
    payload_lodr = {
        "query": "What are the disclosure timelines under Regulation 30?",
        "domain": "LODR",
        "top_k": 3,
    }
    resp_lodr = client.post("/api/v1/query", json=payload_lodr)
    assert resp_lodr.status_code == 200
    data_lodr = resp_lodr.json()
    assert data_lodr["query"] == payload_lodr["query"]
    assert "SEBI LODR corpus" in data_lodr["summary_narrative"]

    # 2. Query CSCRF topic with domain="LODR" -> should abstain controlled because CSCRF concepts don't exist in LODR
    payload_mismatch = {
        "query": "What are the CISO appointment requirements under CSCRF?",
        "domain": "LODR",
        "top_k": 3,
    }
    resp_mismatch = client.post("/api/v1/query", json=payload_mismatch)
    assert resp_mismatch.status_code == 200
    data_mismatch = resp_mismatch.json()
    assert data_mismatch["disposition"].upper() == "CONTROLLED_ABSTENTION"
    assert "SEBI LODR corpus" in data_mismatch["summary_narrative"]


def test_query_api_invalid_regulator_rejection(multi_domain_env):
    """Test 6: Query with unsupported regulator (e.g. 'RBI') produces HTTP 422 validation error."""
    client = multi_domain_env["client"]

    payload = {
        "query": "What are the capital adequacy requirements?",
        "regulator": "RBI",
    }
    resp = client.post("/api/v1/query", json=payload)
    assert resp.status_code == 422
    assert "Only 'SEBI' is supported as the regulator in Phase 8C" in resp.text


def test_version_aware_searchability_distinction(multi_domain_env):
    """
    Test 7 (Requirement 6):
    Verify that document searchability checks distinguish between published and unpublished versions.
    If a document has a published version sha256_A, but a newly ingested version sha256_B is pending,
    is_document_searchable(doc_id, sha256=sha256_B) must return False.
    """
    publisher = multi_domain_env["publisher"]
    lodr_doc_id = multi_domain_env["lodr_doc_id"]

    published_sha = "c0ffee1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
    new_pending_sha = "9999991234567890abcdef1234567890abcdef1234567890abcdef1234567890"

    # Document-only check returns True
    assert publisher.is_document_searchable(lodr_doc_id) is True

    # Published version check returns True
    assert publisher.is_document_searchable(lodr_doc_id, sha256=published_sha) is True

    # Un-published new version check returns False (prevents falsely reporting new version searchable)
    assert publisher.is_document_searchable(lodr_doc_id, sha256=new_pending_sha) is False

    # Verify actual Admin Job Status API endpoint behavior
    client = multi_domain_env["client"]
    factory = multi_domain_env["factory"]
    job_repo = factory.get_ingestion_job_repository()
    import uuid
    from app.schemas.ingestion_job import IngestionJobRecord, JobStatus, ProcessingStage

    # Job 1: Completed with published SHA -> searchable: True
    job_pub = job_repo.create_job(
        source_url="https://example.com/lodr_pub.pdf",
        idempotency_key="lodr-pub-test-01",
        document_id=lodr_doc_id,
    )
    claimed_pub = job_repo.claim_next_job(worker_id="test-worker-1")
    assert claimed_pub is not None
    job_repo.mark_completed(
        job_id=claimed_pub.id,
        claim_token=claimed_pub.claim_token,
        document_id=lodr_doc_id,
        sha256=published_sha,
    )

    # Job 2: Completed with new pending SHA -> searchable: False, publication_status: pending_publication
    job_pending = job_repo.create_job(
        source_url="https://example.com/lodr_pending.pdf",
        idempotency_key="lodr-pending-test-02",
        document_id=lodr_doc_id,
    )
    claimed_pending = job_repo.claim_next_job(worker_id="test-worker-1")
    assert claimed_pending is not None
    job_repo.mark_completed(
        job_id=claimed_pending.id,
        claim_token=claimed_pending.claim_token,
        document_id=lodr_doc_id,
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
