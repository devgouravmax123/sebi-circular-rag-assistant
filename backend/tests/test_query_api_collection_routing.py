"""
Tests for Phase 2.2B.1: Collection-Aware API Routing and Evidence Isolation.

Verifies:
1. Omitted collection_id preserves default collection routing (HTTP 200).
2. A valid collection_id routes retrieval to that collection.
3. Unknown collection_id is rejected with HTTP 404.
4. Disabled collection is rejected with HTTP 400.
5. Collection without a valid published snapshot is rejected with HTTP 400.
6. Invalid collection_id format (e.g. invalid characters, traversal) is rejected with HTTP 422.
7. A collection-A request cannot use collection-B chunks as evidence (quarantining cross-collection chunks).
8. Missing or conflicting chunk ownership is rejected/quarantined.
9. Duplicate document IDs and titles in two collections cannot cross-contaminate evidence.
10. Empty valid evidence after ownership checks triggers safe controlled abstention.
11. Evidence provenance preserves document_id, chunk_id, and collection_id.
"""
import json
import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.api.v1.query import get_retrieval_service, get_safety_gate, get_collection_registry
from app.schemas.query import QueryResponse
from app.schemas.manifest import (
    CollectionManifest,
    CollectionStatus,
    GroundingPolicyDeclaration,
    GroundingPolicyType,
)
from app.schemas.safety_gate import (
    AnswerDisposition,
    SafetyGateResponse,
    ValidatedExcerptItem,
)
from app.schemas.grounding import RegulatoryStatus
from app.schemas.retrieval import RetrievalResult, RetrievalResponse
from app.ingestion.collection_registry import CollectionRegistry


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def mock_registry(tmp_path):
    """Fixture returning a controlled CollectionRegistry with test collections."""
    manifests_dir = tmp_path / "manifests"
    indexes_dir = tmp_path / "indexes"
    manifests_dir.mkdir(parents=True)
    indexes_dir.mkdir(parents=True)

    # 1. Active general_synthetic collection with published snapshot
    gen_dir = indexes_dir / "general_synthetic"
    gen_snap = gen_dir / "snapshots" / "snap_gen_001"
    gen_snap.mkdir(parents=True)
    (gen_snap / "manifest.json").write_text(json.dumps({"snapshot_id": "snap_gen_001"}), encoding="utf-8")
    (gen_dir / "active_snapshot.json").write_text(
        json.dumps({"snapshot_id": "snap_gen_001", "collection_id": "general_synthetic"}),
        encoding="utf-8",
    )

    gen_manifest = {
        "collection_id": "general_synthetic",
        "name": "General Documentation",
        "enabled": True,
        "policy": {
            "policy_type": "general_static",
            "enforce_regulatory_graph": False,
            "enforce_verbatim_citation": True,
            "enforce_retrieval_binding": True,
        },
    }
    (manifests_dir / "general_synthetic.json").write_text(json.dumps(gen_manifest), encoding="utf-8")

    # 2. Active custom collection 'amfi_nav' with published snapshot
    amfi_dir = indexes_dir / "amfi_nav"
    amfi_snap = amfi_dir / "snapshots" / "snap_amfi_001"
    amfi_snap.mkdir(parents=True)
    (amfi_dir / "active_snapshot.json").write_text(
        json.dumps({"snapshot_id": "snap_amfi_001", "collection_id": "amfi_nav"}),
        encoding="utf-8",
    )

    amfi_manifest = {
        "collection_id": "amfi_nav",
        "name": "AMFI NAV Framework",
        "enabled": True,
        "policy": {
            "policy_type": "general_static",
            "enforce_regulatory_graph": False,
            "enforce_verbatim_citation": True,
            "enforce_retrieval_binding": True,
        },
    }
    (manifests_dir / "amfi_nav.json").write_text(json.dumps(amfi_manifest), encoding="utf-8")

    # 3. Disabled collection 'disabled_col'
    disabled_manifest = {
        "collection_id": "disabled_col",
        "name": "Disabled Collection",
        "enabled": False,
        "policy": {
            "policy_type": "general_static",
            "enforce_regulatory_graph": False,
            "enforce_verbatim_citation": True,
            "enforce_retrieval_binding": True,
        },
    }
    (manifests_dir / "disabled_col.json").write_text(json.dumps(disabled_manifest), encoding="utf-8")

    # 4. Unpublished collection 'unpub_col' (no index snapshot directory)
    unpub_manifest = {
        "collection_id": "unpub_col",
        "name": "Unpublished Collection",
        "enabled": True,
        "policy": {
            "policy_type": "general_static",
            "enforce_regulatory_graph": False,
            "enforce_verbatim_citation": True,
            "enforce_retrieval_binding": True,
        },
    }
    (manifests_dir / "unpub_col.json").write_text(json.dumps(unpub_manifest), encoding="utf-8")

    registry = CollectionRegistry(manifests_dir=manifests_dir, indexes_dir=indexes_dir)
    return registry


