"""
Phase 2.2B.9 — Deterministic Public API Benchmark Tests for GENERAL_STATIC Collection.

Exercises the actual POST /api/v1/query path with the synthetic general collection
using deterministic mock providers across:
1. Valid answer with correct evidence citations.
2. Unsupported claims.
3. Fabricated chunk IDs.
4. Wrong page references.
5. Contradictory / polarity claims.
6. Cross-collection citations.
7. Missing evidence / zero-results.
8. Provider timeout and malformed JSON failure paths.
"""

from typing import List, Dict, Any, Optional
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.api.v1.query import get_synthesis_service
from app.synthesis.service import SynthesisService
from app.synthesis.mock_provider import MockSynthesisProvider
from app.schemas.synthesis import (
    SynthesisInput,
    SynthesisOutput,
    SynthesisStatus,
    ClaimCitation,
    CitationProvenance,
)


class CustomConfigurableMockProvider(MockSynthesisProvider):
    """
    Deterministic provider allowing custom predefined SynthesisOutput responses.
    """
    def __init__(self, predefined_output: SynthesisOutput):
        super().__init__()
        self.predefined_output = predefined_output
        self.synthesize_called = False
        self.last_input: Optional[SynthesisInput] = None

    def synthesize(self, payload: SynthesisInput) -> SynthesisOutput:
        self.synthesize_called = True
        self.last_input = payload
        return self.predefined_output


