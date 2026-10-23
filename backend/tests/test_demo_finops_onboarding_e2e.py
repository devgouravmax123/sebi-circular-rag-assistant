"""
End-to-end integration and onboarding tests for Phase 3.2:
Synthetic FinOps Collection Onboarding Workflow (demo_finops).

Verifies:
1. Manifest validation and path containment against unauthorized traversal.
2. Synthetic PDF generation and deterministic ingestion into partitioned directories.
3. Search index building, SHA-256 integrity, chunk creation, and atomic snapshot publication.
4. Dynamic collection catalog discovery via GET /api/v1/collections.
5. In-scope query answering: 30-day unattached disk SLA answered with verbatim citation against demo_finops.
6. Controlled abstention on out-of-scope question: SEBI quarterly cybersecurity audit deadlines safely abstains.
7. Cross-collection evidence isolation: Foreign chunks injected into retrieval are quarantined and rejected.
8. Repeatability and clean isolation without touching pre-existing SEBI or general_synthetic data.
"""
import hashlib
import json
import os
from pathlib import Path
import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient
import fitz

from app.main import app
from app.api.v1.query import (
    get_collection_registry,
    get_retrieval_service,
    get_safety_gate,
    get_synthesis_service,
)
from app.ingestion.manifest_runner import (
    ManifestIngestionRunner,
    ManifestSecurityError,
    ManifestValidationError,
    resolve_and_validate_pdf_path,
)
from app.ingestion.collection_registry import CollectionRegistry
from app.retrieval.service import RetrievalService
from app.grounding.safety_gate import AnswerSafetyGate
from app.grounding.validator import CitationValidator
from app.synthesis.service import SynthesisService
from app.synthesis.mock_provider import MockSynthesisProvider
from app.schemas.manifest import (
    CollectionManifest,
    CollectionStatus,
    GroundingPolicyType,
)
from app.schemas.safety_gate import AnswerDisposition
from app.schemas.grounding import RegulatoryStatus
from app.schemas.synthesis import SynthesisStatus
from app.schemas.retrieval import RetrievalResult, RetrievalResponse


def compute_sha256(file_path: Path) -> str:
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


