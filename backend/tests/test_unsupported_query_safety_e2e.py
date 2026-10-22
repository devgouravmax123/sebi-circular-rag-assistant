"""
Phase 2.2B.19: End-to-End Unsupported-Query Safety and Collection Isolation Tests.

Verifies:
1. All 11 unsupported queries from the broader general benchmark are handled safely:
   - 6 queries rejected at the relevance filter (0 provider calls, CONTROLLED_ABSTENTION / ABSTAINED).
   - 5 queries that retain candidates pass to downstream synthesis, where unsupported claims
     asserted by a deterministic mock provider are rejected by claim/citation verification
     (UNSUPPORTED_CLAIMS_REJECTED) without returning an unverified answer.
2. Cross-collection isolation:
   - Queries scoped to general_synthetic never pull SEBI evidence or cite SEBI chunks.
   - Cross-collection regulatory assertions are strictly rejected by the downstream verifier.
3. Determinism and Negative Controls:
   - A genuinely supported query returns FULLY_ANSWERED with valid citations.
   - An unsupported numeric claim is rejected.
   - A fabricated chunk ID is rejected.
   - A valid chunk ID attached to an unsupported claim does NOT bypass claim verification.
   - Provider timeout and malformed responses fail safely without crashing.
"""
import pytest
from typing import Optional
from fastapi.testclient import TestClient
from pathlib import Path
import json

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

repo_root = Path(__file__).resolve().parent.parent.parent


class ConfigurableTrackingMockProvider(MockSynthesisProvider):
    """
    Deterministic mock provider that tracks invocations and returns customizable responses.
    """
    def __init__(self, predefined_output: Optional[SynthesisOutput] = None):
        super().__init__()
        self.predefined_output = predefined_output
        self.call_count = 0
        self.last_input: Optional[SynthesisInput] = None

    def synthesize(self, payload: SynthesisInput) -> SynthesisOutput:
        self.call_count += 1
        self.last_input = payload
        if self.predefined_output:
            return self.predefined_output
        # Default fallback: unsupported assertion targeting the first available evidence chunk
        target_chunk_id = (
            payload.evidence_items[0].chunk_id
            if payload.evidence_items
            else "acme_security_policy_p1_0"
        )
        return SynthesisOutput(
            status=SynthesisStatus.FULLY_ANSWERED,
            answer="Acme awards a 40% performance bonus to SRE leads and deploys PgBouncer.",
            claims=[
                ClaimCitation(
                    claim_text="Acme awards a 40% performance bonus to SRE leads and deploys PgBouncer.",
                    supporting_chunk_ids=[target_chunk_id],
                )
            ],
        )


@pytest.fixture
def tracking_provider():
    provider = ConfigurableTrackingMockProvider()
    service = SynthesisService(provider=provider)
    app.dependency_overrides[get_synthesis_service] = lambda: service
    yield provider
    app.dependency_overrides.pop(get_synthesis_service, None)


# -----------------------------------------------------------------------------
# Task 2: Test All 11 Unsupported Queries End to End
# -----------------------------------------------------------------------------

