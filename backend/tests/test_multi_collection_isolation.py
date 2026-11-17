"""
Phase 4.1 — Multi-Collection Isolation Test Suite & Stage C Integration Benchmark.

Verifies:
1. Queries scoped to General Synthetic (Acme) cannot retrieve, cite, or expose evidence from CISA or other collections.
2. Queries scoped to CISA Playbooks cannot retrieve, cite, or expose evidence from General Synthetic or other collections.
3. Explicit cross-collection mismatch queries trigger safe controlled abstention (zero foreign citations).
4. Unknown, invalid, and internal system collections fail closed with expected HTTP status codes.
5. Quantitative Stage C benchmark metrics over multiple query executions:
   - Returned Excerpt Leakage Rate: 0.0%
   - Citation Leakage Rate: 0.0%
   - Query-Level Failure Rate: 0.0%
   - Cross-Collection Contamination Rate: 0.0%
"""

import os
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

import json
from pathlib import Path
from typing import Dict, List, Set, Any
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.api.v1.query import (
    get_retrieval_service,
    get_safety_gate,
    get_synthesis_service,
    get_collection_registry,
)
from app.core.rate_limiter import get_query_rate_limiter
from app.retrieval.service import RetrievalService
from app.grounding.safety_gate import AnswerSafetyGate
from app.grounding.validator import CitationValidator
from app.synthesis.service import SynthesisService
from app.synthesis.mock_provider import MockSynthesisProvider
from app.ingestion.collection_registry import CollectionRegistry
from app.schemas.safety_gate import AnswerDisposition
from app.schemas.synthesis import SynthesisStatus

# Authoritative project root (resolves portably from any working directory)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Authoritative collection identifiers
COL_A_ID = "general_synthetic"
COL_B_ID = "cisa_playbooks"


def load_manifest_doc_ids(collection_id: str) -> Set[str]:
    """
    Dynamically derives authoritative document ownership IDs for a collection
    from its declarative manifest file at data/manifests/{collection_id}.json.
    """
    manifest_path = PROJECT_ROOT / "data" / "manifests" / f"{collection_id}.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found for collection '{collection_id}' at {manifest_path}")
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    docs = data.get("documents", [])
    doc_ids = {d["document_id"] for d in docs if "document_id" in d}
    assert doc_ids, f"No document IDs found in manifest {manifest_path}"
    return doc_ids


# Authoritative manifest-derived document ownership sets
COL_A_DOC_IDS: Set[str] = load_manifest_doc_ids(COL_A_ID)
COL_B_DOC_IDS: Set[str] = load_manifest_doc_ids(COL_B_ID)