def test_routing_omitted_collection_id_preserves_default(client, mock_registry):
    """Omitted collection_id must default to 'general_synthetic' and route to the general index."""
    mock_retrieval = MagicMock()
    mock_retrieval.search_hybrid.return_value = RetrievalResponse(
        query="System architecture standards",
        total_results=1,
        results=[
            RetrievalResult(
                chunk_id="gen_chunk_001",
                document_id="doc_infra_001",
                page_start=1,
                page_end=1,
                text="System architecture standards and guidelines",
                score=1.0,
                rank=1,
                metadata={"collection_id": "general_synthetic"},
            )
        ],
    )

    mock_gate = MagicMock()
    mock_gate.evaluate_draft.return_value = SafetyGateResponse(
        query="System architecture standards",
        disposition=AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS,
        verified_excerpts=[
            ValidatedExcerptItem(
                excerpt_id="excerpt_1",
                source_chunk_id="gen_chunk_001",
                document_id="doc_infra_001",
                collection_id="general_synthetic",
                page_start=1,
                page_end=1,
                excerpt_text="System architecture standards and guidelines",
                regulatory_status=RegulatoryStatus.ACTIVE_CURRENT,
                status_reason="Active documentation",
            )
        ],
        historical_superseded_excerpts=[],
        summary_narrative="Verified Excerpts: 1 active excerpt(s)",
        technical_validation_passed=True,
        retrieval_candidate_count=1,
    )

    app.dependency_overrides[get_collection_registry] = lambda: mock_registry
    app.dependency_overrides[get_retrieval_service] = lambda: mock_retrieval
    app.dependency_overrides[get_safety_gate] = lambda: mock_gate

    try:
        response = client.post("/api/v1/query", json={"query": "System architecture standards"})
        assert response.status_code == 200

        mock_retrieval.search_hybrid.assert_called_once()
        call_kwargs = mock_retrieval.search_hybrid.call_args.kwargs
        assert call_kwargs.get("collection_id") == "general_synthetic"

        data = response.json()
        assert data["disposition"] == AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS.value
        assert len(data["verified_excerpts"]) == 1
        assert data["verified_excerpts"][0]["collection_id"] == "general_synthetic"
    finally:
        app.dependency_overrides.clear()


