"""
Phase 6.3C: Tests for Collection-Aware Evidence Scope and Controlled Abstention.

Verifies:
Test 1 - Production failure case: SEBI CSCRF requirement asked against cisa_playbooks -> controlled abstention before synthesis.
Test 2 - Valid CISA reporting question -> answered with appropriate CISA excerpt.
Test 3 - Original positive CISA question -> both playbooks answerable.
Test 4 - Cross-collection SEBI question -> normal SEBI strict behavior preserved without false scope mismatch.
Test 5 - General factual question with external organization mention -> answered if candidate evidence supports it.
Test 6 - Ambiguous query without incompatible framework -> no automatic scope abstention.
Test 7 - Provider failure distinction -> genuine synthesis failure produces PROVIDER_FAILED, not mislabeled as ABSTAINED.
"""
import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.api.v1.query import get_synthesis_service
from app.schemas.safety_gate import AnswerDisposition
from app.schemas.synthesis import SynthesisOutput, SynthesisStatus
from app.synthesis.provider import SynthesisMalformedOutputError


from app.synthesis.mock_provider import MockSynthesisProvider
from app.synthesis.service import SynthesisService


@pytest.fixture(autouse=True)
def mock_synthesis_env():
    """Ensure all tests in this suite run against MockSynthesisProvider, preventing external LLM API calls."""
    mock_service = SynthesisService(provider=MockSynthesisProvider())
    app.dependency_overrides[get_synthesis_service] = lambda: mock_service
    yield
    app.dependency_overrides.pop(get_synthesis_service, None)


@pytest.fixture
def client():
    return TestClient(app)


def test_1_sebi_cscrf_against_cisa_playbooks_abstains_before_synthesis(client):
    """
    Test 1 — The production failure case:
    Selected collection: cisa_playbooks
    Query: 'What is the mandatory reporting timeline for cyber incidents under SEBI CSCRF?'
    Expected:
    - Controlled abstention.
    - synthesis_status == 'ABSTAINED'.
    - Clear scope-related abstention reason.
    - No unrelated CISA excerpt presented as support for SEBI.
    - Synthesis provider is NOT invoked.
    """
    mock_synthesis = MagicMock()
    app.dependency_overrides[get_synthesis_service] = lambda: mock_synthesis

    try:
        response = client.post(
            "/api/v1/query",
            json={
                "collection_id": "cisa_playbooks",
                "query": "What is the mandatory reporting timeline for cyber incidents under SEBI CSCRF?",
            },
        )
        assert response.status_code == 200
        data = response.json()

        assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION
        assert data["synthesis_status"] == "ABSTAINED"
        assert len(data["verified_excerpts"]) == 0
        assert len(data["claim_citations"]) == 0
        assert "SEBI" in data["abstention_reason"]
        assert "outside the evidence scope" in data["abstention_reason"]
        assert "Controlled Abstention:" in data["summary_narrative"]

        # Verify synthesis provider was NOT invoked
        assert not mock_synthesis.synthesize_answer.called
    finally:
        app.dependency_overrides.pop(get_synthesis_service, None)


def test_2_valid_cisa_reporting_question_is_answered(client):
    """
    Test 2 — Valid CISA reporting question:
    Selected collection: cisa_playbooks
    Query: 'When should an agency notify CISA after determining an incident?'
    Expected:
    - Relevant CISA evidence is retained.
    - Not rejected by scope safeguard.
    - Answers/cites CISA evidence.
    """
    response = client.post(
        "/api/v1/query",
        json={
            "collection_id": "cisa_playbooks",
            "query": "When should an agency notify CISA after determining an incident?",
        },
    )
    assert response.status_code == 200
    data = response.json()

    assert data["disposition"] != AnswerDisposition.CONTROLLED_ABSTENTION
    assert data["technical_validation_passed"] is True
    assert len(data["verified_excerpts"]) > 0
    assert all(e["collection_id"] == "cisa_playbooks" for e in data["verified_excerpts"])
    assert data["synthesis_status"] in ("FULLY_ANSWERED", "PARTIALLY_ANSWERED")


def test_3_original_positive_cisa_question(client):
    """
    Test 3 — Original positive CISA question:
    Selected collection: cisa_playbooks
    Query: 'What two distinct playbooks are established in the CISA guidance issued under Executive Order 14028?'
    Expected:
    - Both playbooks remain answerable.
    - Correct CISA evidence and citations.
    """
    response = client.post(
        "/api/v1/query",
        json={
            "collection_id": "cisa_playbooks",
            "query": "What two distinct playbooks are established in the CISA guidance issued under Executive Order 14028?",
        },
    )
    assert response.status_code == 200
    data = response.json()

    assert data["disposition"] != AnswerDisposition.CONTROLLED_ABSTENTION
    assert data["synthesis_status"] == "FULLY_ANSWERED"
    assert len(data["verified_excerpts"]) > 0
    assert "incident response" in data["answer_text"].lower()
    assert "vulnerability response" in data["answer_text"].lower()
    assert len(data["claim_citations"]) > 0


def test_4_cross_collection_sebi_question_preserved(client):
    """
    Test 4 — Cross-collection SEBI question:
    Selected collection: sebi_cscrf
    Query: 'What are the ISO 27001 requirements for Qualified REs under CSCRF?'
    Expected:
    - Existing SEBI strict-policy retrieval and validation behavior is preserved.
    - No false scope mismatch.
    """
    response = client.post(
        "/api/v1/query",
        json={
            "collection_id": "sebi_cscrf",
            "query": "What are the ISO 27001 requirements for Qualified REs under CSCRF?",
        },
    )
    assert response.status_code == 200
    data = response.json()

    assert data["disposition"] in (
        AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS,
        AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS,
    )
    assert len(data["verified_excerpts"]) > 0
    assert all(e["collection_id"] == "sebi_cscrf" for e in data["verified_excerpts"])


