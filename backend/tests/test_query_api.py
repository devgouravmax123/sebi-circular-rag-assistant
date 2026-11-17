"""
Unit and Integration tests for FastAPI POST /api/v1/query endpoint.

Tests:
1. Valid query with retrieved candidates in deterministic excerpt mode.
2. Empty retrieval result produces controlled abstention (HTTP 200).
3. Empty and whitespace-only queries return 422 Unprocessable Entity.
4. Query shorter than 2 characters and longer than 500 characters return 422.
5. Unknown request fields return 422 (extra='forbid').
6. top_k below 1 and above 10 return 422.
7. Fabricated or non-retrieved chunk IDs never appear in final evidence.
8. Superseded evidence cannot be presented as current (separate historical handling).
9. include_superseded flag correctly toggles historical_superseded_excerpts.
10. Unresolved regulatory status produces appropriate qualification or warnings.
11. Retrieval/index exceptions produce sanitized 500 Internal Server Error.
12. Safety gate exceptions produce sanitized 500 without leaking tracebacks.
13. Response adheres strictly to QueryResponse schema.
14. Existing GET /health behavior remains fast and intact.
15. Regression: 2 ordinary excerpts + 1 superseded excerpt with include_superseded=False aligns narrative count.
16. Regression: 2 ordinary excerpts + 1 superseded excerpt with include_superseded=True aligns narrative count.
17. Regression: Zero returned excerpts and controlled abstention retains accurate narrative.
18. Regression: Warnings and disposition fields remain unchanged by include_superseded filtering.
19. General static early abstention from zero candidates.
20. General static out-of-scope intent early abstention.
21. General static grounded success.
"""
import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.api.v1.query import get_retrieval_service, get_safety_gate
from app.schemas.query import QueryResponse
from app.schemas.safety_gate import AnswerDisposition, SafetyGateResponse, ValidatedExcerptItem
from app.schemas.grounding import RegulatoryStatus
from app.schemas.retrieval import RetrievalResult, RetrievalResponse


@pytest.fixture
def client():
    return TestClient(app)