@pytest.fixture
def finops_env(tmp_path: Path):
    """
    Sets up a fully isolated, clean directory structure for the demo_finops onboarding test.
    Preserves all repository production and baseline directories.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)

    data_dir = repo_root / "data"
    raw_dir = data_dir / "raw" / "finops"
    processed_dir = data_dir / "processed"
    indexes_dir = data_dir / "indexes"
    manifests_dir = data_dir / "manifests"

    for d in (raw_dir, processed_dir, indexes_dir, manifests_dir):
        d.mkdir(parents=True)

    # 1. Deterministic Synthetic PDF Content using PyMuPDF textbox formatting
    # Required facts:
    # - Cloud tags: Owner, CostCenter
    # - Unattached-disk reclamation SLA: 30 days
    # Multiple clauses ensure multi-chunk sliding window coverage across pages
    doc = fitz.open()

    # Page 1: Cloud Tagging Policy
    p1 = doc.new_page()  # type: ignore[attr-defined]
    t1 = (
        "DEMO SYNTHETIC FINOPS GUIDE - FOR DEMONSTRATION USE ONLY. "
        "Section 1: Mandatory Cloud Tagging Policy. "
        "All cloud infrastructure assets across production and staging environments must bear standard cost-allocation tags. "
        "Every provisioned virtual machine, serverless function, and cloud storage bucket must have mandatory tags: Owner and CostCenter. "
        + " ".join([f"tagging_rule_{i} requires mandatory Owner and CostCenter resource tags." for i in range(40)])
    )
    p1.insert_textbox(fitz.Rect(50, 50, 550, 750), t1, fontsize=9)

    # Page 2: Unattached Disk SLA
    p2 = doc.new_page()  # type: ignore[attr-defined]
    t2 = (
        "DEMO SYNTHETIC FINOPS GUIDE - FOR DEMONSTRATION USE ONLY. "
        "Section 2: Storage Volume Reclamation SLA. "
        "Under the Acme FinOps policy, unattached-disk reclamation SLA is 30 days. "
        "Any storage volume unattached for 30 days is automatically archived and deleted. "
        + " ".join([f"reclamation_clause_{i} unattached disk retention SLA is 30 days." for i in range(40)])
    )
    p2.insert_textbox(fitz.Rect(50, 50, 550, 750), t2, fontsize=9)

    pdf_path = raw_dir / "acme_finops_governance_guide.pdf"
    doc.save(str(pdf_path))
    doc.close()

    pdf_sha256 = compute_sha256(pdf_path)

    # 2. Declarative Manifest following the exact CollectionManifest schema
    manifest_dict = {
        "collection_id": "demo_finops",
        "name": "Acme Cloud FinOps & Governance Guide",
        "description": "Synthetic demonstration FinOps and cloud resource governance guidelines.",
        "regulator": None,
        "domain": "FINOPS",
        "enabled": True,
        "policy": {
            "policy_type": "general_static",
            "enforce_regulatory_graph": False,
            "enforce_verbatim_citation": True,
            "enforce_retrieval_binding": True,
        },
        "documents": [
            {
                "document_id": "acme_finops_guide_2026",
                "title": "Acme Cloud FinOps & Governance Guide",
                "pdf_path": "data/raw/finops/acme_finops_governance_guide.pdf",
                "department": "Cloud FinOps & Infrastructure Governance",
                "issue_date": "2026-03-01",
                "effective_date": "2026-04-01",
                "document_type": "standard_operating_procedure",
                "source_url": "https://internal.acme.example/finops/guide_v1.pdf",
                "expected_sha256": pdf_sha256,
                "metadata": {
                    "synthetic_demo": True,
                    "target_audience": "FinOps Engineers",
                },
            }
        ],
    }

    manifest_file = manifests_dir / "demo_finops.json"
    manifest_file.write_text(json.dumps(manifest_dict, indent=2), encoding="utf-8")

    return {
        "repo_root": repo_root,
        "raw_dir": raw_dir,
        "processed_dir": processed_dir,
        "indexes_dir": indexes_dir,
        "manifests_dir": manifests_dir,
        "pdf_path": pdf_path,
        "pdf_sha256": pdf_sha256,
        "manifest_file": manifest_file,
    }


def test_manifest_validation_and_security_traversal(finops_env):
    """Verify manifest schema validation and path containment enforcement."""
    # 1. Valid resolution inside approved root
    valid_resolved = resolve_and_validate_pdf_path(
        "data/raw/finops/acme_finops_governance_guide.pdf",
        repo_root=finops_env["repo_root"],
    )
    assert valid_resolved == finops_env["pdf_path"].resolve()

    # 2. Traversal attempt rejected
    with pytest.raises(ManifestSecurityError) as exc_info:
        resolve_and_validate_pdf_path(
            "../../etc/shadow",
            repo_root=finops_env["repo_root"],
        )
    assert "Path traversal or unauthorized path detected" in str(exc_info.value)

    # 3. Registry load of valid manifest
    registry = CollectionRegistry(
        manifests_dir=finops_env["manifests_dir"],
        indexes_dir=finops_env["indexes_dir"],
    )
    assert registry.has_collection("demo_finops")
    manifest = registry.get_collection("demo_finops")
    assert manifest.collection_id == "demo_finops"
    assert manifest.name == "Acme Cloud FinOps & Governance Guide"
    assert manifest.domain == "FINOPS"
    assert manifest.policy.policy_type == GroundingPolicyType.GENERAL_STATIC
    assert manifest.policy.enforce_regulatory_graph is False
    assert manifest.policy.enforce_verbatim_citation is True
    assert manifest.policy.enforce_retrieval_binding is True


def test_manifest_ingestion_and_snapshot_publication(finops_env):
    """
    Ingest synthetic PDF via ManifestIngestionRunner and atomically publish search snapshot.
    Verifies chunk IDs, page ranges, SHA-256 checksum, and snapshot layout.
    """
    runner = ManifestIngestionRunner(
        repo_root=finops_env["repo_root"],
        processed_dir=finops_env["processed_dir"],
        indexes_dir=finops_env["indexes_dir"],
    )

    report = runner.run_manifest(finops_env["manifest_file"], publish_index=True)

    assert report.collection_id == "demo_finops"
    assert report.total_declared == 1
    assert report.success_count == 1
    assert report.error_count == 0
    assert report.duplicate_count == 0
    assert report.index_published is True
    assert report.snapshot_id is not None

    # Check outcome details
    outcome = report.document_outcomes[0]
    assert outcome.document_id == "acme_finops_guide_2026"
    assert outcome.status == "success"
    assert outcome.page_count == 2
    assert outcome.chunk_count >= 2
    assert outcome.sha256 == finops_env["pdf_sha256"]

    # Verify partitioned persistence in processed_dir
    doc_dir = finops_env["processed_dir"] / "acme_finops_guide_2026"
    assert (doc_dir / "document.json").exists()
    assert (doc_dir / "pages.json").exists()
    assert (doc_dir / "chunks.json").exists()

    with open(doc_dir / "chunks.json", "r", encoding="utf-8") as f:
        chunks = json.load(f)
    assert len(chunks) >= 2
    chunk_pages = {c["page_start"] for c in chunks}
    assert 1 in chunk_pages
    assert 2 in chunk_pages

    # Verify partitioned snapshot layout under data/indexes/demo_finops/
    finops_index_dir = finops_env["indexes_dir"] / "demo_finops"
    active_ptr_file = finops_index_dir / "active_snapshot.json"
    assert active_ptr_file.exists()

    with open(active_ptr_file, "r", encoding="utf-8") as f:
        ptr_data = json.load(f)
    assert ptr_data["collection_id"] == "demo_finops"
    assert ptr_data["snapshot_id"] == report.snapshot_id

    snap_dir = finops_index_dir / "snapshots" / report.snapshot_id
    assert snap_dir.exists()
    assert (snap_dir / "manifest.json").exists()
    assert (snap_dir / "chunks.json").exists()
    assert (snap_dir / "documents.json").exists()

    # Re-running manifest is idempotent duplicate detection
    repeat_report = runner.run_manifest(finops_env["manifest_file"], publish_index=False)
    assert repeat_report.duplicate_count == 1
    assert repeat_report.success_count == 0


def test_catalog_discovery_and_frontend_contract(finops_env):
    """
    Verify GET /api/v1/collections dynamically discovers the published demo_finops collection.
    """
    runner = ManifestIngestionRunner(
        repo_root=finops_env["repo_root"],
        processed_dir=finops_env["processed_dir"],
        indexes_dir=finops_env["indexes_dir"],
    )
    runner.run_manifest(finops_env["manifest_file"], publish_index=True)

    registry = CollectionRegistry(
        manifests_dir=finops_env["manifests_dir"],
        indexes_dir=finops_env["indexes_dir"],
    )
    retrieval_svc = RetrievalService(
        processed_dir=finops_env["processed_dir"],
        indexes_dir=finops_env["indexes_dir"],
    )

    app.dependency_overrides[get_collection_registry] = lambda: registry
    app.dependency_overrides[get_retrieval_service] = lambda: retrieval_svc

    try:
        client = TestClient(app)
        res = client.get("/api/v1/collections")
        assert res.status_code == 200
        collections = res.json()
        assert len(collections) == 1

        demo = collections[0]
        assert demo["collection_id"] == "demo_finops"
        assert demo["name"] == "Acme Cloud FinOps & Governance Guide"
        assert demo["domain"] == "FINOPS"
        assert demo["policy_type"] == "general_static"
        assert demo["status"] == "active"
        assert "Synthetic demonstration" in demo["description"]
    finally:
        app.dependency_overrides.pop(get_collection_registry, None)
        app.dependency_overrides.pop(get_retrieval_service, None)


def test_end_to_end_in_scope_finops_query(finops_env):
    """
    Ask an in-scope question about the 30-day unattached-disk policy.
    Assert verified excerpts, verbatim citation, INFORMATIONAL status, and FULLY_ANSWERED synthesis.
    """
    # 1. Ingest & publish
    runner = ManifestIngestionRunner(
        repo_root=finops_env["repo_root"],
        processed_dir=finops_env["processed_dir"],
        indexes_dir=finops_env["indexes_dir"],
    )
    runner.run_manifest(finops_env["manifest_file"], publish_index=True)

    # 2. Setup services pointing to demo_finops
    registry = CollectionRegistry(
        manifests_dir=finops_env["manifests_dir"],
        indexes_dir=finops_env["indexes_dir"],
    )
    retrieval_svc = RetrievalService(
        processed_dir=finops_env["processed_dir"],
        indexes_dir=finops_env["indexes_dir"],
    )
    mock_synthesis = SynthesisService(provider=MockSynthesisProvider())

    app.dependency_overrides[get_collection_registry] = lambda: registry
    app.dependency_overrides[get_retrieval_service] = lambda: retrieval_svc
    app.dependency_overrides[get_synthesis_service] = lambda: mock_synthesis

    try:
        client = TestClient(app)
        query_text = "What is the unattached disk retention SLA?"
        response = client.post(
            "/api/v1/query",
            json={
                "query": query_text,
                "collection_id": "demo_finops",
            },
        )
        assert response.status_code == 200
        data = response.json()

        # Disposition check
        assert data["disposition"] in [
            AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS,
            AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS,
        ]
        assert data["synthesis_status"] == SynthesisStatus.FULLY_ANSWERED.value

        # Verified excerpts check
        assert len(data["verified_excerpts"]) > 0
        found_sla = False
        for exc in data["verified_excerpts"]:
            assert exc["collection_id"] == "demo_finops"
            assert exc["document_id"] == "acme_finops_guide_2026"
            assert exc["regulatory_status"] == RegulatoryStatus.INFORMATIONAL.value
            if "30 days" in exc["excerpt_text"]:
                found_sla = True
        assert found_sla, "Expected 30 days SLA in verified excerpt text"

        # Claim citation check
        assert len(data["claim_citations"]) > 0
        assert any("30 days" in c["claim_text"] or "sla" in c["claim_text"].lower() for c in data["claim_citations"])
        for c in data["claim_citations"]:
            assert len(c["supporting_chunk_ids"]) > 0
            for cit in c.get("citations", []):
                assert cit["collection_id"] == "demo_finops"
                assert cit["document_id"] == "acme_finops_guide_2026"

        # Answer text contains verified fact
        assert "30 days" in data["answer_text"]

    finally:
        app.dependency_overrides.pop(get_collection_registry, None)
        app.dependency_overrides.pop(get_retrieval_service, None)
        app.dependency_overrides.pop(get_synthesis_service, None)


def test_controlled_abstention_on_unsupported_query(finops_env):
    """
    Ask an unsupported question about SEBI quarterly cybersecurity audit deadlines to demo_finops.
    Expect controlled abstention with empty verified excerpts and zero claims.
    """
    runner = ManifestIngestionRunner(
        repo_root=finops_env["repo_root"],
        processed_dir=finops_env["processed_dir"],
        indexes_dir=finops_env["indexes_dir"],
    )
    runner.run_manifest(finops_env["manifest_file"], publish_index=True)

    registry = CollectionRegistry(
        manifests_dir=finops_env["manifests_dir"],
        indexes_dir=finops_env["indexes_dir"],
    )
    retrieval_svc = RetrievalService(
        processed_dir=finops_env["processed_dir"],
        indexes_dir=finops_env["indexes_dir"],
    )
    mock_synthesis = SynthesisService(provider=MockSynthesisProvider())

    app.dependency_overrides[get_collection_registry] = lambda: registry
    app.dependency_overrides[get_retrieval_service] = lambda: retrieval_svc
    app.dependency_overrides[get_synthesis_service] = lambda: mock_synthesis

    try:
        client = TestClient(app)
        query_text = "What are the SEBI quarterly cybersecurity audit deadlines under CSCRF?"
        response = client.post(
            "/api/v1/query",
            json={
                "query": query_text,
                "collection_id": "demo_finops",
            },
        )
        assert response.status_code == 200
        data = response.json()

        assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION
        assert data["synthesis_status"] == SynthesisStatus.ABSTAINED.value
        assert len(data["verified_excerpts"]) == 0
        assert len(data["claim_citations"]) == 0
        assert "Controlled Abstention" in data["answer_text"]

    finally:
        app.dependency_overrides.pop(get_collection_registry, None)
        app.dependency_overrides.pop(get_retrieval_service, None)
        app.dependency_overrides.pop(get_synthesis_service, None)


def test_cross_collection_quarantine_and_isolation(finops_env):
    """
    Ensure foreign chunks (e.g. from sebi_cscrf) injected into retrieval candidates
    during a demo_finops query are quarantined and rejected before evidence verification.
    """
    runner = ManifestIngestionRunner(
        repo_root=finops_env["repo_root"],
        processed_dir=finops_env["processed_dir"],
        indexes_dir=finops_env["indexes_dir"],
    )
    runner.run_manifest(finops_env["manifest_file"], publish_index=True)

    registry = CollectionRegistry(
        manifests_dir=finops_env["manifests_dir"],
        indexes_dir=finops_env["indexes_dir"],
    )
    retrieval_svc = RetrievalService(
        processed_dir=finops_env["processed_dir"],
        indexes_dir=finops_env["indexes_dir"],
    )
    finops_chunks = retrieval_svc.get_chunks("demo_finops")
    valid_finops_chunk = finops_chunks[0]

    mock_results = [
        RetrievalResult(
            chunk_id=valid_finops_chunk.chunk_id,
            document_id=valid_finops_chunk.document_id,
            text=valid_finops_chunk.text,
            page_start=valid_finops_chunk.page_start,
            page_end=valid_finops_chunk.page_end,
            score=1.0,
            rank=1,
            metadata={"collection_id": "demo_finops"},
        ),
        # Foreign chunk claiming sebi_cscrf
        RetrievalResult(
            chunk_id="sebi_cscrf_2024_113_p1_0",
            document_id="sebi_cscrf_2024_113",
            text="Foreign SEBI CSCRF circular content that should never leak into demo_finops.",
            page_start=1,
            page_end=1,
            score=0.9,
            rank=2,
            metadata={"collection_id": "sebi_cscrf"},
        ),
    ]

    retrieval_svc.search_hybrid = MagicMock(
        return_value=RetrievalResponse(
            query="unattached disk SLA",
            total_results=2,
            results=mock_results,
        )
    )

    mock_synthesis = SynthesisService(provider=MockSynthesisProvider())

    app.dependency_overrides[get_collection_registry] = lambda: registry
    app.dependency_overrides[get_retrieval_service] = lambda: retrieval_svc
    app.dependency_overrides[get_synthesis_service] = lambda: mock_synthesis

    try:
        client = TestClient(app)
        response = client.post(
            "/api/v1/query",
            json={
                "query": "unattached disk SLA",
                "collection_id": "demo_finops",
            },
        )
        assert response.status_code == 200
        data = response.json()

        # Check that foreign chunk was strictly excluded
        for exc in data["verified_excerpts"]:
            assert exc["chunk_id"] != "sebi_cscrf_2024_113_p1_0"
            assert exc["collection_id"] == "demo_finops"

        for clm in data["claim_citations"]:
            assert clm["chunk_id"] != "sebi_cscrf_2024_113_p1_0"
            assert clm["collection_id"] == "demo_finops"

        assert "Foreign SEBI CSCRF" not in data["answer_text"]

    finally:
        app.dependency_overrides.pop(get_collection_registry, None)
        app.dependency_overrides.pop(get_retrieval_service, None)
        app.dependency_overrides.pop(get_synthesis_service, None)
