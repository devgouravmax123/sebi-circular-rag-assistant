"""
End-to-End Response Contract Verification Test Suite (Phase 8F.3.1).

Verifies response semantics and contract enforcement across the entire query
pipeline through final REST API response serialization.

Checks:
1. val_unans_05 (NIST SP 800-53 Rev 5 controls) -> CONTROLLED_ABSTENTION / ABSTAINED
2. val_unans_08 (Generic risk controls across financial systems) -> CONTROLLED_ABSTENTION / ABSTAINED
3. val_unans_10 (Cyber insurance sum insured in Rupees) -> CONTROLLED_ABSTENTION / ABSTAINED
4. Genuine CSCRF question (e.g. CISO reporting) -> ACCEPTED / FULLY_ANSWERED with valid citations
5. Legitimate non-verbatim paraphrase (e.g. air-gapped backups) -> ACCEPTED / FULLY_ANSWERED
6. Multi-part question with supported + unsupported subparts -> PARTIALLY_ANSWERED with UnresolvedSubquestion
7. Absent external regulatory framework (e.g. DORA TLPT) -> CONTROLLED_ABSTENTION / ABSTAINED
8. Unsupported monetary/insurance amount inquiry -> CONTROLLED_ABSTENTION / ABSTAINED
9. Provider timeout fallback -> PROVIDER_FAILED, safe narrative, no affirmative claims
10. Unsupported claim rejected by verifier -> UNSUPPORTED_CLAIMS_REJECTED, no unsupported assertions
"""
import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.safety_gate import AnswerDisposition
from app.schemas.synthesis import SynthesisStatus
from app.synthesis.service import SynthesisService, get_synthesis_service
from app.synthesis.mock_provider import MockSynthesisProvider
from app.api.v1.query import get_synthesis_service as api_get_synthesis_service


@pytest.fixture
def client():
    # Explicitly isolate the test client with a MockSynthesisProvider so tests
    # never leak live outbound requests to external LLM APIs, regardless of .env settings.
    app.dependency_overrides[api_get_synthesis_service] = lambda: SynthesisService(provider=MockSynthesisProvider())
    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.pop(api_get_synthesis_service, None)


# ---------------------------------------------------------------------------
# Section 2 & 4: The Three Known Validation Cases End-to-End
# ---------------------------------------------------------------------------