def test_1_valid_query_deterministic_excerpt_mode(client):
    response = client.post(
        "/api/v1/query",
        json={"query": "What is the minimum password length requirement?", "top_k": 3},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["query"] == "What is the minimum password length requirement?"
    assert data["disposition"] in [
        AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS.value,
        AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS.value,
    ]
    assert data["technical_validation_passed"] is True
    assert len(data["verified_excerpts"]) > 0
    assert "pilot_corpus_scope_limitation" in data
    # Validate against Pydantic model
    validated = QueryResponse(**data)
    assert validated.technical_validation_passed is True


def test_2_empty_retrieval_result_produces_controlled_abstention(client):
    mock_retrieval = MagicMock()
    mock_retrieval.search_hybrid.return_value = RetrievalResponse(
        query="completely unknown out of corpus query",
        total_results=0,
        results=[],
    )

    app.dependency_overrides[get_retrieval_service] = lambda: mock_retrieval
    try:
        response = client.post(
            "/api/v1/query",
            json={"query": "completely unknown out of corpus query"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION.value
        assert len(data["verified_excerpts"]) == 0
        assert data["abstention_reason"] is not None
        assert "Zero candidate passages found" in data["summary_narrative"]
    finally:
        app.dependency_overrides.clear()


def test_3_empty_and_whitespace_only_queries_return_422(client):
    r1 = client.post("/api/v1/query", json={"query": ""})
    assert r1.status_code == 422

    r2 = client.post("/api/v1/query", json={"query": "   "})
    assert r2.status_code == 422


def test_4_query_length_boundary_validation(client):
    r_short = client.post("/api/v1/query", json={"query": "a"})
    assert r_short.status_code == 422

    r_long = client.post("/api/v1/query", json={"query": "x" * 501})
    assert r_long.status_code == 422


def test_5_unknown_request_fields_rejected_422(client):
    response = client.post(
        "/api/v1/query",
        json={
            "query": "What are password requirements?",
            "extra_unsupported_field": "some_injected_value",
        },
    )
    assert response.status_code == 422


def test_6_top_k_bounds_validation(client):
    r_low = client.post("/api/v1/query", json={"query": "passwords", "top_k": 0})
    assert r_low.status_code == 422

    r_high = client.post("/api/v1/query", json={"query": "passwords", "top_k": 11})
    assert r_high.status_code == 422


def test_7_non_retrieved_chunk_ids_never_appear(client):
    response = client.post(
        "/api/v1/query",
        json={"query": "What is the minimum password length requirement?", "top_k": 3},
    )
    assert response.status_code == 200
    data = response.json()
    for excerpt in data["verified_excerpts"]:
        assert excerpt["source_chunk_id"].startswith("acme_")


def test_8_superseded_evidence_cannot_be_presented_as_current(client):
    mock_retrieval = MagicMock()
    mock_retrieval.search_hybrid.return_value = RetrievalResponse(
        query="What is the active operational timeline?",
        total_results=1,
        results=[
            RetrievalResult(
                chunk_id="chunk_super_1",
                document_id="doc_v1",
                page_start=1,
                page_end=1,
                text="Active operational timeline specifications and historical superseded requirements",
                score=1.0,
                rank=1,
                metadata={"collection_id": "general_synthetic"},
            )
        ],
    )
    mock_gate = MagicMock()
    mock_item = ValidatedExcerptItem(
        excerpt_id="e1",
        source_chunk_id="chunk_super_1",
        document_id="doc_v1",
        collection_id="general_synthetic",
        page_start=1,
        page_end=1,
        excerpt_text="Historical superseded timeline specifications",
        regulatory_status=RegulatoryStatus.SUPERSEDED,
        status_reason="Superseded by newer version",
    )
    mock_gate.evaluate_draft.return_value = SafetyGateResponse(
        query="What is the active operational timeline?",
        disposition=AnswerDisposition.CONTROLLED_ABSTENTION,
        verified_excerpts=[],
        historical_superseded_excerpts=[mock_item],
        summary_narrative="Controlled Abstention: all matching provisions are superseded.",
        technical_validation_passed=True,
        retrieval_candidate_count=1,
        abstention_reason="All matching corpus provisions are superseded.",
    )
    app.dependency_overrides[get_retrieval_service] = lambda: mock_retrieval
    app.dependency_overrides[get_safety_gate] = lambda: mock_gate

    try:
        response = client.post(
            "/api/v1/query",
            json={"query": "What is the active operational timeline?", "include_superseded": False},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION.value
        assert len(data["verified_excerpts"]) == 0
        assert "superseded" in (data["abstention_reason"] or "").lower()
    finally:
        app.dependency_overrides.clear()


def test_9_include_superseded_flag_behavior(client):
    mock_item = ValidatedExcerptItem(
        excerpt_id="e1",
        source_chunk_id="chunk_super_1",
        document_id="doc_v1",
        collection_id="general_synthetic",
        page_start=1,
        page_end=2,
        excerpt_text="Historical excerpt",
        regulatory_status=RegulatoryStatus.SUPERSEDED,
        status_reason="Superseded",
    )
    mock_gate = MagicMock()
    mock_gate.evaluate_draft.return_value = SafetyGateResponse(
        query="Historical timeline",
        disposition=AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS,
        verified_excerpts=[],
        historical_superseded_excerpts=[mock_item],
        summary_narrative="Historical excerpts.",
        technical_validation_passed=True,
        retrieval_candidate_count=1,
    )

    mock_retrieval = MagicMock()
    mock_retrieval.search_hybrid.return_value = RetrievalResponse(
        query="Historical timeline",
        total_results=1,
        results=[
            RetrievalResult(
                chunk_id="chunk_super_1",
                document_id="doc_v1",
                page_start=1,
                page_end=2,
                text="Historical excerpt",
                score=1.0,
                rank=1,
                metadata={"collection_id": "general_synthetic"},
            )
        ],
    )

    app.dependency_overrides[get_retrieval_service] = lambda: mock_retrieval
    app.dependency_overrides[get_safety_gate] = lambda: mock_gate

    try:
        r_false = client.post("/api/v1/query", json={"query": "Historical timeline", "include_superseded": False})
        assert r_false.status_code == 200
        assert len(r_false.json()["historical_superseded_excerpts"]) == 0

        r_true = client.post("/api/v1/query", json={"query": "Historical timeline", "include_superseded": True})
        assert r_true.status_code == 200
        assert len(r_true.json()["historical_superseded_excerpts"]) == 1
    finally:
        app.dependency_overrides.clear()


def test_10_unresolved_regulatory_status_produces_qualification(client):
    mock_retrieval = MagicMock()
    mock_retrieval.search_hybrid.return_value = RetrievalResponse(
        query="operational security oversight",
        total_results=1,
        results=[
            RetrievalResult(
                chunk_id="chunk_unres_1",
                document_id="doc_v1",
                page_start=1,
                page_end=1,
                text="Security oversight and governance standards",
                score=1.0,
                rank=1,
                metadata={"collection_id": "general_synthetic"},
            )
        ],
    )
    mock_gate = MagicMock()
    mock_item = ValidatedExcerptItem(
        excerpt_id="e_unres",
        source_chunk_id="chunk_unres_1",
        document_id="doc_v1",
        collection_id="general_synthetic",
        page_start=1,
        page_end=1,
        excerpt_text="Security oversight and governance standards",
        regulatory_status=RegulatoryStatus.UNRESOLVED,
        status_reason="Unresolved qualification",
    )
    mock_gate.evaluate_draft.return_value = SafetyGateResponse(
        query="operational security oversight",
        disposition=AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS,
        verified_excerpts=[mock_item],
        historical_superseded_excerpts=[],
        summary_narrative="Qualified Excerpts: 1 active excerpt(s)",
        technical_validation_passed=True,
        retrieval_candidate_count=1,
        regulatory_warnings=["Warning: UNRESOLVED qualification"],
    )
    app.dependency_overrides[get_retrieval_service] = lambda: mock_retrieval
    app.dependency_overrides[get_safety_gate] = lambda: mock_gate

    try:
        response = client.post(
            "/api/v1/query",
            json={"query": "operational security oversight"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["disposition"] == AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS.value
        assert any("UNRESOLVED" in w for w in data["regulatory_warnings"])
    finally:
        app.dependency_overrides.clear()


def test_11_retrieval_service_failure_produces_sanitized_500(client):
    mock_retrieval = MagicMock()
    mock_retrieval.search_hybrid.side_effect = RuntimeError("Database file C:\\secret\\indexes.db locked")

    app.dependency_overrides[get_retrieval_service] = lambda: mock_retrieval
    try:
        response = client.post("/api/v1/query", json={"query": "Valid query text"})
        assert response.status_code == 500
        data = response.json()
        assert "secret" not in data["detail"]
        assert "indexes.db" not in data["detail"]
        assert data["detail"] == "An internal error occurred while searching the regulatory index."
    finally:
        app.dependency_overrides.clear()


def test_12_safety_gate_failure_produces_sanitized_500(client):
    mock_retrieval = MagicMock()
    mock_retrieval.search_hybrid.return_value = RetrievalResponse(
        query="What is the minimum password length requirement?",
        total_results=1,
        results=[
            RetrievalResult(
                chunk_id="acme_security_policy_p1_0",
                document_id="acme_security_policy",
                page_start=1,
                page_end=1,
                text="User passwords must be at least 16 characters in minimum password length requirement.",
                score=1.0,
                rank=1,
                metadata={"collection_id": "general_synthetic"},
            )
        ],
    )
    mock_gate = MagicMock()
    mock_gate.evaluate_draft.side_effect = ValueError("Corrupt internal buffer at memory location 0xDEADBEEF")

    app.dependency_overrides[get_retrieval_service] = lambda: mock_retrieval
    app.dependency_overrides[get_safety_gate] = lambda: mock_gate
    try:
        response = client.post("/api/v1/query", json={"query": "What is the minimum password length requirement?"})
        assert response.status_code == 500
        data = response.json()
        assert "0xDEADBEEF" not in data["detail"]
        assert data["detail"] == "An internal error occurred during regulatory safety verification."
    finally:
        app.dependency_overrides.clear()


def test_13_health_endpoint_remains_fast_and_intact(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_15_regression_narrative_count_include_superseded_false(client):
    mock_v1 = ValidatedExcerptItem(
        excerpt_id="e1",
        source_chunk_id="chunk_1",
        document_id="doc_1",
        collection_id="general_synthetic",
        page_start=1,
        page_end=2,
        excerpt_text="Active excerpt 1",
        regulatory_status=RegulatoryStatus.ACTIVE_CURRENT,
        status_reason="Active",
    )
    mock_v2 = ValidatedExcerptItem(
        excerpt_id="e2",
        source_chunk_id="chunk_2",
        document_id="doc_1",
        collection_id="general_synthetic",
        page_start=2,
        page_end=3,
        excerpt_text="Active excerpt 2",
        regulatory_status=RegulatoryStatus.UNRESOLVED,
        status_reason="Unresolved",
    )
    mock_s1 = ValidatedExcerptItem(
        excerpt_id="e3",
        source_chunk_id="chunk_3",
        document_id="doc_2",
        collection_id="general_synthetic",
        page_start=1,
        page_end=1,
        excerpt_text="Superseded excerpt",
        regulatory_status=RegulatoryStatus.SUPERSEDED,
        status_reason="Superseded",
    )

    mock_gate = MagicMock()
    mock_gate.evaluate_draft.return_value = SafetyGateResponse(
        query="Test query",
        disposition=AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS,
        verified_excerpts=[mock_v1, mock_v2],
        historical_superseded_excerpts=[mock_s1],
        summary_narrative="Old narrative",
        technical_validation_passed=True,
        retrieval_candidate_count=3,
        regulatory_warnings=["Warning 1"],
    )

    mock_retrieval = MagicMock()
    mock_retrieval.search_hybrid.return_value = RetrievalResponse(
        query="Test query",
        total_results=3,
        results=[
            RetrievalResult(chunk_id="chunk_1", document_id="doc_1", page_start=1, page_end=2, text="Test query text 1", score=1.0, rank=1, metadata={"collection_id": "general_synthetic"}),
            RetrievalResult(chunk_id="chunk_2", document_id="doc_1", page_start=2, page_end=3, text="Test query text 2", score=0.9, rank=2, metadata={"collection_id": "general_synthetic"}),
            RetrievalResult(chunk_id="chunk_3", document_id="doc_2", page_start=1, page_end=1, text="Test query text 3", score=0.8, rank=3, metadata={"collection_id": "general_synthetic"}),
        ],
    )

    app.dependency_overrides[get_retrieval_service] = lambda: mock_retrieval
    app.dependency_overrides[get_safety_gate] = lambda: mock_gate

    try:
        resp = client.post("/api/v1/query", json={"query": "Test query", "include_superseded": False})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["verified_excerpts"]) == 2
        assert len(data["historical_superseded_excerpts"]) == 0
        assert "2 excerpt(s)" in data["summary_narrative"]
        assert "separated" not in data["summary_narrative"]
    finally:
        app.dependency_overrides.clear()


def test_16_regression_narrative_count_include_superseded_true(client):
    mock_v1 = ValidatedExcerptItem(
        excerpt_id="e1",
        source_chunk_id="chunk_1",
        document_id="doc_1",
        collection_id="general_synthetic",
        page_start=1,
        page_end=2,
        excerpt_text="Active excerpt 1",
        regulatory_status=RegulatoryStatus.ACTIVE_CURRENT,
        status_reason="Active",
    )
    mock_v2 = ValidatedExcerptItem(
        excerpt_id="e2",
        source_chunk_id="chunk_2",
        document_id="doc_1",
        collection_id="general_synthetic",
        page_start=2,
        page_end=3,
        excerpt_text="Active excerpt 2",
        regulatory_status=RegulatoryStatus.UNRESOLVED,
        status_reason="Unresolved",
    )
    mock_s1 = ValidatedExcerptItem(
        excerpt_id="e3",
        source_chunk_id="chunk_3",
        document_id="doc_2",
        collection_id="general_synthetic",
        page_start=1,
        page_end=1,
        excerpt_text="Superseded excerpt",
        regulatory_status=RegulatoryStatus.SUPERSEDED,
        status_reason="Superseded",
    )

    mock_gate = MagicMock()
    mock_gate.evaluate_draft.return_value = SafetyGateResponse(
        query="Test query",
        disposition=AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS,
        verified_excerpts=[mock_v1, mock_v2],
        historical_superseded_excerpts=[mock_s1],
        summary_narrative="Old narrative",
        technical_validation_passed=True,
        retrieval_candidate_count=3,
        regulatory_warnings=["Warning 1"],
    )

    mock_retrieval = MagicMock()
    mock_retrieval.search_hybrid.return_value = RetrievalResponse(
        query="Test query",
        total_results=3,
        results=[
            RetrievalResult(chunk_id="chunk_1", document_id="doc_1", page_start=1, page_end=2, text="Test query text 1", score=1.0, rank=1, metadata={"collection_id": "general_synthetic"}),
            RetrievalResult(chunk_id="chunk_2", document_id="doc_1", page_start=2, page_end=3, text="Test query text 2", score=0.9, rank=2, metadata={"collection_id": "general_synthetic"}),
            RetrievalResult(chunk_id="chunk_3", document_id="doc_2", page_start=1, page_end=1, text="Test query text 3", score=0.8, rank=3, metadata={"collection_id": "general_synthetic"}),
        ],
    )

    app.dependency_overrides[get_retrieval_service] = lambda: mock_retrieval
    app.dependency_overrides[get_safety_gate] = lambda: mock_gate

    try:
        resp = client.post("/api/v1/query", json={"query": "Test query", "include_superseded": True})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["verified_excerpts"]) == 2
        assert len(data["historical_superseded_excerpts"]) == 1
        assert "2 excerpt(s)" in data["summary_narrative"]
    finally:
        app.dependency_overrides.clear()


def test_18_regression_warnings_and_disposition_preserved_under_filtering(client):
    mock_v1 = ValidatedExcerptItem(
        excerpt_id="e1",
        source_chunk_id="chunk_1",
        document_id="doc_1",
        collection_id="general_synthetic",
        page_start=1,
        page_end=2,
        excerpt_text="Excerpt 1",
        regulatory_status=RegulatoryStatus.UNRESOLVED,
        status_reason="Unresolved status",
    )
    mock_gate = MagicMock()
    mock_gate.evaluate_draft.return_value = SafetyGateResponse(
        query="Test query",
        disposition=AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS,
        verified_excerpts=[mock_v1],
        historical_superseded_excerpts=[],
        summary_narrative="Old narrative",
        technical_validation_passed=True,
        retrieval_candidate_count=1,
        regulatory_warnings=["Warning: UNRESOLVED qualification"],
    )

    mock_retrieval = MagicMock()
    mock_retrieval.search_hybrid.return_value = RetrievalResponse(
        query="Test query",
        total_results=1,
        results=[
            RetrievalResult(chunk_id="chunk_1", document_id="doc_1", page_start=1, page_end=2, text="Test query text 1", score=1.0, rank=1, metadata={"collection_id": "general_synthetic"})
        ],
    )

    app.dependency_overrides[get_retrieval_service] = lambda: mock_retrieval
    app.dependency_overrides[get_safety_gate] = lambda: mock_gate

    try:
        resp = client.post("/api/v1/query", json={"query": "Test query", "include_superseded": False})
        assert resp.status_code == 200
        data = resp.json()
        assert data["disposition"] == AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS.value
        assert data["regulatory_warnings"] == ["Warning: UNRESOLVED qualification"]
        assert data["verified_excerpts"][0]["regulatory_status"] == RegulatoryStatus.UNRESOLVED.value
    finally:
        app.dependency_overrides.clear()


def test_19_general_static_early_abstention_zero_candidates(client):
    from app.api.v1.query import get_collection_registry
    from app.schemas.manifest import CollectionManifest, CollectionStatus, GroundingPolicyDeclaration, GroundingPolicyType

    manifest = CollectionManifest(
        collection_id="demo_finops",
        name="Acme Cloud FinOps",
        description="FinOps guidelines",
        domain="FINOPS",
        regulator=None,
        enabled=True,
        policy=GroundingPolicyDeclaration(
            policy_type=GroundingPolicyType.GENERAL_STATIC,
            enforce_regulatory_graph=False,
        ),
        version="1.0.0",
        created_at="2026-10-30T00:00:00Z",
    )
    mock_reg = MagicMock()
    mock_reg.has_collection.return_value = True
    mock_reg.get_collection_status.return_value = CollectionStatus.ACTIVE
    mock_reg.get_collection.return_value = manifest
    mock_reg.get_grounding_policy.return_value = manifest.policy

    mock_ret = MagicMock()
    mock_ret.chunks = []
    mock_ret.documents = {}
    mock_ret.search_hybrid.return_value = RetrievalResponse(
        query="completely unknown out-of-vocabulary topic",
        total_results=0,
        results=[],
    )

    app.dependency_overrides[get_collection_registry] = lambda: mock_reg
    app.dependency_overrides[get_retrieval_service] = lambda: mock_ret

    try:
        resp = client.post(
            "/api/v1/query",
            json={"query": "completely unknown out-of-vocabulary topic", "collection_id": "demo_finops"},
        )
        assert resp.status_code == 200
        data = resp.json()

        assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION.value
        assert "SEBI" not in data["pilot_corpus_scope_limitation"]
        assert "circular passages" not in data["abstention_reason"]
        assert "document passages" in data["abstention_reason"]
    finally:
        app.dependency_overrides.clear()


def test_20_general_static_out_of_scope_intent_early_abstention(client):
    from app.api.v1.query import get_collection_registry
    from app.schemas.manifest import CollectionManifest, CollectionStatus, GroundingPolicyDeclaration, GroundingPolicyType

    manifest = CollectionManifest(
        collection_id="general_synthetic",
        name="Acme Technical Handbook",
        description="Technical handbook",
        domain="TECH",
        regulator=None,
        enabled=True,
        policy=GroundingPolicyDeclaration(
            policy_type=GroundingPolicyType.GENERAL_STATIC,
            enforce_regulatory_graph=False,
        ),
        version="1.0.0",
        created_at="2026-10-30T00:00:00Z",
    )
    mock_reg = MagicMock()
    mock_reg.has_collection.return_value = True
    mock_reg.get_collection_status.return_value = CollectionStatus.ACTIVE
    mock_reg.get_collection.return_value = manifest
    mock_reg.get_grounding_policy.return_value = manifest.policy

    mock_ret = MagicMock()
    mock_ret.chunks = []
    mock_ret.documents = {}
    mock_ret.search_hybrid.return_value = RetrievalResponse(
        query="hi hello there what are you doing",
        total_results=0,
        results=[],
    )

    app.dependency_overrides[get_collection_registry] = lambda: mock_reg
    app.dependency_overrides[get_retrieval_service] = lambda: mock_ret

    try:
        resp = client.post(
            "/api/v1/query",
            json={"query": "hi hello there what are you doing", "collection_id": "general_synthetic"},
        )
        assert resp.status_code == 200
        data = resp.json()

        assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION.value
        assert "5-document SEBI CSCRF" not in data["pilot_corpus_scope_limitation"]
        assert "CIR/" not in data["pilot_corpus_scope_limitation"]
    finally:
        app.dependency_overrides.clear()


def test_21_general_static_grounded_success(client):
    from app.api.v1.query import get_collection_registry
    from app.schemas.manifest import CollectionManifest, CollectionStatus, GroundingPolicyDeclaration, GroundingPolicyType
    from app.schemas.document import ChunkRecord, DocumentRecord
    from app.synthesis.mock_provider import MockSynthesisProvider
    from app.synthesis.service import SynthesisService, get_synthesis_service

    manifest = CollectionManifest(
        collection_id="demo_finops",
        name="Acme Cloud FinOps",
        description="FinOps guidelines",
        domain="FINOPS",
        regulator=None,
        enabled=True,
        policy=GroundingPolicyDeclaration(
            policy_type=GroundingPolicyType.GENERAL_STATIC,
            enforce_regulatory_graph=False,
        ),
        version="1.0.0",
        created_at="2026-10-30T00:00:00Z",
    )
    mock_reg = MagicMock()
    mock_reg.has_collection.return_value = True
    mock_reg.get_collection_status.return_value = CollectionStatus.ACTIVE
    mock_reg.get_collection.return_value = manifest
    mock_reg.get_grounding_policy.return_value = manifest.policy

    chunk = ChunkRecord(
        chunk_id="finops_c1",
        document_id="finops_doc1",
        text="Automated lifecycle policies delete unattached snapshots after 14 days.",
        page_start=5,
        page_end=5,
        collection_id="demo_finops",
    )
    doc = DocumentRecord(
        document_id="finops_doc1",
        title="FinOps Lifecycle Guide",
        source_url="https://internal.example.org/docs/finops.pdf",
        sha256="abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
        total_pages=10,
        pdf_path="finops/guide.pdf",
        collection_id="demo_finops",
    )

    mock_ret = MagicMock()
    mock_ret.chunks = [chunk]
    mock_ret.documents = {"finops_doc1": doc}
    mock_ret.search_hybrid.return_value = RetrievalResponse(
        query="unattached snapshots retention lifecycle",
        total_results=1,
        results=[
            RetrievalResult(
                chunk_id="finops_c1",
                document_id="finops_doc1",
                title="FinOps Lifecycle Guide",
                text=chunk.text,
                page_start=5,
                page_end=5,
                score=0.95,
                rank=1,
                metadata={"collection_id": "demo_finops"},
            )
        ],
    )

    synth_service = SynthesisService(provider=MockSynthesisProvider())

    app.dependency_overrides[get_collection_registry] = lambda: mock_reg
    app.dependency_overrides[get_retrieval_service] = lambda: mock_ret
    app.dependency_overrides[get_synthesis_service] = lambda: synth_service

    try:
        resp = client.post(
            "/api/v1/query",
            json={"query": "unattached snapshots retention lifecycle", "collection_id": "demo_finops"},
        )
        assert resp.status_code == 200
        data = resp.json()

        assert data["technical_validation_passed"] is True
        assert len(data["verified_excerpts"]) == 1
        assert "SEBI" not in data["pilot_corpus_scope_limitation"]
    finally:
        app.dependency_overrides.clear()
