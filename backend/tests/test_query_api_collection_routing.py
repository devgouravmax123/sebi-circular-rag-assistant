"""
Tests for Phase 2.2B.1: Collection-Aware API Routing and Evidence Isolation.

Verifies:
1. Omitted collection_id preserves SEBI pilot routing by default (HTTP 200).
2. A valid collection_id routes retrieval to that collection.
3. Unknown collection_id is rejected with HTTP 404 (not false fallback to SEBI).
4. Disabled collection is rejected with HTTP 400.
5. Collection without a valid published snapshot is rejected with HTTP 400 (never silently falls back to SEBI).
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

    # 1. Active sebi_cscrf collection with published snapshot
    sebi_dir = indexes_dir / "sebi_cscrf"
    sebi_snap = sebi_dir / "snapshots" / "snap_sebi_001"
    sebi_snap.mkdir(parents=True)
    (sebi_dir / "active_snapshot.json").write_text(
        json.dumps({"snapshot_id": "snap_sebi_001", "collection_id": "sebi_cscrf"}),
        encoding="utf-8",
    )

    sebi_manifest = {
        "collection_id": "sebi_cscrf",
        "name": "SEBI CSCRF Pilot",
        "enabled": True,
        "policy": {
            "policy_type": "sebi_cscrf_strict",
            "enforce_regulatory_graph": True,
            "enforce_verbatim_citation": True,
            "enforce_retrieval_binding": True,
        },
    }
    (manifests_dir / "sebi_cscrf.json").write_text(json.dumps(sebi_manifest), encoding="utf-8")

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


def test_routing_omitted_collection_id_preserves_sebi_default(client, mock_registry):
    """Omitted collection_id must default to 'sebi_cscrf' and route to the SEBI pilot index."""
    mock_retrieval = MagicMock()
    mock_retrieval.search_hybrid.return_value = RetrievalResponse(
        query="ISO 27001 certification requirement",
        total_results=1,
        results=[
            RetrievalResult(
                chunk_id="sebi_cscrf_2024_113_p1_0",
                document_id="sebi_cscrf_2024_113",
                page_start=1,
                page_end=1,
                text="ISO 27001 certification requirement guidelines and standards",
                score=1.0,
                rank=1,
            )
        ],
    )

    mock_gate = MagicMock()
    mock_gate.evaluate_draft.return_value = SafetyGateResponse(
        query="ISO 27001 certification requirement",
        disposition=AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS,
        verified_excerpts=[
            ValidatedExcerptItem(
                excerpt_id="excerpt_1",
                source_chunk_id="sebi_cscrf_2024_113_p1_0",
                document_id="sebi_cscrf_2024_113",
                page_start=1,
                page_end=1,
                excerpt_text="ISO 27001 certification requirement guidelines and standards",
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
        # Request omitting collection_id
        response = client.post("/api/v1/query", json={"query": "ISO 27001 certification requirement"})
        assert response.status_code == 200

        # Verify search_hybrid was called with collection_id="sebi_cscrf"
        mock_retrieval.search_hybrid.assert_called_once()
        call_kwargs = mock_retrieval.search_hybrid.call_args.kwargs
        assert call_kwargs.get("collection_id") == "sebi_cscrf"

        data = response.json()
        assert len(data["verified_excerpts"]) == 1
        assert data["verified_excerpts"][0]["collection_id"] == "sebi_cscrf"
    finally:
        app.dependency_overrides.clear()


def test_routing_valid_custom_collection_routes_to_target(client, mock_registry):
    """Explicit valid collection_id routes retrieval directly to that collection."""
    mock_retrieval = MagicMock()
    mock_retrieval.search_hybrid.return_value = RetrievalResponse(
        query="cut-off timing for NAV",
        total_results=1,
        results=[
            RetrievalResult(
                chunk_id="amfi_nav_chunk_001",
                document_id="amfi_nav_doc_001",
                page_start=1,
                page_end=1,
                text="AMFI NAV calculation cut-off timing is 15:00 IST.",
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
                source_chunk_id="amfi_nav_chunk_001",
                document_id="amfi_nav_doc_001",
                page_start=1,
                page_end=1,
                excerpt_text="AMFI NAV calculation cut-off timing is 15:00 IST.",
                regulatory_status=RegulatoryStatus.ACTIVE_CURRENT,
                status_reason="Active provision",
                collection_id="amfi_nav",
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
    """Unknown collection ID is rejected with HTTP 404 (never silently falls back to SEBI)."""
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
    """Collection without a published snapshot is rejected with HTTP 400 (not fallback)."""
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


def test_routing_invalid_collection_id_format_rejected_422(client):
    """collection_id with invalid characters or traversal patterns is rejected by schema validator with 422."""
    for bad_id in ["../traversal", "sebi/cscrf", "col with spaces", "col$special"]:
        response = client.post(
            "/api/v1/query",
            json={"query": "any valid question", "collection_id": bad_id},
        )
        assert response.status_code == 422


def test_evidence_isolation_cross_collection_chunks_quarantined(client, mock_registry):
    """
    A collection_id='sebi_cscrf' request must quarantine and exclude any foreign Collection-B chunks
    that might be returned by retrieval.
    """
    mock_retrieval = MagicMock()
    # Retrieval erroneously returns a candidate from amfi_nav and one from sebi_cscrf
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
                chunk_id="sebi_cscrf_2024_113_p1_0",
                document_id="sebi_cscrf_2024_113",
                page_start=1,
                page_end=1,
                text="Authorized SEBI CSCRF text framework provisions",
                score=1.0,
                rank=2,
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
                source_chunk_id="sebi_cscrf_2024_113_p1_0",
                document_id="sebi_cscrf_2024_113",
                page_start=1,
                page_end=1,
                excerpt_text="Authorized SEBI CSCRF text",
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
            json={"query": "framework provisions", "collection_id": "sebi_cscrf"},
        )
        assert response.status_code == 200

        # Verify that safety gate ONLY received the authorized chunk, NOT the foreign one
        mock_gate.evaluate_draft.assert_called_once()
        draft_payload = mock_gate.evaluate_draft.call_args.kwargs["payload"]
        authorized_ids = mock_gate.evaluate_draft.call_args.kwargs["retrieval_candidates"]

        assert "foreign_amfi_chunk_1" not in authorized_ids
        assert "sebi_cscrf_2024_113_p1_0" in authorized_ids
        assert len(draft_payload.excerpts) == 1
        assert draft_payload.excerpts[0].source_chunk_id == "sebi_cscrf_2024_113_p1_0"
    finally:
        app.dependency_overrides.clear()


def test_evidence_isolation_unassigned_chunks_quarantined_not_defaulted_to_sebi(client, mock_registry):
    """
    Arbitrary document IDs without declared collection_id and outside the 5 legacy SEBI documents
    must be quarantined and NEVER silently defaulted to 'sebi_cscrf'.
    """
    mock_retrieval = MagicMock()
    mock_retrieval.search_hybrid.return_value = RetrievalResponse(
        query="unassigned document notice",
        total_results=1,
        results=[
            RetrievalResult(
                chunk_id="unknown_regulatory_notice_999_p1_0",
                document_id="unknown_regulatory_notice_999",  # Not in 5 legacy docs, no collection_id
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
        # Since all candidates are quarantined, zero valid candidates remain -> Controlled Abstention
        response = client.post(
            "/api/v1/query",
            json={"query": "unassigned document notice", "collection_id": "sebi_cscrf"},
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
    If two collections have chunks with identical document_id (e.g. 'circular_01'),
    a query to Collection-A must only accept chunks declaring ownership of Collection-A.
    """
    mock_retrieval = MagicMock()
    mock_retrieval.search_hybrid.return_value = RetrievalResponse(
        query="circular provisions",
        total_results=2,
        results=[
            RetrievalResult(
                chunk_id="col_b_chunk_1",
                document_id="circular_01",
                page_start=1,
                page_end=1,
                text="Collection B circular_01 text circular provisions",
                score=1.2,
                rank=1,
                metadata={"collection_id": "amfi_nav"},
            ),
            RetrievalResult(
                chunk_id="col_a_chunk_1",
                document_id="circular_01",
                page_start=1,
                page_end=1,
                text="Collection A circular_01 text circular provisions",
                score=1.0,
                rank=2,
                metadata={"collection_id": "sebi_cscrf"},
            ),
        ],
    )

    mock_gate = MagicMock()
    mock_gate.evaluate_draft.return_value = SafetyGateResponse(
        query="circular provisions",
        disposition=AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS,
        verified_excerpts=[
            ValidatedExcerptItem(
                excerpt_id="excerpt_1",
                source_chunk_id="col_a_chunk_1",
                document_id="circular_01",
                page_start=1,
                page_end=1,
                excerpt_text="Collection A circular_01 text",
                regulatory_status=RegulatoryStatus.ACTIVE_CURRENT,
                status_reason="Active provision",
                collection_id="sebi_cscrf",
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
            json={"query": "circular provisions", "collection_id": "sebi_cscrf"},
        )
        assert response.status_code == 200

        mock_gate.evaluate_draft.assert_called_once()
        authorized_ids = mock_gate.evaluate_draft.call_args.kwargs["retrieval_candidates"]
        assert "col_a_chunk_1" in authorized_ids
        assert "col_b_chunk_1" not in authorized_ids

        data = response.json()
        assert len(data["verified_excerpts"]) == 1
        assert data["verified_excerpts"][0]["source_chunk_id"] == "col_a_chunk_1"
        assert data["verified_excerpts"][0]["collection_id"] == "sebi_cscrf"
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
            json={"query": "conflicting foreign chunks", "collection_id": "sebi_cscrf"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION.value
        assert len(data["verified_excerpts"]) == 0
        assert data["technical_validation_passed"] is False
        assert data["synthesis_status"] == "ABSTAINED"
    finally:
        app.dependency_overrides.clear()