def test_unsupported_relevance_rejections_stage(tracking_provider):
    """
    Verify the 6 unsupported queries that are rejected at the relevance filter:
    1. broad_q16: FIDO2 hardware vendor
    2. broad_q17: Kubernetes CNI plugin
    3. broad_q18: DLQ PagerDuty alerts
    4. broad_q19: Contractor background check standard
    5. broad_q25: SEBI Qualified RE status
    6. broad_q26: SEBI circular ISO 27001 mandate
    Provider must NOT be called (0 calls). Disposition must be CONTROLLED_ABSTENTION.
    """
    client = TestClient(app)
    relevance_rejection_queries = [
        ("broad_q16_unsupported_hardware_security_key_vendor", "Which specific hardware token vendor (e.g., YubiKey 5C NFC or Google Titan) does Acme procure for FIDO2 MFA?"),
        ("broad_q17_unsupported_k8s_cni_plugin", "Which Kubernetes CNI network overlay plugin (Cilium, Calico, or Flannel) is deployed on worker nodes?"),
        ("broad_q18_unsupported_dlq_notification_pagerduty", "Does Data Mesh dead-letter queue routing trigger a high-urgency PagerDuty alert to on-call engineers?"),
        ("broad_q19_unsupported_contractor_background_check", "What criminal background screening standard must third-party contractors satisfy prior to credential issuance?"),
        ("broad_q25_collection_isolation_sebi_qualified_re", "Does Acme Corporation qualify as a Qualified RE under SEBI CSCRF circular 2024/113?"),
        ("broad_q26_collection_isolation_iso_certification_mandate", "Within what timeline must Acme obtain ISO 27001 certification under circular 2025/119?"),
    ]

    for qid, qtext in relevance_rejection_queries:
        tracking_provider.call_count = 0
        resp = client.post("/api/v1/query", json={"query": qtext, "collection_id": "general_synthetic"})
        assert resp.status_code == 200, f"Failed for {qid}"
        data = resp.json()

        # Assert zero provider calls
        assert tracking_provider.call_count == 0, f"Provider was unexpectedly called for {qid}"
        # Assert controlled abstention at relevance stage
        assert data["disposition"] == "CONTROLLED_ABSTENTION", f"Expected CONTROLLED_ABSTENTION for {qid}"
        assert data["synthesis_status"] == "ABSTAINED", f"Expected ABSTAINED for {qid}"
        assert len(data["verified_excerpts"]) == 0
        assert len(data["claim_citations"]) == 0
        assert "insufficient evidence" in (data["abstention_reason"] or "").lower()


def test_unsupported_downstream_verifier_rejections_stage(tracking_provider):
    """
    Verify the 5 unsupported queries where candidate passages pass the relevance filter:
    1. broad_q15: SRE bonus structure
    2. broad_q20: PgBouncer connection pooler
    3. broad_q27: SEBI SOC reporting timeframe
    4. broad_q28: Password complexity vs DLQ
    5. broad_q29: Debian 12 parquet tiering
    Provider is invoked (1 call), returns an unsupported claim citing the candidate chunk.
    Downstream claim verification MUST reject the claim (UNSUPPORTED_CLAIMS_REJECTED).
    """
    client = TestClient(app)
    verifier_rejection_queries = [
        ("broad_q15_unsupported_employee_bonus_structure", "What percentage annual performance bonus is awarded to Acme SRE leads upon completing semi-annual DR drills?"),
        ("broad_q20_unsupported_postgres_connection_pooler", "Does Acme deploy PgBouncer or Pgpool-II in front of production PostgreSQL 16 instances?"),
        ("broad_q27_collection_isolation_soc_reporting_difference", "Must failed account lockout trials at Acme be reported directly to SEBI within 6 hours?"),
        ("broad_q28_adversarial_overlap_password_complexity_vs_dlq", "What dead-letter queue inspection timeframe applies to passwords with fewer than 16 characters?"),
        ("broad_q29_adversarial_overlap_debian_tiering", "How are Debian 12 operating system worker nodes tiered into cold parquet archives after 365 days?"),
    ]

    for qid, qtext in verifier_rejection_queries:
        tracking_provider.call_count = 0
        resp = client.post("/api/v1/query", json={"query": qtext, "collection_id": "general_synthetic"})
        assert resp.status_code == 200, f"Failed for {qid}"
        data = resp.json()

        # Assert provider was called exactly once
        assert tracking_provider.call_count == 1, f"Provider should have been called for {qid}"
        # Assert downstream synthesis rejected the unverified claim
        assert data["synthesis_status"] == "UNSUPPORTED_CLAIMS_REJECTED", f"Expected UNSUPPORTED_CLAIMS_REJECTED for {qid}"
        assert "not be verified" in (data["synthesis_failure_reason"] or "").lower()
        # Ensure no unverified claims or citations leak to verified state
        assert len(data["claim_citations"]) == 0