def test_contract_val_unans_05_nist_sp_800_53(client):
    """
    val_unans_05: 'What are the baseline control enhancements in NIST SP 800-53 Revision 5 for supply chain risk management?'
    Must abstain cleanly end-to-end. Must not return QUALIFIED_HISTORICAL_EXCERPTS or FULLY_ANSWERED.
    """
    response = client.post(
        "/api/v1/query",
        json={"query": "What are the baseline control enhancements in NIST SP 800-53 Revision 5 for supply chain risk management?"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION
    assert data["synthesis_status"] == SynthesisStatus.ABSTAINED.value
    assert len(data["verified_excerpts"]) == 0
    assert len(data["claim_citations"]) == 0
    assert "Controlled Abstention" in data["answer_text"]
    assert "Insufficient evidence" in data["abstention_reason"]
    # Verify no unsupported affirmative claims are made
    assert "NIST" not in data["answer_text"]


def test_contract_val_unans_08_generic_cyber_risk_controls(client):
    """
    val_unans_08: 'How should general risk management and operational controls be implemented across financial systems?'
    Must abstain cleanly end-to-end. Must not produce false acceptance.
    """
    response = client.post(
        "/api/v1/query",
        json={"query": "How should general risk management and operational controls be implemented across financial systems?"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION
    assert data["synthesis_status"] == SynthesisStatus.ABSTAINED.value
    assert len(data["verified_excerpts"]) == 0
    assert len(data["claim_citations"]) == 0
    assert "Controlled Abstention" in data["answer_text"]
    assert "Insufficient evidence" in data["abstention_reason"]


def test_contract_val_unans_10_cyber_insurance_sum_insured(client):
    """
    val_unans_10: 'What is the minimum mandatory sum insured in Rupees for cyber insurance policies under CSCRF?'
    Must abstain cleanly end-to-end. Must not invent or assert insurance coverage monetary values.
    """
    response = client.post(
        "/api/v1/query",
        json={"query": "What is the minimum mandatory sum insured in Rupees for cyber insurance policies under CSCRF?"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION
    assert data["synthesis_status"] == SynthesisStatus.ABSTAINED.value
    assert len(data["verified_excerpts"]) == 0
    assert len(data["claim_citations"]) == 0
    assert "Controlled Abstention" in data["answer_text"]
    assert "Insufficient evidence" in data["abstention_reason"]
    # Ensure no fabricated rupees or insurance values are present in answer
    assert "Rupees" not in data["answer_text"]
    assert "sum insured" not in data["answer_text"]


# ---------------------------------------------------------------------------
# Section 4: Genuine CSCRF Question and Legitimate Paraphrase
# ---------------------------------------------------------------------------

def test_contract_genuine_cscrf_question(client):
    """
    Genuine CSCRF question: 'To whom does the Chief Information Security Officer (CISO) directly report under SEBI CSCRF?'
    Must be accepted with verified excerpts and FULLY_ANSWERED synthesis status.
    """
    response = client.post(
        "/api/v1/query",
        json={"query": "To whom does the Chief Information Security Officer (CISO) directly report under SEBI CSCRF?"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["disposition"] in [
        AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS,
        AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS,
    ]
    assert data["synthesis_status"] == SynthesisStatus.FULLY_ANSWERED.value
    assert len(data["verified_excerpts"]) > 0
    assert len(data["claim_citations"]) > 0
    assert any("ciso" in c["claim_text"].lower() or "md & ceo" in c["claim_text"].lower() for c in data["claim_citations"])


def test_contract_legitimate_paraphrase(client):
    """
    Legitimate non-verbatim paraphrase: 'Must offline isolated backups be stored in air-gapped locations to prevent tampering by ransomware?'
    Must be accepted with verified evidence and FULLY_ANSWERED.
    """
    response = client.post(
        "/api/v1/query",
        json={"query": "Must offline isolated backups be stored in air-gapped locations to prevent tampering by ransomware?"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["disposition"] in [
        AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS,
        AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS,
    ]
    assert data["synthesis_status"] == SynthesisStatus.FULLY_ANSWERED.value
    assert len(data["verified_excerpts"]) > 0
    assert len(data["claim_citations"]) > 0
    assert any("backup" in e["excerpt_text"].lower() or "isolated" in e["excerpt_text"].lower() for e in data["verified_excerpts"])


# ---------------------------------------------------------------------------
# Section 4: Question with Supported and Unsupported Subparts
# ---------------------------------------------------------------------------

def test_contract_multipart_supported_and_unsupported(client):
    """
    Question with both supported and unsupported subparts:
    'Must offline isolated backups be air-gapped and what is the penalty in rupees for non-compliance?'
    The backup requirement is supported in CSCRF, but the penalty in rupees is not established in the corpus passages.
    Must return PARTIALLY_ANSWERED with explicit UnresolvedSubquestion disclosure.
    """
    response = client.post(
        "/api/v1/query",
        json={"query": "Must offline isolated backups be air-gapped and what is the penalty in rupees for non-compliance?"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["disposition"] in [
        AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS,
        AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS,
    ]
    assert data["synthesis_status"] == SynthesisStatus.PARTIALLY_ANSWERED.value
    assert len(data["unresolved_subquestions"]) > 0
    assert any("penalty" in u["subquestion"].lower() for u in data["unresolved_subquestions"])



# ---------------------------------------------------------------------------
# Section 4: Absent External Regulatory Framework
# ---------------------------------------------------------------------------

def test_contract_absent_external_framework_dora(client):
    """
    Absent external framework inquiry (e.g. EU DORA TLPT threat-led penetration testing).
    Must cleanly abstain.
    """
    response = client.post(
        "/api/v1/query",
        json={"query": "What are the technical controls required under DORA TLPT threat-led penetration testing?"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION
    assert data["synthesis_status"] == SynthesisStatus.ABSTAINED.value
    assert len(data["verified_excerpts"]) == 0
    assert "Controlled Abstention" in data["answer_text"]


# ---------------------------------------------------------------------------
# Section 4: Provider Timeout and Unavailable Error Handling
# ---------------------------------------------------------------------------

def test_contract_provider_timeout_fallback(client, monkeypatch):
    """
    Provider timeout must result in PROVIDER_FAILED status, safe narrative,
    and must not silently present as a successful answer.
    """
    timeout_provider = MockSynthesisProvider(simulate_timeout=True)
    custom_service = SynthesisService(provider=timeout_provider)
    app.dependency_overrides[api_get_synthesis_service] = lambda: custom_service

    try:
        response = client.post(
            "/api/v1/query",
            json={"query": "To whom does the CISO directly report under SEBI CSCRF?"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["synthesis_status"] == SynthesisStatus.PROVIDER_FAILED.value
        assert "timed out" in data["answer_text"].lower()
        assert len(data["claim_citations"]) == 0
        assert data["synthesis_failure_reason"] is not None
    finally:
        app.dependency_overrides.pop(api_get_synthesis_service, None)


# ---------------------------------------------------------------------------
# Section 4: Unsupported Claim Rejected by Verifier
# ---------------------------------------------------------------------------

def test_contract_unsupported_claim_rejected(client, monkeypatch):
    """
    When the synthesis provider generates an unsupported assertion (e.g. fabricated RBI LCR rule),
    the verifier must reject the claims and return UNSUPPORTED_CLAIMS_REJECTED.
    """
    unsupported_provider = MockSynthesisProvider(simulate_unsupported_claim=True)
    custom_service = SynthesisService(provider=unsupported_provider)
    app.dependency_overrides[api_get_synthesis_service] = lambda: custom_service

    try:
        response = client.post(
            "/api/v1/query",
            json={"query": "To whom does the CISO directly report under SEBI CSCRF?"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["synthesis_status"] == SynthesisStatus.UNSUPPORTED_CLAIMS_REJECTED.value
        assert len(data["claim_citations"]) == 0
        assert "not substantiated by the retrieved evidence" in data["answer_text"]
    finally:
        app.dependency_overrides.pop(api_get_synthesis_service, None)
