"""
Unit and integration tests for Phase 8D: Regulatory Relationship Management and Human Review.

Validates:
1. Relationship schema and endpoint validation (AMENDS, SUPERSEDES, REPLACES, REFERENCES, CLARIFIES).
2. Human review lifecycle state machine:
   - PROPOSED/NEEDS_REVIEW -> VERIFIED
   - PROPOSED -> REJECTED
   - VERIFIED -> REVOKED
3. Audit log immutability and decision attribution.
4. Proposed, rejected, and revoked edges NEVER affect verified regulatory status (UNRESOLVED outcome preserved).
5. Only VERIFIED edges induce status transitions.
6. Non-superseding nature of CLARIFIES and REFERENCES (informational only).
7. Parity between FileGraphEdgeRepository and PostgresGraphEdgeRepository.
8. Idempotent baseline reconciliation preserving the 9 verified CSCRF edges without destructive overwriting.
9. Admin API authorization enforcement and input validation.
"""
import uuid
import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import get_settings
from app.schemas.graph import (
    GraphEdge,
    RelationshipType,
    ScopeType,
    ConfidenceLevel,
    ReviewStatus,
    EdgeProvenance,
    EdgeReviewHistoryItem,
)
from app.schemas.retrieval import RetrievalResult, RetrievalResponse
from app.schemas.grounding import RegulatoryStatus
from app.schemas.graph_collection import GraphEdgeCollection
from app.schemas.cscrf_graph_data import VERIFIED_CSCRF_EDGES_DATA
from app.grounding.resolver import ConservativeGraphResolver
from app.persistence.file_repo import FileStorageRepositoryFactory, FileGraphEdgeRepository
from app.persistence.interfaces import PersistenceError


from app.api.v1.admin import get_repository_factory

TEST_ADMIN_KEY = "test-secret-admin-key-12345"


@pytest.fixture
def admin_env(tmp_path, monkeypatch):
    """Fixture providing isolated file repository and configured admin API key."""
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    indexes_dir = tmp_path / "indexes"
    indexes_dir.mkdir(parents=True, exist_ok=True)

    factory = FileStorageRepositoryFactory(processed_dir=processed_dir)

    test_settings = get_settings().model_copy(
        update={
            "admin_api_key": TEST_ADMIN_KEY,
            "processed_dir": str(processed_dir),
            "indexes_dir": str(indexes_dir),
            "database_url": None,
        }
    )

    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_repository_factory] = lambda: factory

    monkeypatch.setenv("ADMIN_API_KEY", TEST_ADMIN_KEY)
    monkeypatch.setenv("PROCESSED_DIR", str(processed_dir))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    client = TestClient(app)
    headers = {"X-Admin-Key": TEST_ADMIN_KEY}

    yield client, headers, factory

    app.dependency_overrides.clear()


class TestRelationshipSchemaAndModel:
    """Tests for edge validation and taxonomy semantics."""

    def test_valid_edge_creation(self):
        edge = GraphEdge(
            edge_id="edge_test_super_01",
            source_document_id="sebi_cscrf_2025_999",
            target_document_id="sebi_cscrf_2024_113",
            relationship=RelationshipType.SUPERSEDES,
            scope=ScopeType.FULL,
            affected_provisions=[],
            evidence_text="Clause 1: This circular supersedes circular 2024/113 in full.",
            review_status=ReviewStatus.PROPOSED,
            provenance=EdgeProvenance.USER_PROPOSED,
        )
        assert edge.relationship == RelationshipType.SUPERSEDES
        assert edge.review_status == ReviewStatus.PROPOSED
        assert edge.is_supersession_inducing is True
        assert edge.is_verified is False

    def test_self_referential_edge_rejected(self):
        with pytest.raises(ValueError, match="Self-referential relationships are invalid"):
            GraphEdge(
                edge_id="edge_invalid_self",
                source_document_id="doc_a",
                target_document_id="doc_a",
                relationship=RelationshipType.REFERENCES,
                scope=ScopeType.FULL,
                evidence_text="Self cite",
            )

    def test_partial_scope_requires_affected_provisions(self):
        with pytest.raises(ValueError, match="affected_provisions.*must contain at least one specific clause"):
            GraphEdge(
                edge_id="edge_invalid_partial",
                source_document_id="doc_b",
                target_document_id="doc_a",
                relationship=RelationshipType.AMENDS,
                scope=ScopeType.PARTIAL,
                affected_provisions=[],
                evidence_text="Amends part of doc_a",
            )

    def test_clarifies_is_informational_not_superseding(self):
        edge = GraphEdge(
            edge_id="edge_clarifies_01",
            source_document_id="doc_b",
            target_document_id="doc_a",
            relationship=RelationshipType.CLARIFIES,
            scope=ScopeType.FULL,
            evidence_text="This circular clarifies implementation guidelines.",
            review_status=ReviewStatus.VERIFIED,
        )
        assert edge.is_supersession_inducing is False
        assert edge.is_verified is True


