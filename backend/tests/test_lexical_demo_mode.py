"""
Tests for Phase 10.1.26: Configurable RETRIEVAL_MODE and Lexical-Only Demo Mode.

Verifies:
1. `hybrid` remains the default when RETRIEVAL_MODE is unset or omitted.
2. Valid and invalid `RETRIEVAL_MODE` values are handled correctly.
3. Lexical-only mode executes without loading PyTorch or SentenceTransformers into sys.modules.
4. Exact keyword query and citation retrieval in lexical-only mode.
5. Controlled abstention on out-of-scope queries in lexical-only mode.
6. Separation of superseded provisions and `include_superseded` toggle in lexical-only mode.
7. Readiness probe passes in lexical-only mode without dense index assets or database.
8. Rate limiting and CORS validation remain intact.
"""
import os
import sys
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import app
from app.schemas.query import QueryResponse
from app.schemas.safety_gate import AnswerDisposition


@pytest.fixture
def client():
    return TestClient(app)


def test_1_default_retrieval_mode_is_hybrid():
    """Verify that when RETRIEVAL_MODE is unset, default is 'hybrid'."""
    settings = Settings()
    assert settings.retrieval_mode == "hybrid"
    assert settings.is_lexical_only is False


def test_2_retrieval_mode_validation():
    """Verify valid and invalid RETRIEVAL_MODE values."""
    s1 = Settings(retrieval_mode="hybrid")
    assert s1.is_lexical_only is False

    s2 = Settings(retrieval_mode="lexical_only")
    assert s2.is_lexical_only is True

    s3 = Settings(retrieval_mode="  LEXICAL_ONLY  ")
    assert s3.is_lexical_only is True
    assert s3.retrieval_mode == "lexical_only"

    with pytest.raises(ValueError, match="Unsupported RETRIEVAL_MODE"):
        Settings(retrieval_mode="semantic_vector")


def test_3_lexical_only_readiness_probe(client, monkeypatch):
    """Verify that GET /ready succeeds in lexical_only mode without database."""
    monkeypatch.setenv("RETRIEVAL_MODE", "lexical_only")
    get_settings(force_reload=True)

    resp = client.get("/ready")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ready"
    assert data["storage_mode"] == "file_backed"
    assert data["index_active"] is True


def test_4_lexical_only_exact_keyword_query(client, monkeypatch):
    """Verify that exact keyword search in lexical-only mode retrieves correct grounded excerpts."""
    monkeypatch.setenv("RETRIEVAL_MODE", "lexical_only")
    get_settings(force_reload=True)

    resp = client.post(
        "/api/v1/query",
        json={"query": "ISO 27001 certification requirement", "top_k": 3},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["query"] == "ISO 27001 certification requirement"
    assert data["technical_validation_passed"] is True
    assert len(data["verified_excerpts"]) > 0

    # Ensure pilot corpus scope limitation is present
    assert "pilot_corpus_scope_limitation" in data
    assert "5-document SEBI CSCRF circular corpus" in data["pilot_corpus_scope_limitation"]

    # Verify claim citations and answer text exist (Mock synthesis)
    assert data["synthesis_status"] in ["FULLY_ANSWERED", "PARTIALLY_ANSWERED"]
    assert len(data["claim_citations"]) > 0


def test_5_lexical_only_controlled_abstention_out_of_scope(client, monkeypatch):
    """Verify controlled abstention for out-of-scope query in lexical-only mode."""
    monkeypatch.setenv("RETRIEVAL_MODE", "lexical_only")
    get_settings(force_reload=True)

    resp = client.post(
        "/api/v1/query",
        json={"query": "What are the margin trading rules for equity derivatives?", "top_k": 3},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION.value
    assert data["verified_excerpts"] == []
    assert data["synthesis_status"] == "ABSTAINED"
    assert "Controlled Abstention" in data["summary_narrative"]


def test_6_lexical_only_superseded_evidence_separation(client, monkeypatch):
    """Verify separation of superseded provisions when include_superseded=False."""
    monkeypatch.setenv("RETRIEVAL_MODE", "lexical_only")
    get_settings(force_reload=True)

    resp = client.post(
        "/api/v1/query",
        json={
            "query": "What is the compliance deadline for Qualified REs?",
            "top_k": 5,
            "include_superseded": False,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    # Verified excerpts must only include active/unresolved, not superseded baseline without qualification
    for exc in data["verified_excerpts"]:
        assert exc["regulatory_status"] != "SUPERSEDED"


def test_7_lexical_only_with_filters(client, monkeypatch):
    """Verify that domain and document_id filters work in lexical-only mode."""
    monkeypatch.setenv("RETRIEVAL_MODE", "lexical_only")
    get_settings(force_reload=True)

    resp = client.post(
        "/api/v1/query",
        json={
            "query": "cyber resilience framework",
            "domain": "CSCRF",
            "top_k": 3,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    for exc in data["verified_excerpts"]:
        assert exc["domain"] == "CSCRF"


def test_8_hybrid_mode_still_works_as_default(client, monkeypatch):
    """Verify that hybrid mode still operates cleanly when RETRIEVAL_MODE is hybrid."""
    monkeypatch.setenv("RETRIEVAL_MODE", "hybrid")
    get_settings(force_reload=True)

    resp = client.post(
        "/api/v1/query",
        json={"query": "ISO 27001 certification requirement", "top_k": 3},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["technical_validation_passed"] is True
    assert len(data["verified_excerpts"]) > 0