def test_api_1_valid_answer_with_correct_citations():
    """
    1. Valid answer with faithful claim and correct evidence citations returns ACCEPTED / FULLY_ANSWERED.
    """
    custom_output = SynthesisOutput(
        status=SynthesisStatus.FULLY_ANSWERED,
        answer="All Acme user accounts must be a minimum 16 characters in length and require uppercase, lowercase, numeric, and special symbol characters.",
        claims=[
            ClaimCitation(
                claim_text="Acme user accounts must be a minimum 16 characters in length and require uppercase, lowercase, numeric, and special symbol characters.",
                supporting_chunk_ids=["acme_security_policy_p1_0"],
            )
        ],
    )
    provider = CustomConfigurableMockProvider(custom_output)
    service = SynthesisService(provider=provider)
    app.dependency_overrides[get_synthesis_service] = lambda: service

    try:
        client = TestClient(app)
        resp = client.post(
            "/api/v1/query",
            json={
                "query": "What is the minimum password length requirement and character complexity for Acme user accounts?",
                "collection_id": "general_synthetic",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["synthesis_status"] == "FULLY_ANSWERED"
        assert len(data["claim_citations"]) == 1
        claim = data["claim_citations"][0]
        assert claim["supporting_chunk_ids"] == ["acme_security_policy_p1_0"]
        # Provenance must be mechanically populated from authoritative evidence metadata
        assert len(claim["citations"]) == 1
        prov = claim["citations"][0]
        assert prov["document_id"] == "acme_security_policy"
        assert prov["page_start"] == 1
        assert prov["collection_id"] == "general_synthetic"
    finally:
        app.dependency_overrides.pop(get_synthesis_service, None)


def test_api_2_unsupported_claim_rejection():
    """
    2. Model emits an unsupported claim; verifier rejects with UNSUPPORTED_CLAIMS_REJECTED.
    """
    custom_output = SynthesisOutput(
        status=SynthesisStatus.FULLY_ANSWERED,
        answer="Acme accounts require a minimum of 48 characters with biometric retinal scans mandatory.",
        claims=[
            ClaimCitation(
                claim_text="Acme accounts require a minimum of 48 characters with biometric retinal scans mandatory.",
                supporting_chunk_ids=["acme_security_policy_p1_0"],
            )
        ],
    )
    provider = CustomConfigurableMockProvider(custom_output)
    service = SynthesisService(provider=provider)
    app.dependency_overrides[get_synthesis_service] = lambda: service

    try:
        client = TestClient(app)
        resp = client.post(
            "/api/v1/query",
            json={
                "query": "What is the minimum password length requirement for Acme user accounts?",
                "collection_id": "general_synthetic",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["synthesis_status"] == "UNSUPPORTED_CLAIMS_REJECTED"
        assert "not be verified" in (data["synthesis_failure_reason"] or "").lower()
    finally:
        app.dependency_overrides.pop(get_synthesis_service, None)


def test_api_3_fabricated_chunk_id_rejection():
    """
    3. Model hallucinates/invents a nonexistent chunk ID.
    """
    custom_output = SynthesisOutput(
        status=SynthesisStatus.FULLY_ANSWERED,
        answer="Service accounts must use short-lived certificates expiring every 12 hours.",
        claims=[
            ClaimCitation(
                claim_text="Service accounts must use short-lived certificates expiring every 12 hours.",
                supporting_chunk_ids=["fabricated_nonexistent_chunk_999"],
            )
        ],
    )
    provider = CustomConfigurableMockProvider(custom_output)
    service = SynthesisService(provider=provider)
    app.dependency_overrides[get_synthesis_service] = lambda: service

    try:
        client = TestClient(app)
        resp = client.post(
            "/api/v1/query",
            json={
                "query": "How are service accounts required to authenticate?",
                "collection_id": "general_synthetic",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["synthesis_status"] == "UNSUPPORTED_CLAIMS_REJECTED"
        assert "not be verified" in (data["synthesis_failure_reason"] or "").lower()
    finally:
        app.dependency_overrides.pop(get_synthesis_service, None)


def test_api_4_wrong_page_reference_sanitization():
    """
    4. Model attempts to invent a wrong page reference in CitationProvenance;
       the verifier overwrites it with authoritative corpus metadata.
    """
    custom_output = SynthesisOutput(
        status=SynthesisStatus.FULLY_ANSWERED,
        answer="Automated account lockout triggers after exceeding 5 consecutive trials.",
        claims=[
            ClaimCitation(
                claim_text="Automated account lockout triggers after exceeding 5 consecutive trials.",
                supporting_chunk_ids=["acme_security_policy_p1_0"],
                citations=[
                    CitationProvenance(
                        source_chunk_id="acme_security_policy_p1_0",
                        document_id="acme_security_policy",
                        page_start=999,  # Wrong page asserted by model
                        page_end=999,
                        regulatory_status="INFORMATIONAL",
                        collection_id="general_synthetic",
                    )
                ],
            )
        ],
    )
    provider = CustomConfigurableMockProvider(custom_output)
    service = SynthesisService(provider=provider)
    app.dependency_overrides[get_synthesis_service] = lambda: service

    try:
        client = TestClient(app)
        resp = client.post(
            "/api/v1/query",
            json={
                "query": "What happens after consecutive failed login trials at Acme?",
                "collection_id": "general_synthetic",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["synthesis_status"] == "FULLY_ANSWERED"
        # Page must be sanitized to authoritative corpus page_start (1), never the hallucinated 999
        prov = data["claim_citations"][0]["citations"][0]
        assert prov["page_start"] == 1
        assert prov["page_end"] == 2
    finally:
        app.dependency_overrides.pop(get_synthesis_service, None)


def test_api_5_contradictory_polarity_claim_rejection():
    """
    5. Contradictory claim asserting exemption where source states requirement or vice versa.
    """
    custom_output = SynthesisOutput(
        status=SynthesisStatus.FULLY_ANSWERED,
        answer="Service accounts are exempted and not required to use certificates.",
        claims=[
            ClaimCitation(
                claim_text="Service accounts are exempted and not required to use certificates.",
                supporting_chunk_ids=["acme_security_policy_p1_0"],
            )
        ],
    )
    provider = CustomConfigurableMockProvider(custom_output)
    service = SynthesisService(provider=provider)
    app.dependency_overrides[get_synthesis_service] = lambda: service

    try:
        client = TestClient(app)
        resp = client.post(
            "/api/v1/query",
            json={
                "query": "How are service accounts required to authenticate?",
                "collection_id": "general_synthetic",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["synthesis_status"] == "UNSUPPORTED_CLAIMS_REJECTED"
    finally:
        app.dependency_overrides.pop(get_synthesis_service, None)


def test_api_6_cross_collection_citation_rejection():
    """
    6. Cross-collection citation: Model attempts to cite a chunk belonging to another collection.
    """
    custom_output = SynthesisOutput(
        status=SynthesisStatus.FULLY_ANSWERED,
        answer="All entities must comply with cyber resilience framework standards.",
        claims=[
            ClaimCitation(
                claim_text="All entities must comply with cyber resilience framework standards.",
                supporting_chunk_ids=["sebi_cscrf_2024_089_p1_0"],  # SEBI chunk, foreign to general_synthetic
            )
        ],
    )
    provider = CustomConfigurableMockProvider(custom_output)
    service = SynthesisService(provider=provider)
    app.dependency_overrides[get_synthesis_service] = lambda: service

    try:
        client = TestClient(app)
        resp = client.post(
            "/api/v1/query",
            json={
                "query": "What is the minimum password length requirement and character complexity for Acme user accounts?",
                "collection_id": "general_synthetic",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["synthesis_status"] == "UNSUPPORTED_CLAIMS_REJECTED"
        assert "not be verified" in (data["synthesis_failure_reason"] or "").lower()
    finally:
        app.dependency_overrides.pop(get_synthesis_service, None)


def test_api_7_provider_timeout_and_malformed_failure_paths():
    """
    7. Exercise provider failure paths (timeout and malformed response) on general_synthetic collection.
    """
    # 7a. Timeout
    timeout_provider = MockSynthesisProvider(simulate_timeout=True)
    timeout_service = SynthesisService(provider=timeout_provider)
    app.dependency_overrides[get_synthesis_service] = lambda: timeout_service

    try:
        client = TestClient(app)
        resp = client.post(
            "/api/v1/query",
            json={
                "query": "What is the database recovery point objective?",
                "collection_id": "general_synthetic",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["synthesis_status"] == "PROVIDER_FAILED"
        assert "timed out" in (data["synthesis_failure_reason"] or "").lower()
    finally:
        app.dependency_overrides.pop(get_synthesis_service, None)

    # 7b. Malformed response
    malformed_provider = MockSynthesisProvider(simulate_malformed=True)
    malformed_service = SynthesisService(provider=malformed_provider)
    app.dependency_overrides[get_synthesis_service] = lambda: malformed_service

    try:
        client = TestClient(app)
        resp = client.post(
            "/api/v1/query",
            json={
                "query": "What is the database recovery point objective?",
                "collection_id": "general_synthetic",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["synthesis_status"] == "PROVIDER_FAILED"
        assert "validation" in (data["synthesis_failure_reason"] or "").lower()
    finally:
        app.dependency_overrides.pop(get_synthesis_service, None)