def test_5_general_factual_question_with_external_org_mention(client):
    """
    Test 5 — General factual question with an external organization mention:
    Selected collection: cisa_playbooks
    Query: 'What guidance does NIST SP 800-92 provide for computer security log management?'
    Expected:
    - NIST SP 800-92 is referenced within CISA Playbooks chunk (Page 7).
    - Do not reject solely because NIST is mentioned.
    - Evidence-based answering allowed when candidate evidence contains it.
    """
    response = client.post(
        "/api/v1/query",
        json={
            "collection_id": "cisa_playbooks",
            "query": "What guidance does NIST SP 800-92 provide for computer security log management?",
        },
    )
    assert response.status_code == 200
    data = response.json()

    # The CISA playbook contains: "NIST SP 800-92: Guide to Computer Security Log Management"
    assert data["disposition"] != AnswerDisposition.CONTROLLED_ABSTENTION
    assert len(data["verified_excerpts"]) > 0
    assert any("nist sp 800-92" in e["excerpt_text"].lower() for e in data["verified_excerpts"])


def test_6_ambiguous_query_no_automatic_scope_abstention(client):
    """
    Test 6 — Ambiguous query:
    Selected collection: cisa_playbooks
    Query: 'What are the essential steps for incident containment?'
    Expected:
    - No automatic scope abstention.
    - Normal relevance and retrieval pipeline processes it.
    """
    response = client.post(
        "/api/v1/query",
        json={
            "collection_id": "cisa_playbooks",
            "query": "What are the essential steps for incident containment?",
        },
    )
    assert response.status_code == 200
    data = response.json()

    assert data["disposition"] != AnswerDisposition.CONTROLLED_ABSTENTION
    assert len(data["verified_excerpts"]) > 0
    assert data["synthesis_status"] in ("FULLY_ANSWERED", "PARTIALLY_ANSWERED")


def test_7_provider_failure_distinction(client):
    """
    Test 7 — Provider failure distinction:
    Verify that a genuine synthesis-provider failure still produces PROVIDER_FAILED
    and is not mislabeled as a controlled abstention.
    """
    mock_synthesis_service = MagicMock()
    mock_synthesis_service.synthesize_answer.return_value = SynthesisOutput(
        status=SynthesisStatus.PROVIDER_FAILED,
        answer="Synthesis error: The synthesis provider returned invalid output.",
        claims=[],
        unresolved_subquestions=[],
        failure_reason="Provider output failed structural validation.",
    )
    app.dependency_overrides[get_synthesis_service] = lambda: mock_synthesis_service

    try:
        response = client.post(
            "/api/v1/query",
            json={
                "collection_id": "cisa_playbooks",
                "query": "What two distinct playbooks are established in the CISA guidance issued under Executive Order 14028?",
            },
        )
        assert response.status_code == 200
        data = response.json()

        assert data["synthesis_status"] == "PROVIDER_FAILED"
        assert data["synthesis_failure_reason"] == "Provider output failed structural validation."
        assert data["disposition"] != AnswerDisposition.CONTROLLED_ABSTENTION
    finally:
        app.dependency_overrides.pop(get_synthesis_service, None)


def test_8_cisa_playbooks_explicitly_requested_against_sebi_collection_abstains(client):
    """
    Phase 6.3E Test: CISA Playbooks explicitly requested against SEBI collection -> controlled abstention.
    Selected collection: sebi_cscrf
    Query: 'What operational playbooks are established by CISA under Executive Order 14028?'
    Expected:
    - Controlled abstention before synthesis.
    - synthesis_status == 'ABSTAINED'.
    - Abstention reason notes CISA Playbooks framework mismatch.
    - Synthesis provider is NOT called.
    """
    mock_synthesis = MagicMock()
    app.dependency_overrides[get_synthesis_service] = lambda: mock_synthesis

    try:
        response = client.post(
            "/api/v1/query",
            json={
                "collection_id": "sebi_cscrf",
                "query": "What operational playbooks are established by CISA under Executive Order 14028?",
            },
        )
        assert response.status_code == 200
        data = response.json()

        assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION
        assert data["synthesis_status"] == "ABSTAINED"
        assert len(data["verified_excerpts"]) == 0
        assert "CISA" in data["abstention_reason"]
        assert "outside the evidence scope" in data["abstention_reason"]
        assert not mock_synthesis.synthesize_answer.called
    finally:
        app.dependency_overrides.pop(get_synthesis_service, None)


def test_9_incidental_mention_in_excerpt_does_not_bypass_genuine_mismatch(client):
    """
    Phase 6.3E Test: An excerpt containing an incidental acronym mention (e.g. CISA certification
    in SEBI audit norms chunk p150_118) must not automatically bypass a genuine framework mismatch.
    Query: 'What are the CISA playbook requirements for incident handling?'
    Target collection: sebi_cscrf
    Expected:
    - Controlled abstention.
    - No incidental bypass.
    """
    response = client.post(
        "/api/v1/query",
        json={
            "collection_id": "sebi_cscrf",
            "query": "What are the CISA playbook requirements for incident handling?",
        },
    )
    assert response.status_code == 200
    data = response.json()

    assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION
    assert data["synthesis_status"] == "ABSTAINED"
    assert len(data["verified_excerpts"]) == 0
    assert "CISA" in data["abstention_reason"]