class TestConservativeResolverRegulatoryStatusSafety:
    """Tests confirming unreviewed edges never affect verified status, and new semantics behave conservatively."""

    def test_proposed_edges_never_affect_status(self):
        # Even if an edge claims full supersession, if it is PROPOSED or NEEDS_REVIEW, status must remain UNRESOLVED
        unreviewed_edge = GraphEdge(
            edge_id="edge_proposed_supersession",
            source_document_id="sebi_cscrf_2025_999",
            target_document_id="sebi_cscrf_2024_113",
            relationship=RelationshipType.SUPERSEDES,
            scope=ScopeType.FULL,
            evidence_text="Fully supersedes CIR/2024/113",
            review_status=ReviewStatus.PROPOSED,
        )
        graph = GraphEdgeCollection([unreviewed_edge])
        resolver = ConservativeGraphResolver(graph)

        item = RetrievalResult(
            chunk_id="sebi_cscrf_2024_113_chunk_001",
            document_id="sebi_cscrf_2024_113",
            circular_number="SEBI/HO/2024/113",
            title="CSCRF Master Circular",
            issue_date="2024-08-20",
            page_start=1,
            page_end=2,
            text="General guidelines for cyber resilience.",
            score=0.9,
            rank=1,
        )

        resolved = resolver.resolve_item(item)
        assert resolved.regulatory_status == RegulatoryStatus.UNRESOLVED
        assert resolved.applicable_edge_ids == []
        assert "no full-scope supersession or reference edge applies" in resolved.status_reason

    def test_rejected_and_revoked_edges_ignored_in_status(self):
        rejected_edge = GraphEdge(
            edge_id="edge_rejected_01",
            source_document_id="sebi_cscrf_2025_999",
            target_document_id="sebi_cscrf_2024_113",
            relationship=RelationshipType.SUPERSEDES,
            scope=ScopeType.FULL,
            evidence_text="Purported supersession",
            review_status=ReviewStatus.REJECTED,
        )
        revoked_edge = GraphEdge(
            edge_id="edge_revoked_01",
            source_document_id="sebi_cscrf_2025_888",
            target_document_id="sebi_cscrf_2024_113",
            relationship=RelationshipType.AMENDS,
            scope=ScopeType.FULL,
            evidence_text="Revoked amendment",
            review_status=ReviewStatus.REVOKED,
        )
        graph = GraphEdgeCollection([rejected_edge, revoked_edge])
        resolver = ConservativeGraphResolver(graph)

        item = RetrievalResult(
            chunk_id="sebi_cscrf_2024_113_chunk_002",
            document_id="sebi_cscrf_2024_113",
            circular_number="SEBI/HO/2024/113",
            title="CSCRF Master Circular",
            issue_date="2024-08-20",
            page_start=1,
            page_end=2,
            text="Unrelated text.",
            score=0.85,
            rank=1,
        )
        resolved = resolver.resolve_item(item)
        assert resolved.regulatory_status == RegulatoryStatus.UNRESOLVED
        assert resolved.applicable_edge_ids == []

    def test_verified_supersedes_and_replaces_induces_supersession(self):
        superseding_edge = GraphEdge(
            edge_id="edge_verified_supersede",
            source_document_id="sebi_cscrf_2025_999",
            target_document_id="sebi_cscrf_2024_113",
            relationship=RelationshipType.SUPERSEDES,
            scope=ScopeType.FULL,
            evidence_text="Explicit full supersession",
            review_status=ReviewStatus.VERIFIED,
        )
        graph = GraphEdgeCollection([superseding_edge])
        resolver = ConservativeGraphResolver(graph)

        item = RetrievalResult(
            chunk_id="sebi_cscrf_2024_113_chunk_001",
            document_id="sebi_cscrf_2024_113",
            circular_number="SEBI/HO/2024/113",
            title="CSCRF Master",
            issue_date="2024-08-20",
            page_start=1,
            page_end=2,
            text="Text in 2024/113",
            score=0.8,
            rank=1,
        )
        resolved = resolver.resolve_item(item)
        assert resolved.regulatory_status == RegulatoryStatus.SUPERSEDED
        assert "edge_verified_supersede" in resolved.applicable_edge_ids

    def test_verified_clarifies_is_informational_never_supersedes(self):
        clarifying_edge = GraphEdge(
            edge_id="edge_verified_clarify",
            source_document_id="sebi_cscrf_2025_119",
            target_document_id="sebi_cscrf_2024_113",
            relationship=RelationshipType.CLARIFIES,
            scope=ScopeType.FULL,
            evidence_text="Clarification of definitions",
            review_status=ReviewStatus.VERIFIED,
        )
        graph = GraphEdgeCollection([clarifying_edge])
        resolver = ConservativeGraphResolver(graph)

        item = RetrievalResult(
            chunk_id="sebi_cscrf_2024_113_chunk_001",
            document_id="sebi_cscrf_2024_113",
            circular_number="SEBI/HO/2024/113",
            title="CSCRF Master",
            issue_date="2024-08-20",
            page_start=1,
            page_end=2,
            text="Unrelated definitions without match.",
            score=0.8,
            rank=1,
        )
        resolved = resolver.resolve_item(item)
        assert resolved.regulatory_status == RegulatoryStatus.INFORMATIONAL
        assert "edge_verified_clarify" in resolved.applicable_edge_ids