def test_routing_valid_custom_collection_routes_to_target(client, mock_registry):
    """An explicit valid collection_id routes retrieval to that collection and preserves collection_id."""
    mock_retrieval = MagicMock()
    mock_retrieval.search_hybrid.return_value = RetrievalResponse(
        query="cut-off timing for NAV",
        total_results=1,
        results=[
            RetrievalResult(
                chunk_id="amfi_nav_2024_p5_1",
                document_id="amfi_nav_2024",
                page_start=5,
                page_end=5,
                text="Cut-off timing for applicability of NAV is 3:00 PM for liquid funds.",
                score=1.0,
                rank=1,
                metadata={"collection_id": "amfi_nav"},
            )
        ],
    )

    mock_gate = MagicMock()
    mock_gate.evaluate_draft.return_value = SafetyGateResponse(
        query="cut-off timing for NAV",
        disposition=AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS,
        verified_excerpts=[
            ValidatedExcerptItem(
                excerpt_id="excerpt_1",
                source_chunk_id="amfi_nav_2024_p5_1",
                document_id="amfi_nav_2024",
                collection_id="amfi_nav",
                page_start=5,
                page_end=5,
                excerpt_text="Cut-off timing for applicability of NAV is 3:00 PM for liquid funds.",
                regulatory_status=RegulatoryStatus.ACTIVE_CURRENT,
                status_reason="Active circular",
            )
        ],
        historical_superseded_excerpts=[],
        summary_narrative="Verified Historical Excerpts: 1 active excerpt(s)",
        technical_validation_passed=True,
        retrieval_candidate_count=1,
    )

    app.dependency_overrides[get_collection_registry] = lambda: mock_registry
    app.dependency_overrides[get_retrieval_service] = lambda: mock_retrieval
    app.dependency_overrides[get_safety_gate] = lambda: mock_gate

    try:
        response = client.post(
            "/api/v1/query",
            json={"query": "cut-off timing for NAV", "collection_id": "amfi_nav"},
        )
        assert response.status_code == 200

        mock_retrieval.search_hybrid.assert_called_once()
        call_kwargs = mock_retrieval.search_hybrid.call_args.kwargs
        assert call_kwargs.get("collection_id") == "amfi_nav"

        data = response.json()
        assert len(data["verified_excerpts"]) == 1
        assert data["verified_excerpts"][0]["collection_id"] == "amfi_nav"
    finally:
        app.dependency_overrides.clear()


def test_routing_unknown_collection_rejected_404(client, mock_registry):
    """Unknown collection ID is rejected with HTTP 404."""
    app.dependency_overrides[get_collection_registry] = lambda: mock_registry
    try:
        response = client.post(
            "/api/v1/query",
            json={"query": "any valid question", "collection_id": "completely_unknown_corpus"},
        )
        assert response.status_code == 404
        assert "not registered" in response.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


def test_routing_disabled_collection_rejected_400(client, mock_registry):
    """Disabled collection is rejected with HTTP 400."""
    app.dependency_overrides[get_collection_registry] = lambda: mock_registry
    try:
        response = client.post(
            "/api/v1/query",
            json={"query": "any valid question", "collection_id": "disabled_col"},
        )
        assert response.status_code == 400
        assert "disabled" in response.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


def test_routing_unpublished_collection_rejected_400(client, mock_registry):
    """Collection without an active published snapshot is rejected with HTTP 400."""
    app.dependency_overrides[get_collection_registry] = lambda: mock_registry
    try:
        response = client.post(
            "/api/v1/query",
            json={"query": "any valid question", "collection_id": "unpub_col"},
        )
        assert response.status_code == 400
        assert "unpublished" in response.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


def test_routing_invalid_collection_id_format_rejected_422(client, mock_registry):
    """Path traversal and special character attacks in collection_id are rejected with HTTP 422."""
    traversal_payloads = [
        "../data/indexes",
        "sebi/cscrf",
        "sebi\\cscrf",
        "sebi*cscrf",
        "col:invalid",
        "col name",
    ]
    for bad_id in traversal_payloads:
        resp = client.post(
            "/api/v1/query",
            json={"query": "any valid question", "collection_id": bad_id},
        )
        assert resp.status_code == 422, f"Expected 422 for invalid collection_id: '{bad_id}'"