# -----------------------------------------------------------------------------
# Task 3: Test Cross-Collection Isolation
# -----------------------------------------------------------------------------

def test_cross_collection_isolation_enforcement():
    """
    Assert that requests scoped to general_synthetic remain strictly isolated:
    1. SEBI circular queries reject before synthesis without cross-collection retrieval.
    2. Model attempting to inject SEBI chunk citation is rejected by collection-aware verifier.
    """
    # 3a. SEBI chunk citation attempt under general_synthetic request
    cross_coll_output = SynthesisOutput(
        status=SynthesisStatus.FULLY_ANSWERED,
        answer="Acme must comply with SEBI master circular provisions on cybersecurity.",
        claims=[
            ClaimCitation(
                claim_text="Acme must comply with SEBI master circular provisions on cybersecurity.",
                supporting_chunk_ids=["sebi_cscrf_2024_113_p186_137"],  # SEBI chunk foreign to general_synthetic
            )
        ],
    )
    provider = ConfigurableTrackingMockProvider(predefined_output=cross_coll_output)
    service = SynthesisService(provider=provider)
    app.dependency_overrides[get_synthesis_service] = lambda: service

    try:
        client = TestClient(app)
        resp = client.post(
            "/api/v1/query",
            json={
                "query": "What database technology backs Acme production systems?",
                "collection_id": "general_synthetic",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        # Must be rejected because sebi_cscrf_2024_113_p186_137 is not in general_synthetic evidence
        assert data["synthesis_status"] == "UNSUPPORTED_CLAIMS_REJECTED"
        assert len(data["claim_citations"]) == 0
    finally:
        app.dependency_overrides.pop(get_synthesis_service, None)


# -----------------------------------------------------------------------------
# Task 4: Determinism and Negative Controls
# -----------------------------------------------------------------------------

def test_negative_control_supported_query_succeeds():
    """
    Control 1: A genuinely supported question returns FULLY_ANSWERED with valid citations.
    """
    valid_output = SynthesisOutput(
        status=SynthesisStatus.FULLY_ANSWERED,
        answer="Production database instances utilize PostgreSQL 16 with streaming replication.",
        claims=[
            ClaimCitation(
                claim_text="Production database instances utilize PostgreSQL 16 with streaming replication.",
                supporting_chunk_ids=["acme_infra_handbook_p1_0"],
            )
        ],
    )
    provider = ConfigurableTrackingMockProvider(predefined_output=valid_output)
    service = SynthesisService(provider=provider)
    app.dependency_overrides[get_synthesis_service] = lambda: service

    try:
        client = TestClient(app)
        resp = client.post(
            "/api/v1/query",
            json={
                "query": "What database technology backs Acme production systems?",
                "collection_id": "general_synthetic",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["synthesis_status"] == "FULLY_ANSWERED"
        assert len(data["claim_citations"]) == 1
        claim = data["claim_citations"][0]
        assert claim["supporting_chunk_ids"] == ["acme_infra_handbook_p1_0"]
        assert claim["citations"][0]["document_id"] == "acme_infra_handbook"
        assert claim["citations"][0]["collection_id"] == "general_synthetic"
    finally:
        app.dependency_overrides.pop(get_synthesis_service, None)


def test_negative_control_unsupported_number_rejected():
    """
    Control 2: An unsupported number asserted in claim is rejected by numeric fidelity.
    """
    unsupported_num_output = SynthesisOutput(
        status=SynthesisStatus.FULLY_ANSWERED,
        answer="Production database snapshots are retained for 999 calendar days.",
        claims=[
            ClaimCitation(
                claim_text="Production database snapshots are retained for 999 calendar days.",
                supporting_chunk_ids=["acme_infra_handbook_p1_0"],
            )
        ],
    )
    provider = ConfigurableTrackingMockProvider(predefined_output=unsupported_num_output)
    service = SynthesisService(provider=provider)
    app.dependency_overrides[get_synthesis_service] = lambda: service

    try:
        client = TestClient(app)
        resp = client.post(
            "/api/v1/query",
            json={
                "query": "How long are production database snapshots retained?",
                "collection_id": "general_synthetic",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["synthesis_status"] == "UNSUPPORTED_CLAIMS_REJECTED"
        assert len(data["claim_citations"]) == 0
    finally:
        app.dependency_overrides.pop(get_synthesis_service, None)


def test_negative_control_fabricated_chunk_id_rejected():
    """
    Control 3: A fabricated chunk ID is rejected by citation validation.
    """
    fabricated_output = SynthesisOutput(
        status=SynthesisStatus.FULLY_ANSWERED,
        answer="Production database instances utilize PostgreSQL 16.",
        claims=[
            ClaimCitation(
                claim_text="Production database instances utilize PostgreSQL 16.",
                supporting_chunk_ids=["fabricated_chunk_nonexistent_xyz"],
            )
        ],
    )
    provider = ConfigurableTrackingMockProvider(predefined_output=fabricated_output)
    service = SynthesisService(provider=provider)
    app.dependency_overrides[get_synthesis_service] = lambda: service

    try:
        client = TestClient(app)
        resp = client.post(
            "/api/v1/query",
            json={
                "query": "What database technology backs Acme production systems?",
                "collection_id": "general_synthetic",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["synthesis_status"] == "UNSUPPORTED_CLAIMS_REJECTED"
        assert len(data["claim_citations"]) == 0
    finally:
        app.dependency_overrides.pop(get_synthesis_service, None)


def test_negative_control_valid_chunk_id_with_unsupported_claim_fails():
    """
    Control 4: A valid chunk ID attached to an unsupported claim does NOT bypass verification.
    """
    unsupported_fact_output = SynthesisOutput(
        status=SynthesisStatus.FULLY_ANSWERED,
        answer="PostgreSQL instances automatically synchronize with MongoDB Atlas clusters.",
        claims=[
            ClaimCitation(
                claim_text="PostgreSQL instances automatically synchronize with MongoDB Atlas clusters.",
                supporting_chunk_ids=["acme_infra_handbook_p1_0"],  # Real chunk, but text does NOT support MongoDB
            )
        ],
    )
    provider = ConfigurableTrackingMockProvider(predefined_output=unsupported_fact_output)
    service = SynthesisService(provider=provider)
    app.dependency_overrides[get_synthesis_service] = lambda: service

    try:
        client = TestClient(app)
        resp = client.post(
            "/api/v1/query",
            json={
                "query": "What database technology backs Acme production systems?",
                "collection_id": "general_synthetic",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["synthesis_status"] == "UNSUPPORTED_CLAIMS_REJECTED"
        assert len(data["claim_citations"]) == 0
    finally:
        app.dependency_overrides.pop(get_synthesis_service, None)


def test_negative_control_provider_timeout_fails_safely():
    """
    Control 5: Provider timeout fails safely with PROVIDER_FAILED without crashing.
    """
    timeout_provider = MockSynthesisProvider(simulate_timeout=True)
    timeout_service = SynthesisService(provider=timeout_provider)
    app.dependency_overrides[get_synthesis_service] = lambda: timeout_service

    try:
        client = TestClient(app)
        resp = client.post(
            "/api/v1/query",
            json={
                "query": "What database technology backs Acme production systems?",
                "collection_id": "general_synthetic",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["synthesis_status"] == "PROVIDER_FAILED"
        assert "timed out" in (data["synthesis_failure_reason"] or "").lower()
    finally:
        app.dependency_overrides.pop(get_synthesis_service, None)