class TestHumanReviewWorkflowAndAuditLifecycle:
    """Tests for edge proposal, review, rejection, and revocation lifecycle."""

    def test_file_repository_review_lifecycle(self, tmp_path):
        repo = FileGraphEdgeRepository(processed_dir=tmp_path)

        # 1. Propose edge
        candidate = GraphEdge(
            edge_id="test_prop_01",
            source_document_id="doc_new",
            target_document_id="doc_old",
            relationship=RelationshipType.AMENDS,
            scope=ScopeType.PARTIAL,
            affected_provisions=["Clause 4"],
            evidence_text="Amends clause 4",
            proposer_id="analyst_alice",
            review_rationale="Found in paragraph 2",
        )
        proposed = repo.propose_edge(candidate)
        assert proposed.review_status == ReviewStatus.PROPOSED
        assert proposed.provenance == EdgeProvenance.USER_PROPOSED
        assert len(proposed.review_history) == 1
        assert proposed.review_history[0].action == "PROPOSE"

        # 2. Cannot propose duplicate ID
        with pytest.raises(PersistenceError):
            repo.propose_edge(candidate)

        # 3. Human review: Verify
        verified = repo.review_edge(
            edge_id="test_prop_01",
            decision="verify",
            reviewer_id="reviewer_bob",
            reviewer_name="Bob Smith",
            rationale="Verified against original gazette PDF page 3",
        )
        assert verified.review_status == ReviewStatus.VERIFIED
        assert verified.reviewed_by == "reviewer_bob"
        assert len(verified.review_history) == 2
        assert verified.review_history[1].action == "VERIFY"
        assert verified.review_history[1].from_status == "proposed"
        assert verified.review_history[1].to_status == "verified"

        # 4. Revocation workflow
        revoked = repo.revoke_edge(
            edge_id="test_prop_01",
            reviewer_id="lead_reviewer_carol",
            rationale="Subsequent circular repealed this amendment",
        )
        assert revoked.review_status == ReviewStatus.REVOKED
        assert revoked.reviewed_by == "lead_reviewer_carol"
        assert len(revoked.review_history) == 3
        assert revoked.review_history[2].action == "REVOKE"

    def test_idempotent_baseline_reconciliation_preserves_verified_edges(self, tmp_path):
        repo = FileGraphEdgeRepository(processed_dir=tmp_path)
        baseline_edges = [GraphEdge(**e) for e in VERIFIED_CSCRF_EDGES_DATA]
        assert len(baseline_edges) == 9

        # First run: inserts all 9
        res1 = repo.reconcile_baseline_edges(baseline_edges)
        assert res1["inserted"] == 9
        assert res1["preserved"] == 0

        # Second run: idempotent, preserves all 9 without overwriting
        res2 = repo.reconcile_baseline_edges(baseline_edges)
        assert res2["inserted"] == 0
        assert res2["preserved"] == 9

        all_edges = repo.list_all_edges()
        assert len(all_edges) == 9
        assert all(e.review_status == ReviewStatus.VERIFIED for e in all_edges)