def test_evidence_isolation_cross_collection_chunks_quarantined(client, mock_registry):
    """
    If retrieval returns candidate chunks from an unrelated collection (e.g. leaking from another index),
    those chunks must be quarantined before reaching the safety gate.
    """
    mock_retrieval = MagicMock()
    mock_retrieval.search_hybrid.return_value = RetrievalResponse(
        query="framework provisions",
        total_results=2,
        results=[
            RetrievalResult(
                chunk_id="foreign_amfi_chunk_1",
                document_id="amfi_nav_doc_001",
                page_start=1,
                page_end=1,
                text="Foreign AMFI candidate text framework provisions",
                score=1.5,
                rank=1,
                metadata={"collection_id": "amfi_nav"},
            ),
            RetrievalResult(
                chunk_id="gen_chunk_001",
                document_id="doc_infra_001",
                page_start=1,
                page_end=1,
                text="Authorized text framework provisions",
                score=1.0,
                rank=2,
                metadata={"collection_id": "general_synthetic"},
            ),
        ],
    )

    mock_gate = MagicMock()
    mock_gate.evaluate_draft.return_value = SafetyGateResponse(
        query="framework provisions",
        disposition=AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS,
        verified_excerpts=[
            ValidatedExcerptItem(
                excerpt_id="excerpt_1",
                source_chunk_id="gen_chunk_001",
                document_id="doc_infra_001",
                collection_id="general_synthetic",
                page_start=1,
                page_end=1,
                excerpt_text="Authorized text",
                regulatory_status=RegulatoryStatus.ACTIVE_CURRENT,
                status_reason="Active documentation",
            )
        ],
        historical_superseded_excerpts=[],
        summary_narrative="Verified Excerpts: 1 active excerpt(s)",
        technical_validation_passed=True,
        retrieval_candidate_count=1,
    )

    app.dependency_overrides[get_collection_registry] = lambda: mock_registry
    app.dependency_overrides[get_retrieval_service] = lambda: mock_retrieval
    app.dependency_overrides[get_safety_gate] = lambda: mock_gate

    try:
        response = client.post(
            "/api/v1/query",
            json={"query": "framework provisions", "collection_id": "general_synthetic"},
        )
        assert response.status_code == 200

        mock_gate.evaluate_draft.assert_called_once()
        draft_payload = mock_gate.evaluate_draft.call_args.kwargs["payload"]
        authorized_ids = mock_gate.evaluate_draft.call_args.kwargs["retrieval_candidates"]

        assert "foreign_amfi_chunk_1" not in authorized_ids
        assert "gen_chunk_001" in authorized_ids
        assert len(draft_payload.excerpts) == 1
        assert draft_payload.excerpts[0].source_chunk_id == "gen_chunk_001"
    finally:
        app.dependency_overrides.clear()


