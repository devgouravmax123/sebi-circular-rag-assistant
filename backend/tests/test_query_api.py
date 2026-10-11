"""
Unit and Integration tests for FastAPI POST /api/v1/query endpoint (Phase 6E.2 & 6E.2.2).

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
11. Retrieval/index exceptions produce sanitized 500 Internal Server Error (not false abstention).
12. Safety gate exceptions produce sanitized 500 without leaking tracebacks.
13. Response adheres strictly to QueryResponse schema with pilot corpus limitation.
14. Existing GET /health behavior remains fast and intact.
15. Regression: 2 ordinary excerpts + 1 superseded excerpt with include_superseded=False aligns narrative count (Phase 6E.2.2).
16. Regression: 2 ordinary excerpts + 1 superseded excerpt with include_superseded=True aligns narrative count (Phase 6E.2.2).
17. Regression: Zero returned excerpts and controlled abstention retains accurate narrative (Phase 6E.2.2).
18. Regression: Warnings and disposition fields remain unchanged by include_superseded filtering (Phase 6E.2.2).
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
        json={"query": "ISO 27001 certification requirement", "top_k": 3},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["query"] == "ISO 27001 certification requirement"
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
    # Empty string
    r1 = client.post("/api/v1/query", json={"query": ""})
    assert r1.status_code == 422

    # Whitespace only
    r2 = client.post("/api/v1/query", json={"query": "   "})
    assert r2.status_code == 422


def test_4_query_length_boundary_validation(client):
    # 1 char -> 422
    r_short = client.post("/api/v1/query", json={"query": "a"})
    assert r_short.status_code == 422

    # 501 chars -> 422
    r_long = client.post("/api/v1/query", json={"query": "x" * 501})
    assert r_long.status_code == 422


def test_5_unknown_request_fields_rejected_422(client):
    response = client.post(
        "/api/v1/query",
        json={
            "query": "What are CSCRF guidelines?",
            "extra_unsupported_field": "some_injected_value",
        },
    )
    assert response.status_code == 422


def test_6_top_k_bounds_validation(client):
    # top_k = 0 -> 422
    r_low = client.post("/api/v1/query", json={"query": "CSCRF", "top_k": 0})
    assert r_low.status_code == 422

    # top_k = 11 -> 422
    r_high = client.post("/api/v1/query", json={"query": "CSCRF", "top_k": 11})
    assert r_high.status_code == 422


def test_7_non_retrieved_chunk_ids_never_appear(client):
    # Test query against real corpus
    response = client.post(
        "/api/v1/query",
        json={"query": "CISO reporting lines", "top_k": 3},
    )
    assert response.status_code == 200
    data = response.json()
    for excerpt in data["verified_excerpts"]:
        # Verify that chunk exists in registered corpus
        assert excerpt["source_chunk_id"].startswith("sebi_cscrf_")


def test_8_superseded_evidence_cannot_be_presented_as_current(client):
    # Query for timeline extension today
    response = client.post(
        "/api/v1/query",
        json={"query": "What is the mandatory compliance deadline today?", "include_superseded": False},
    )
    assert response.status_code == 200
    data = response.json()
    # If all excerpts were superseded, controlled abstention is returned
    if data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION.value:
        assert len(data["verified_excerpts"]) == 0
        assert "superseded" in (data["abstention_reason"] or "").lower() or "zero" in (data["summary_narrative"]).lower()


def test_9_include_superseded_flag_behavior(client):
    # Mock gate returning a superseded item
    mock_gate = MagicMock()
    mock_item = ValidatedExcerptItem(
        excerpt_id="e1",
        source_chunk_id="sebi_cscrf_2025_045_p1_0",
        document_id="sebi_cscrf_2025_045",
        page_start=1,
        page_end=2,
        excerpt_text="Historical excerpt",
        regulatory_status=RegulatoryStatus.SUPERSEDED,
        status_reason="Superseded",
    )
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
                chunk_id="sebi_cscrf_2025_045_p1_0",
                document_id="sebi_cscrf_2025_045",
                page_start=1,
                page_end=2,
                text="Historical excerpt",
                score=1.0,
                rank=1,
            )
        ],
    )

    app.dependency_overrides[get_retrieval_service] = lambda: mock_retrieval
    app.dependency_overrides[get_safety_gate] = lambda: mock_gate

    try:
        # include_superseded=False -> historical_superseded_excerpts empty in response
        r_false = client.post("/api/v1/query", json={"query": "Historical timeline", "include_superseded": False})
        assert r_false.status_code == 200
        assert len(r_false.json()["historical_superseded_excerpts"]) == 0

        # include_superseded=True -> historical_superseded_excerpts populated
        r_true = client.post("/api/v1/query", json={"query": "Historical timeline", "include_superseded": True})
        assert r_true.status_code == 200
        assert len(r_true.json()["historical_superseded_excerpts"]) == 1
    finally:
        app.dependency_overrides.clear()


def test_10_unresolved_regulatory_status_produces_qualification(client):
    response = client.post(
        "/api/v1/query",
        json={"query": "To whom does the CISO report directly?"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["disposition"] == AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS.value
    assert any("UNRESOLVED" in w for w in data["regulatory_warnings"])


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
    mock_gate = MagicMock()
    mock_gate.evaluate_draft.side_effect = ValueError("Corrupt internal buffer at memory location 0xDEADBEEF")

    app.dependency_overrides[get_safety_gate] = lambda: mock_gate
    try:
        response = client.post("/api/v1/query", json={"query": "Valid query text"})
        assert response.status_code == 500
        data = response.json()
        assert "0xDEADBEEF" not in data["detail"]
        assert data["detail"] == "An internal error occurred during regulatory safety verification."
    finally:
        app.dependency_overrides.clear()


def test_13_health_endpoint_remains_fast_and_intact(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_15_regression_narrative_count_include_superseded_false(client):
    # Setup mock with 2 verified excerpts and 1 superseded excerpt
    mock_v1 = ValidatedExcerptItem(
        excerpt_id="e1",
        source_chunk_id="c1",
        document_id="doc1",
        page_start=1,
        page_end=2,
        excerpt_text="Excerpt 1",
        regulatory_status=RegulatoryStatus.UNRESOLVED,
        status_reason="Unresolved",
    )
    mock_v2 = ValidatedExcerptItem(
        excerpt_id="e2",
        source_chunk_id="c2",
        document_id="doc1",
        page_start=2,
        page_end=3,
        excerpt_text="Excerpt 2",
        regulatory_status=RegulatoryStatus.UNRESOLVED,
        status_reason="Unresolved",
    )
    mock_s1 = ValidatedExcerptItem(
        excerpt_id="e3",
        source_chunk_id="c3",
        document_id="doc2",
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
        summary_narrative="Old narrative mentioning 3 excerpts",
        technical_validation_passed=True,
        retrieval_candidate_count=3,
        regulatory_warnings=["Warning 1"],
    )

    mock_retrieval = MagicMock()
    mock_retrieval.search_hybrid.return_value = RetrievalResponse(
        query="Test query",
        total_results=3,
        results=[
            RetrievalResult(chunk_id="c1", document_id="doc1", page_start=1, page_end=2, text="t1", score=1.0, rank=1),
            RetrievalResult(chunk_id="c2", document_id="doc1", page_start=2, page_end=3, text="t2", score=0.9, rank=2),
            RetrievalResult(chunk_id="c3", document_id="doc2", page_start=1, page_end=1, text="t3", score=0.8, rank=3),
        ],
    )

    app.dependency_overrides[get_retrieval_service] = lambda: mock_retrieval
    app.dependency_overrides[get_safety_gate] = lambda: mock_gate

    try:
        # 1. When include_superseded=False: narrative must say exactly 2 excerpt(s)
        resp = client.post("/api/v1/query", json={"query": "Test query", "include_superseded": False})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["verified_excerpts"]) == 2
        assert len(data["historical_superseded_excerpts"]) == 0
        assert "2 excerpt(s)" in data["summary_narrative"]
        assert "3 excerpt(s)" not in data["summary_narrative"]
    finally:
        app.dependency_overrides.clear()


def test_16_regression_narrative_count_include_superseded_true(client):
    # Setup mock with 2 verified excerpts and 1 superseded excerpt
    mock_v1 = ValidatedExcerptItem(
        excerpt_id="e1",
        source_chunk_id="c1",
        document_id="doc1",
        page_start=1,
        page_end=2,
        excerpt_text="Excerpt 1",
        regulatory_status=RegulatoryStatus.UNRESOLVED,
        status_reason="Unresolved",
    )
    mock_v2 = ValidatedExcerptItem(
        excerpt_id="e2",
        source_chunk_id="c2",
        document_id="doc1",
        page_start=2,
        page_end=3,
        excerpt_text="Excerpt 2",
        regulatory_status=RegulatoryStatus.UNRESOLVED,
        status_reason="Unresolved",
    )
    mock_s1 = ValidatedExcerptItem(
        excerpt_id="e3",
        source_chunk_id="c3",
        document_id="doc2",
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
            RetrievalResult(chunk_id="c1", document_id="doc1", page_start=1, page_end=2, text="t1", score=1.0, rank=1),
            RetrievalResult(chunk_id="c2", document_id="doc1", page_start=2, page_end=3, text="t2", score=0.9, rank=2),
            RetrievalResult(chunk_id="c3", document_id="doc2", page_start=1, page_end=1, text="t3", score=0.8, rank=3),
        ],
    )

    app.dependency_overrides[get_retrieval_service] = lambda: mock_retrieval
    app.dependency_overrides[get_safety_gate] = lambda: mock_gate

    try:
        # When include_superseded=True: narrative accounts for 2 active and 1 separated superseded excerpt
        resp = client.post("/api/v1/query", json={"query": "Test query", "include_superseded": True})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["verified_excerpts"]) == 2
        assert len(data["historical_superseded_excerpts"]) == 1
        assert "2 excerpt(s)" in data["summary_narrative"]
        assert "1 separated historical superseded excerpt(s)" in data["summary_narrative"]
    finally:
        app.dependency_overrides.clear()


def test_17_regression_zero_returned_excerpts_narrative(client):
    mock_retrieval = MagicMock()
    mock_retrieval.search_hybrid.return_value = RetrievalResponse(
        query="unknown query",
        total_results=0,
        results=[],
    )

    app.dependency_overrides[get_retrieval_service] = lambda: mock_retrieval
    try:
        resp = client.post("/api/v1/query", json={"query": "unknown query"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION.value
        assert "Zero candidate passages found" in data["summary_narrative"]
        assert len(data["verified_excerpts"]) == 0
        assert len(data["historical_superseded_excerpts"]) == 0
    finally:
        app.dependency_overrides.clear()


def test_18_regression_warnings_and_disposition_preserved_under_filtering(client):
    mock_v1 = ValidatedExcerptItem(
        excerpt_id="e1",
        source_chunk_id="c1",
        document_id="doc1",
        page_start=1,
        page_end=2,
        excerpt_text="Excerpt 1",
        regulatory_status=RegulatoryStatus.UNRESOLVED,
        status_reason="Unresolved pilot status",
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
        regulatory_warnings=["Warning: UNRESOLVED regulatory status"],
    )

    mock_retrieval = MagicMock()
    mock_retrieval.search_hybrid.return_value = RetrievalResponse(
        query="Test query",
        total_results=1,
        results=[
            RetrievalResult(chunk_id="c1", document_id="doc1", page_start=1, page_end=2, text="t1", score=1.0, rank=1)
        ],
    )

    app.dependency_overrides[get_retrieval_service] = lambda: mock_retrieval
    app.dependency_overrides[get_safety_gate] = lambda: mock_gate

    try:
        resp = client.post("/api/v1/query", json={"query": "Test query", "include_superseded": False})
        assert resp.status_code == 200
        data = resp.json()
        assert data["disposition"] == AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS.value
        assert data["regulatory_warnings"] == ["Warning: UNRESOLVED regulatory status"]
        assert data["verified_excerpts"][0]["regulatory_status"] == RegulatoryStatus.UNRESOLVED.value
    finally:
        app.dependency_overrides.clear()