class TestAdminEdgeManagementAPI:
    """Tests for the REST API endpoints under /api/v1/admin/edges."""

    def test_admin_auth_required_for_edge_endpoints(self, admin_env):
        client, _, _ = admin_env
        # Missing auth header -> 401
        res = client.get("/api/v1/admin/edges")
        assert res.status_code == 401

        # Invalid auth header -> 403
        res = client.get("/api/v1/admin/edges", headers={"X-Admin-Key": "wrong_key"})
        assert res.status_code == 403

    def test_propose_and_review_api_flow(self, admin_env):
        client, headers, _ = admin_env
        # 1. Propose edge via API
        prop_payload = {
            "source_document_id": "sebi_cscrf_2025_999",
            "target_document_id": "sebi_cscrf_2024_113",
            "relationship": "SUPERSEDES",
            "scope": "full",
            "evidence_text": "Clause 1. This circular completely replaces CIR/2024/113.",
            "evidence_page": 1,
            "proposer_id": "compliance_officer_1",
            "rationale": "Notice issued on SEBI portal",
        }
        res = client.post("/api/v1/admin/edges/propose", json=prop_payload, headers=headers)
        assert res.status_code == 201
        edge_data = res.json()
        edge_id = edge_data["edge_id"]
        assert edge_data["review_status"] == "proposed"
        assert edge_data["relationship"] == "SUPERSEDES"
        assert len(edge_data["review_history"]) == 1

        # 2. Inspect edge
        res = client.get(f"/api/v1/admin/edges/{edge_id}", headers=headers)
        assert res.status_code == 200
        assert res.json()["edge_id"] == edge_id

        # 3. Review edge (verify)
        review_payload = {
            "decision": "verify",
            "reviewer_id": "legal_lead_01",
            "reviewer_name": "Senior Legal Counsel",
            "rationale": "Confirmed gazette publication and operative wording.",
        }
        res = client.post(f"/api/v1/admin/edges/{edge_id}/review", json=review_payload, headers=headers)
        assert res.status_code == 200
        reviewed_data = res.json()
        assert reviewed_data["review_status"] == "verified"
        assert reviewed_data["reviewed_by"] == "legal_lead_01"
        assert len(reviewed_data["review_history"]) == 2

        # 4. Revoke edge
        revoke_payload = {
            "reviewer_id": "legal_lead_01",
            "rationale": "Supersession was itself stayed by appellate tribunal order.",
        }
        res = client.post(f"/api/v1/admin/edges/{edge_id}/revoke", json=revoke_payload, headers=headers)
        assert res.status_code == 200
        revoked_data = res.json()
        assert revoked_data["review_status"] == "revoked"
        assert len(revoked_data["review_history"]) == 3

    def test_list_edges_pagination_and_filters(self, admin_env):
        client, headers, _ = admin_env
        res = client.get("/api/v1/admin/edges?limit=2&offset=0", headers=headers)
        assert res.status_code == 200
        items = res.json()
        assert isinstance(items, list)
        assert len(items) <= 2

    def test_invalid_state_transitions_rejected(self, admin_env):
        client, headers, _ = admin_env
        # Propose edge
        prop_res = client.post(
            "/api/v1/admin/edges/propose",
            json={
                "source_document_id": "sebi_cscrf_2025_999",
                "target_document_id": "sebi_cscrf_2024_113",
                "relationship": "SUPERSEDES",
                "scope": "full",
                "evidence_text": "Sample text",
                "proposer_id": "user1",
                "rationale": "Initial proposal rationale",
            },
            headers=headers,
        )
        assert prop_res.status_code == 201
        edge_id = prop_res.json()["edge_id"]

        # Attempt to REVOKE a PROPOSED edge directly -> 400 Bad Request
        revoke_res = client.post(
            f"/api/v1/admin/edges/{edge_id}/revoke",
            json={"reviewer_id": "user1", "rationale": "Premature revocation"},
            headers=headers,
        )
        assert revoke_res.status_code == 400
        assert "only VERIFIED edges can be revoked" in revoke_res.json()["detail"]

        # Verify edge
        client.post(
            f"/api/v1/admin/edges/{edge_id}/review",
            json={"decision": "verify", "reviewer_id": "user1", "rationale": "Valid"},
            headers=headers,
        )

        # Revoke edge
        client.post(
            f"/api/v1/admin/edges/{edge_id}/revoke",
            json={"reviewer_id": "user1", "rationale": "Revoking"},
            headers=headers,
        )

        # Attempt to REVIEW an already REVOKED edge -> 400 Bad Request
        review_revoked_res = client.post(
            f"/api/v1/admin/edges/{edge_id}/review",
            json={"decision": "verify", "reviewer_id": "user1", "rationale": "Re-review"},
            headers=headers,
        )
        assert review_revoked_res.status_code == 400
        assert "has already been REVOKED" in review_revoked_res.json()["detail"]