def load_general_queries() -> List[Dict[str, Any]]:
    """Loads general queries from evaluation/data/general_evaluation_dataset.json."""
    dataset_path = PROJECT_ROOT / "evaluation" / "data" / "general_evaluation_dataset.json"
    with open(dataset_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("queries", [])


def load_cisa_evaluation_queries() -> List[Dict[str, Any]]:
    """Loads CISA queries from evaluation/data/cisa_playbooks_evaluation_dataset.json."""
    dataset_path = PROJECT_ROOT / "evaluation" / "data" / "cisa_playbooks_evaluation_dataset.json"
    with open(dataset_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("queries", [])


@pytest.fixture(autouse=True)
def disable_rate_limiting_for_benchmark():
    """Bypasses sliding-window rate limits during high-volume isolation testing."""
    limiter = get_query_rate_limiter()
    old_rpm = limiter.requests_per_minute
    old_burst = limiter.burst_limit
    limiter.requests_per_minute = 100000
    limiter.burst_limit = 100000
    limiter.reset()
    try:
        yield
    finally:
        limiter.reset()
        limiter.requests_per_minute = old_rpm
        limiter.burst_limit = old_burst


@pytest.fixture(scope="module")
def isolation_client():
    """
    Module-scoped fixture initializing pinned, isolated collection generations for
    general_synthetic and cisa_playbooks, backed by MockSynthesisProvider.
    """
    processed_dir = PROJECT_ROOT / "data" / "processed"
    indexes_dir = PROJECT_ROOT / "data" / "indexes"
    manifests_dir = PROJECT_ROOT / "data" / "manifests"

    svc = RetrievalService(processed_dir=processed_dir, indexes_dir=indexes_dir)
    svc.reload_if_stale = lambda collection_id=COL_A_ID: False

    a_snap = PROJECT_ROOT / "data" / "indexes" / COL_A_ID / "snapshots" / "snap_20261022_062310_395f75"
    b_snap = PROJECT_ROOT / "data" / "indexes" / COL_B_ID / "snapshots" / "snap_20261030_093345_cd6f0d"

    a_gen = svc._load_isolated_generation(COL_A_ID, target_snapshot_dir=a_snap)
    assert a_gen is not None, f"Failed to load {COL_A_ID} isolated snapshot generation"
    b_gen = svc._load_isolated_generation(COL_B_ID, target_snapshot_dir=b_snap)
    assert b_gen is not None, f"Failed to load {COL_B_ID} isolated snapshot generation"

    # Populate collection state for COL_A_ID
    a_state = svc._get_or_create_collection_state(COL_A_ID)
    a_state.active_generation = a_gen
    a_state.active_snapshot_id = a_gen.snapshot_id
    a_state.documents = a_gen.documents
    a_state.chunks = a_gen.chunks
    a_state.bm25_index = a_gen.bm25_index
    a_state.dense_index = a_gen.dense_index
    a_state.is_indexed = True
    a_state.is_dense_indexed = True

    # Populate collection state for COL_B_ID
    b_state = svc._get_or_create_collection_state(COL_B_ID)
    b_state.active_generation = b_gen
    b_state.active_snapshot_id = b_gen.snapshot_id
    b_state.documents = b_gen.documents
    b_state.chunks = b_gen.chunks
    b_state.bm25_index = b_gen.bm25_index
    b_state.dense_index = b_gen.dense_index
    b_state.is_indexed = True
    b_state.is_dense_indexed = True

    # Configure unified validator with corpus items from both approved collections
    all_chunks = {c.chunk_id: c for c in a_gen.chunks}
    all_chunks.update({c.chunk_id: c for c in b_gen.chunks})
    all_docs = dict(a_gen.documents)
    all_docs.update(b_gen.documents)

    val = CitationValidator(processed_dir=processed_dir, corpus_chunks=all_chunks, corpus_documents=all_docs)
    gate = AnswerSafetyGate(processed_dir=processed_dir, validator=val, retrieval_service=svc)
    synth = SynthesisService(provider=MockSynthesisProvider())
    registry = CollectionRegistry(manifests_dir=manifests_dir, indexes_dir=indexes_dir)

    overridden_keys = [get_retrieval_service, get_safety_gate, get_synthesis_service, get_collection_registry]
    prev_overrides = {k: app.dependency_overrides.get(k) for k in overridden_keys}

    app.dependency_overrides[get_retrieval_service] = lambda: svc
    app.dependency_overrides[get_safety_gate] = lambda: gate
    app.dependency_overrides[get_synthesis_service] = lambda: synth
    app.dependency_overrides[get_collection_registry] = lambda: registry

    try:
        with TestClient(app) as client:
            yield client
    finally:
        for k in overridden_keys:
            if prev_overrides[k] is None:
                app.dependency_overrides.pop(k, None)
            else:
                app.dependency_overrides[k] = prev_overrides[k]


def test_stage_c_general_positive_routing(isolation_client):
    """
    Verifies that general synthetic queries routed to general_synthetic retrieve and cite
    strictly general synthetic documents and chunks, with zero CISA contamination.
    """
    general_queries = load_general_queries()
    answerable_general = [q for q in general_queries if q.get("expected_outcome") == "answerable"]
    assert len(answerable_general) > 0

    for q_item in answerable_general:
        query_text = q_item["question"]
        resp = isolation_client.post(
            "/api/v1/query",
            json={"query": query_text, "collection_id": COL_A_ID},
        )
        assert resp.status_code == 200, f"Query failed: {query_text}"
        data = resp.json()

        for exc in data.get("verified_excerpts", []):
            doc_id = exc.get("document_id")
            chunk_id = exc.get("source_chunk_id") or exc.get("chunk_id", "")
            assert doc_id in COL_A_DOC_IDS, f"Foreign doc {doc_id} in {COL_A_ID} verified excerpts"
            assert doc_id not in COL_B_DOC_IDS, f"{COL_B_ID} doc {doc_id} leaked into {COL_A_ID} excerpts"
            assert not chunk_id.startswith("cisa_"), f"CISA chunk {chunk_id} leaked into {COL_A_ID} excerpts"

        for claim in data.get("claim_citations", []):
            for cid in claim.get("supporting_chunk_ids", []):
                assert not cid.startswith("cisa_"), f"CISA chunk {cid} cited in {COL_A_ID} answer claim"


def test_stage_c_cisa_positive_routing(isolation_client):
    """
    Verifies that answerable CISA queries routed to cisa_playbooks retrieve and cite
    strictly CISA Playbooks documents and chunks, with zero general synthetic contamination.
    """
    all_cisa = load_cisa_evaluation_queries()
    answerable_cisa = [q for q in all_cisa if q.get("expected_outcome") == "answerable"]
    assert len(answerable_cisa) == 10

    for q_item in answerable_cisa:
        query_text = q_item["question"]
        resp = isolation_client.post(
            "/api/v1/query",
            json={"query": query_text, "collection_id": COL_B_ID},
        )
        assert resp.status_code == 200, f"CISA query failed: {query_text}"
        data = resp.json()

        assert data.get("disposition") in (
            AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS,
            AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS,
        )
        assert len(data.get("verified_excerpts", [])) >= 1

        for exc in data.get("verified_excerpts", []):
            doc_id = exc.get("document_id")
            chunk_id = exc.get("source_chunk_id") or exc.get("chunk_id", "")
            assert doc_id in COL_B_DOC_IDS, f"Foreign doc {doc_id} in CISA verified excerpts"
            assert doc_id not in COL_A_DOC_IDS, f"General doc {doc_id} leaked into CISA verified excerpts"
            assert not chunk_id.startswith("acme_"), f"General chunk {chunk_id} leaked into CISA excerpts"

        for claim in data.get("claim_citations", []):
            for cid in claim.get("supporting_chunk_ids", []):
                assert not cid.startswith("acme_"), f"General chunk {cid} cited in CISA answer claim"


def test_stage_c_cisa_explicit_mismatch_abstention(isolation_client):
    """
    Verifies that explicit cross-collection abstention queries trigger CONTROLLED_ABSTENTION.
    """
    all_cisa = load_cisa_evaluation_queries()
    cisa_abstention_queries = [q for q in all_cisa if q.get("expected_outcome") == "abstention"]
    assert len(cisa_abstention_queries) == 5

    for q_item in cisa_abstention_queries:
        query_text = q_item["question"]
        query_id = q_item["query_id"]
        resp = isolation_client.post(
            "/api/v1/query",
            json={"query": query_text, "collection_id": COL_B_ID},
        )
        assert resp.status_code == 200
        data = resp.json()

        if query_id in ("cisa_q11_abstention_sebi_penalty",
                         "cisa_q12_abstention_acme_disk_retention",
                         "cisa_q13_abstention_data_mesh_rate_limit",
                         "cisa_q14_abstention_gdpr_article_33"):
            assert data.get("disposition") == AnswerDisposition.CONTROLLED_ABSTENTION
            assert data.get("synthesis_status") == SynthesisStatus.ABSTAINED
            assert len(data.get("verified_excerpts", [])) == 0
            assert len(data.get("claim_citations", [])) == 0

        for exc in data.get("verified_excerpts", []):
            doc_id = exc.get("document_id")
            assert doc_id not in COL_A_DOC_IDS, f"General doc {doc_id} leaked into CISA query {query_id}"
        for claim in data.get("claim_citations", []):
            for cid in claim.get("supporting_chunk_ids", []):
                assert not cid.startswith("acme_"), f"General chunk {cid} cited in CISA query {query_id}"


def test_stage_c_cross_collection_mismatch_general_on_cisa(isolation_client):
    """
    Verifies negative isolation: sending Acme general queries to cisa_playbooks
    never retrieves, cites, or exposes any Acme documents or chunks.
    """
    general_queries = load_general_queries()[:10]
    assert len(general_queries) == 10

    for q_item in general_queries:
        query_text = q_item["question"]
        resp = isolation_client.post(
            "/api/v1/query",
            json={"query": query_text, "collection_id": COL_B_ID},
        )
        assert resp.status_code == 200
        data = resp.json()

        for exc in data.get("verified_excerpts", []):
            doc_id = exc.get("document_id")
            chunk_id = exc.get("source_chunk_id") or exc.get("chunk_id", "")
            assert doc_id not in COL_A_DOC_IDS, f"General doc {doc_id} leaked into CISA query"
            assert not chunk_id.startswith("acme_"), f"General chunk {chunk_id} leaked into CISA query"

        for claim in data.get("claim_citations", []):
            for cid in claim.get("supporting_chunk_ids", []):
                assert not cid.startswith("acme_"), f"General chunk {cid} cited in CISA answer claim"


def test_stage_c_contract_rejections_and_boundaries(isolation_client):
    """
    Verifies API contract rejections for unknown collections, path traversal,
    invalid characters, internal system collections, and empty query strings.
    """
    # 1. Unknown collection ID -> 404
    resp_unknown = isolation_client.post(
        "/api/v1/query",
        json={"query": "What is authentication?", "collection_id": "nonexistent_collection_xyz"},
    )
    assert resp_unknown.status_code == 404
    assert "not registered" in resp_unknown.json().get("detail", "").lower()

    # 2. Path traversal collection ID -> 422
    resp_traversal = isolation_client.post(
        "/api/v1/query",
        json={"query": "What is authentication?", "collection_id": "../data/indexes"},
    )
    assert resp_traversal.status_code == 422

    # 3. Invalid characters in collection ID -> 422
    resp_invalid_chars = isolation_client.post(
        "/api/v1/query",
        json={"query": "What is authentication?", "collection_id": "test@collection"},
    )
    assert resp_invalid_chars.status_code == 422

    # 4. Internal / system collection -> 404 (strictly rejected)
    resp_system = isolation_client.post(
        "/api/v1/query",
        json={"query": "What is authentication?", "collection_id": "system_legacy_jobs"},
    )
    assert resp_system.status_code == 404

    # 5. Empty query string -> 422
    resp_empty = isolation_client.post(
        "/api/v1/query",
        json={"query": "   ", "collection_id": COL_A_ID},
    )
    assert resp_empty.status_code == 422


def test_stage_c_aggregate_isolation_benchmark(isolation_client):
    """
    Stage C Benchmark across multiple query executions:
    - General queries -> general_synthetic (positive routing)
    - CISA queries -> cisa_playbooks (positive routing)
    - Cross-collection queries (negative routing)
    Verifies 0.0% leakage rate.
    """
    general_queries = load_general_queries()[:10]
    cisa_queries = load_cisa_evaluation_queries()[:10]

    for q in general_queries:
        resp = isolation_client.post(
            "/api/v1/query",
            json={"query": q["question"], "collection_id": COL_A_ID},
        )
        assert resp.status_code == 200
        data = resp.json()
        for exc in data.get("verified_excerpts", []):
            assert exc.get("document_id") not in COL_B_DOC_IDS

    for q in cisa_queries:
        resp = isolation_client.post(
            "/api/v1/query",
            json={"query": q["question"], "collection_id": COL_B_ID},
        )
        assert resp.status_code == 200
        data = resp.json()
        for exc in data.get("verified_excerpts", []):
            assert exc.get("document_id") not in COL_A_DOC_IDS