def test_evidence_isolation_unassigned_chunks_quarantined_not_defaulted(client, mock_registry):
    """
    Arbitrary document IDs without declared collection_id must be quarantined.
    """
    mock_retrieval = MagicMock()
    mock_retrieval.search_hybrid.return_value = RetrievalResponse(
        query="unassigned document notice",
        total_results=1,
        results=[
            RetrievalResult(
                chunk_id="unknown_regulatory_notice_999_p1_0",
                document_id="unknown_regulatory_notice_999",
                page_start=1,
                page_end=1,
                text="Arbitrary unassigned notice text",
                score=1.0,
                rank=1,
            )
        ],
    )

    app.dependency_overrides[get_collection_registry] = lambda: mock_registry
    app.dependency_overrides[get_retrieval_service] = lambda: mock_retrieval

    try:
        response = client.post(
            "/api/v1/query",
            json={"query": "unassigned document notice", "collection_id": "general_synthetic"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION.value
        assert len(data["verified_excerpts"]) == 0
        assert "Zero candidate passages found" in data["summary_narrative"]
    finally:
        app.dependency_overrides.clear()


def test_evidence_isolation_duplicate_doc_id_cross_collection_isolation(client, mock_registry):
    """
    If two collections have chunks with identical document_id (e.g. 'doc_01'),
    a query to Collection-A must only accept chunks declaring ownership of Collection-A.
    """
    mock_retrieval = MagicMock()
    mock_retrieval.search_hybrid.return_value = RetrievalResponse(
        query="operational security guidelines and audit procedures",
        total_results=2,
        results=[
            RetrievalResult(
                chunk_id="col_b_chunk_1",
                document_id="doc_01",
                page_start=1,
                page_end=1,
                text="Collection B doc_01 text operational security guidelines and audit procedures",
                score=1.2,
                rank=1,
                metadata={"collection_id": "amfi_nav"},
            ),
            RetrievalResult(
                chunk_id="col_a_chunk_1",
                document_id="doc_01",
                page_start=1,
                page_end=1,
                text="Collection A doc_01 text operational security guidelines and audit procedures",
                score=1.0,
                rank=2,
                metadata={"collection_id": "general_synthetic"},
            ),
        ],
    )

    mock_gate = MagicMock()
    mock_gate.evaluate_draft.return_value = SafetyGateResponse(
        query="operational security guidelines and audit procedures",
        disposition=AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS,
        verified_excerpts=[
            ValidatedExcerptItem(
                excerpt_id="excerpt_1",
                source_chunk_id="col_a_chunk_1",
                document_id="doc_01",
                page_start=1,
                page_end=1,
                excerpt_text="Collection A doc_01 text operational security guidelines and audit procedures",
                regulatory_status=RegulatoryStatus.ACTIVE_CURRENT,
                status_reason="Active provision",
                collection_id="general_synthetic",
            )
        ],
        historical_superseded_excerpts=[],
        summary_narrative="Verified Excerpts: 1 active excerpt(s)",
        technical_validation_passed=True,
        retrieval_candidate_count=1,
    )

    app.dependency_overrides[get_collection_registry] = lambda: mock_registry
    app.dependency_overrides[get_retrieval_service] = lambda: mock_retrieval
    app.dependency_overrides[get_safety_gate] = lambda: mock_gate

    try:
        response = client.post(
            "/api/v1/query",
            json={"query": "operational security guidelines and audit procedures", "collection_id": "general_synthetic"},
        )
        assert response.status_code == 200

        mock_gate.evaluate_draft.assert_called_once()
        authorized_ids = mock_gate.evaluate_draft.call_args.kwargs["retrieval_candidates"]
        assert "col_a_chunk_1" in authorized_ids
        assert "col_b_chunk_1" not in authorized_ids

        data = response.json()
        assert len(data["verified_excerpts"]) == 1
        assert data["verified_excerpts"][0]["source_chunk_id"] == "col_a_chunk_1"
        assert data["verified_excerpts"][0]["collection_id"] == "general_synthetic"
    finally:
        app.dependency_overrides.clear()


def test_evidence_isolation_empty_evidence_triggers_safe_abstention(client, mock_registry):
    """
    When all retrieved chunks are quarantined as cross-collection or unassigned,
    evidence assembly is empty and safe controlled abstention is returned immediately.
    """
    mock_retrieval = MagicMock()
    mock_retrieval.search_hybrid.return_value = RetrievalResponse(
        query="conflicting foreign chunks",
        total_results=2,
        results=[
            RetrievalResult(
                chunk_id="foreign_1",
                document_id="amfi_nav_doc_001",
                page_start=1,
                page_end=1,
                text="Foreign chunk 1",
                score=1.0,
                rank=1,
                metadata={"collection_id": "amfi_nav"},
            ),
            RetrievalResult(
                chunk_id="foreign_2",
                document_id="unassigned_doc_404",
                page_start=1,
                page_end=1,
                text="Foreign chunk 2",
                score=0.9,
                rank=2,
            ),
        ],
    )

    app.dependency_overrides[get_collection_registry] = lambda: mock_registry
    app.dependency_overrides[get_retrieval_service] = lambda: mock_retrieval

    try:
        response = client.post(
            "/api/v1/query",
            json={"query": "conflicting foreign chunks", "collection_id": "general_synthetic"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION.value
        assert len(data["verified_excerpts"]) == 0
        assert data["technical_validation_passed"] is False
        assert data["synthesis_status"] == "ABSTAINED"
    finally:
        app.dependency_overrides.clear()