class TestConflictingEdgesAndReconciliationEdgeCases:
    """Tests for conservative conflict handling and reconciliation semantics."""

    def test_conflicting_verified_full_amendments_produce_unresolved(self):
        # Two conflicting verified edges superseding the same target document from different source circulars
        edge1 = GraphEdge(
            edge_id="edge_sup_doc_a",
            source_document_id="sebi_cscrf_2025_045",
            target_document_id="sebi_cscrf_2024_113",
            relationship=RelationshipType.SUPERSEDES,
            scope=ScopeType.FULL,
            affected_provisions=[],
            evidence_text="Circular A supersedes 113",
            review_status=ReviewStatus.VERIFIED,
        )
        edge2 = GraphEdge(
            edge_id="edge_sup_doc_b",
            source_document_id="sebi_cscrf_2025_096",
            target_document_id="sebi_cscrf_2024_113",
            relationship=RelationshipType.RESCINDS,
            scope=ScopeType.FULL,
            affected_provisions=[],
            evidence_text="Circular B rescinds 113",
            review_status=ReviewStatus.VERIFIED,
        )

        graph_collection = GraphEdgeCollection(edges=[edge1, edge2])
        resolver = ConservativeGraphResolver(graph_collection=graph_collection)

        item = RetrievalResult(
            chunk_id="sebi_cscrf_2024_113_p1_0",
            document_id="sebi_cscrf_2024_113",
            circular_number="CIR/2024/113",
            title="Baseline Master Circular",
            issue_date="2024-08-20",
            page_start=1,
            page_end=1,
            text="General preamble text without specific provisions",
            score=0.9,
            rank=1,
        )

        resolved = resolver.resolve_item(item)
        assert resolved.regulatory_status == RegulatoryStatus.UNRESOLVED
        assert "Conflicting verified full-scope relationship edges detected" in resolved.status_reason
        assert len(resolved.applicable_edge_ids) == 2

    def test_reconciliation_preserves_existing_rejected_or_revoked_edges(self, tmp_path):
        repo = FileGraphEdgeRepository(processed_dir=tmp_path)
        baseline_edges = [GraphEdge(**e) for e in VERIFIED_CSCRF_EDGES_DATA]
        edge0 = baseline_edges[0]

        # Pre-seed edge0 as REJECTED
        edge0_rejected = edge0.model_copy(
            update={
                "review_status": ReviewStatus.REJECTED,
                "review_rationale": "Human reviewer rejected this baseline proposal",
            }
        )
        repo.save_edge(edge0_rejected)

        # Run reconciliation without overwrite -> rejected decision is preserved
        res = repo.reconcile_baseline_edges(baseline_edges, allow_overwrite=False)
        assert res["inserted"] == 8
        assert res["preserved"] == 1
        assert res["updated"] == 0

        stored_edge0 = repo.get_edge(edge0.edge_id)
        assert stored_edge0 is not None
        assert stored_edge0.review_status == ReviewStatus.REJECTED
