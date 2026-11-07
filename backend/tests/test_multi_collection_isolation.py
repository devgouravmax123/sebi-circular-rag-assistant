"""
Phase 4.1 — Multi-Collection Isolation Test Suite & Stage C Integration Benchmark.

Verifies:
1. Queries scoped to SEBI CSCRF cannot retrieve, cite, or expose evidence from CISA or other collections.
2. Queries scoped to CISA Playbooks cannot retrieve, cite, or expose evidence from SEBI or other collections.
3. Explicit cross-collection mismatch queries trigger safe controlled abstention (zero foreign citations).
4. Unknown, invalid, and internal system collections fail closed with expected HTTP status codes.
5. Quantitative Stage C benchmark metrics over 67 query executions (across 47 unique natural-language queries):
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
SEBI_COLLECTION_ID = "sebi_cscrf"
CISA_COLLECTION_ID = "cisa_playbooks"


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
SEBI_DOC_IDS: Set[str] = load_manifest_doc_ids(SEBI_COLLECTION_ID)
CISA_DOC_IDS: Set[str] = load_manifest_doc_ids(CISA_COLLECTION_ID)


def load_sebi_gold_queries() -> List[Dict[str, Any]]:
    """Loads 32 SEBI CSCRF queries from evaluation/data/gold_evaluation_dataset.json."""
    dataset_path = PROJECT_ROOT / "evaluation" / "data" / "gold_evaluation_dataset.json"
    with open(dataset_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("queries", [])


def load_cisa_evaluation_queries() -> List[Dict[str, Any]]:
    """Loads 15 CISA queries from evaluation/data/cisa_playbooks_evaluation_dataset.json."""
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
    sebi_cscrf (full 168-chunk baseline) and cisa_playbooks (46-chunk baseline),
    backed by MockSynthesisProvider for offline, ₹0-cost evaluation under ADR-020.
    """
    processed_dir = PROJECT_ROOT / "data" / "processed"
    indexes_dir = PROJECT_ROOT / "data" / "indexes"
    manifests_dir = PROJECT_ROOT / "data" / "manifests"

    svc = RetrievalService(processed_dir=processed_dir, indexes_dir=indexes_dir)
    # Prevent disk pointer reloading from test-snapshot state
    svc.reload_if_stale = lambda collection_id="sebi_cscrf": False

    sebi_snap = PROJECT_ROOT / "data" / "indexes" / "snapshots" / "snap_20261011_104512_23a014"
    cisa_snap = PROJECT_ROOT / "data" / "indexes" / "cisa_playbooks" / "snapshots" / "snap_20261030_093345_cd6f0d"

    sebi_gen = svc._load_isolated_generation(SEBI_COLLECTION_ID, target_snapshot_dir=sebi_snap)
    assert sebi_gen is not None, "Failed to load SEBI isolated snapshot generation"
    cisa_gen = svc._load_isolated_generation(CISA_COLLECTION_ID, target_snapshot_dir=cisa_snap)
    assert cisa_gen is not None, "Failed to load CISA isolated snapshot generation"

    # Populate collection state for sebi_cscrf
    s_state = svc._get_or_create_collection_state(SEBI_COLLECTION_ID)
    s_state.active_generation = sebi_gen
    s_state.active_snapshot_id = sebi_gen.snapshot_id
    s_state.documents = sebi_gen.documents
    s_state.chunks = sebi_gen.chunks
    s_state.bm25_index = sebi_gen.bm25_index
    s_state.dense_index = sebi_gen.dense_index
    s_state.is_indexed = True
    s_state.is_dense_indexed = True

    # Populate collection state for cisa_playbooks
    c_state = svc._get_or_create_collection_state(CISA_COLLECTION_ID)
    c_state.active_generation = cisa_gen
    c_state.active_snapshot_id = cisa_gen.snapshot_id
    c_state.documents = cisa_gen.documents
    c_state.chunks = cisa_gen.chunks
    c_state.bm25_index = cisa_gen.bm25_index
    c_state.dense_index = cisa_gen.dense_index
    c_state.is_indexed = True
    c_state.is_dense_indexed = True

    # Configure unified validator with corpus items from both approved collections
    all_chunks = {c.chunk_id: c for c in sebi_gen.chunks}
    all_chunks.update({c.chunk_id: c for c in cisa_gen.chunks})
    all_docs = dict(sebi_gen.documents)
    all_docs.update(cisa_gen.documents)

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


def test_stage_c_sebi_positive_routing(isolation_client):
    """
    Verifies that SEBI gold queries routed to sebi_cscrf retrieve and cite
    strictly SEBI CSCRF documents and chunks, with zero CISA contamination.
    """
    sebi_queries = load_sebi_gold_queries()
    assert len(sebi_queries) == 32

    for q_item in sebi_queries:
        query_text = q_item["query_text"]
        resp = isolation_client.post(
            "/api/v1/query",
            json={"query": query_text, "collection_id": SEBI_COLLECTION_ID},
        )
        assert resp.status_code == 200, f"Query failed: {query_text}"
        data = resp.json()

        # Check verified excerpts
        for exc in data.get("verified_excerpts", []):
            doc_id = exc.get("document_id")
            chunk_id = exc.get("source_chunk_id") or exc.get("chunk_id", "")
            assert doc_id in SEBI_DOC_IDS, f"Foreign doc {doc_id} in SEBI verified excerpts"
            assert doc_id not in CISA_DOC_IDS, f"CISA doc {doc_id} leaked into SEBI verified excerpts"
            assert not chunk_id.startswith("cisa_"), f"CISA chunk {chunk_id} leaked into SEBI excerpts"

        # Check historical superseded excerpts
        for exc in data.get("historical_superseded_excerpts", []):
            doc_id = exc.get("document_id")
            chunk_id = exc.get("source_chunk_id") or exc.get("chunk_id", "")
            assert doc_id in SEBI_DOC_IDS, f"Foreign doc {doc_id} in SEBI superseded excerpts"
            assert doc_id not in CISA_DOC_IDS, f"CISA doc {doc_id} leaked into SEBI superseded excerpts"
            assert not chunk_id.startswith("cisa_"), f"CISA chunk {chunk_id} leaked into SEBI superseded"

        # Check claim citations
        for claim in data.get("claim_citations", []):
            for cid in claim.get("supporting_chunk_ids", []):
                assert not cid.startswith("cisa_"), f"CISA chunk {cid} cited in SEBI answer claim"


def test_stage_c_cisa_positive_routing(isolation_client):
    """
    Verifies that answerable CISA queries routed to cisa_playbooks retrieve and cite
    strictly CISA Playbooks documents and chunks, with zero SEBI contamination.
    """
    all_cisa = load_cisa_evaluation_queries()
    answerable_cisa = [q for q in all_cisa if q.get("expected_outcome") == "answerable"]
    assert len(answerable_cisa) == 10

    for q_item in answerable_cisa:
        query_text = q_item["question"]
        resp = isolation_client.post(
            "/api/v1/query",
            json={"query": query_text, "collection_id": CISA_COLLECTION_ID},
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
            assert doc_id in CISA_DOC_IDS, f"Foreign doc {doc_id} in CISA verified excerpts"
            assert doc_id not in SEBI_DOC_IDS, f"SEBI doc {doc_id} leaked into CISA verified excerpts"
            assert not chunk_id.startswith("sebi_"), f"SEBI chunk {chunk_id} leaked into CISA excerpts"

        for claim in data.get("claim_citations", []):
            for cid in claim.get("supporting_chunk_ids", []):
                assert not cid.startswith("sebi_"), f"SEBI chunk {cid} cited in CISA answer claim"


def test_stage_c_cisa_explicit_mismatch_abstention(isolation_client):
    """
    Verifies that explicit cross-collection abstention queries (cisa_q11 - cisa_q14)
    trigger CONTROLLED_ABSTENTION with synthesis_status=ABSTAINED and zero citations,
    and out-of-scope query cisa_q15 retrieves strictly within CISA with zero foreign docs.
    """
    all_cisa = load_cisa_evaluation_queries()
    cisa_abstention_queries = [q for q in all_cisa if q.get("expected_outcome") == "abstention"]
    assert len(cisa_abstention_queries) == 5

    for q_item in cisa_abstention_queries:
        query_text = q_item["question"]
        query_id = q_item["query_id"]
        resp = isolation_client.post(
            "/api/v1/query",
            json={"query": query_text, "collection_id": CISA_COLLECTION_ID},
        )
        assert resp.status_code == 200
        data = resp.json()

        # Explicit foreign collection queries (cisa_q11 to cisa_q14) must trigger CONTROLLED_ABSTENTION
        if query_id in ("cisa_q11_abstention_sebi_penalty",
                         "cisa_q12_abstention_acme_disk_retention",
                         "cisa_q13_abstention_data_mesh_rate_limit",
                         "cisa_q14_abstention_gdpr_article_33"):
            assert data.get("disposition") == AnswerDisposition.CONTROLLED_ABSTENTION
            assert data.get("synthesis_status") == SynthesisStatus.ABSTAINED
            assert len(data.get("verified_excerpts", [])) == 0
            assert len(data.get("claim_citations", [])) == 0
            assert data.get("abstention_reason") is not None and len(data["abstention_reason"]) > 0

        # Absolute isolation invariant across all CISA abstention queries: zero foreign documents or citations
        for exc in data.get("verified_excerpts", []):
            doc_id = exc.get("document_id")
            assert doc_id not in SEBI_DOC_IDS, f"SEBI doc {doc_id} leaked into CISA query {query_id}"
        for claim in data.get("claim_citations", []):
            for cid in claim.get("supporting_chunk_ids", []):
                assert not cid.startswith("sebi_"), f"SEBI chunk {cid} cited in CISA query {query_id}"


def test_stage_c_cross_collection_mismatch_sebi_on_cisa(isolation_client):
    """
    Verifies negative isolation: sending 10 SEBI regulatory queries to cisa_playbooks
    never retrieves, cites, or exposes any SEBI CSCRF documents or chunks.
    Queries explicitly mentioning SEBI trigger immediate CONTROLLED_ABSTENTION.
    """
    sebi_queries = load_sebi_gold_queries()[:10]
    assert len(sebi_queries) == 10

    for q_item in sebi_queries:
        query_text = q_item["query_text"]
        resp = isolation_client.post(
            "/api/v1/query",
            json={"query": query_text, "collection_id": CISA_COLLECTION_ID},
        )
        assert resp.status_code == 200
        data = resp.json()

        # Queries explicitly mentioning SEBI or CSCRF must trigger CONTROLLED_ABSTENTION
        if "sebi" in query_text.lower() or "cscrf" in query_text.lower():
            assert data.get("disposition") == AnswerDisposition.CONTROLLED_ABSTENTION
            assert data.get("synthesis_status") == SynthesisStatus.ABSTAINED

        # Absolute zero SEBI contamination permitted in any CISA response
        for exc in data.get("verified_excerpts", []):
            doc_id = exc.get("document_id")
            chunk_id = exc.get("source_chunk_id") or exc.get("chunk_id", "")
            assert doc_id not in SEBI_DOC_IDS, f"SEBI doc {doc_id} leaked into CISA query"
            assert not chunk_id.startswith("sebi_"), f"SEBI chunk {chunk_id} leaked into CISA query"

        for exc in data.get("historical_superseded_excerpts", []):
            doc_id = exc.get("document_id")
            assert doc_id not in SEBI_DOC_IDS, f"SEBI doc {doc_id} leaked into CISA superseded"

        for claim in data.get("claim_citations", []):
            for cid in claim.get("supporting_chunk_ids", []):
                assert not cid.startswith("sebi_"), f"SEBI chunk {cid} cited in CISA answer claim"


def test_stage_c_cross_collection_mismatch_cisa_on_sebi(isolation_client):
    """
    Verifies negative isolation: sending 10 CISA queries to sebi_cscrf
    never retrieves, cites, or exposes any CISA Playbooks documents or chunks.
    Queries explicitly mentioning CISA, playbooks, or EO 14028 trigger immediate CONTROLLED_ABSTENTION.
    """
    cisa_queries = load_cisa_evaluation_queries()[:10]
    assert len(cisa_queries) == 10

    for q_item in cisa_queries:
        query_text = q_item["question"]
        resp = isolation_client.post(
            "/api/v1/query",
            json={"query": query_text, "collection_id": SEBI_COLLECTION_ID},
        )
        assert resp.status_code == 200
        data = resp.json()

        # Queries explicitly mentioning CISA or playbooks must trigger CONTROLLED_ABSTENTION
        q_lower = query_text.lower()
        if "cisa" in q_lower or "14028" in q_lower or "federal" in q_lower:
            assert data.get("disposition") == AnswerDisposition.CONTROLLED_ABSTENTION
            assert data.get("synthesis_status") == SynthesisStatus.ABSTAINED

        # Absolute zero CISA contamination permitted in any SEBI response
        for exc in data.get("verified_excerpts", []):
            doc_id = exc.get("document_id")
            chunk_id = exc.get("source_chunk_id") or exc.get("chunk_id", "")
            assert doc_id not in CISA_DOC_IDS, f"CISA doc {doc_id} leaked into SEBI query"
            assert not chunk_id.startswith("cisa_"), f"CISA chunk {chunk_id} leaked into SEBI query"

        for exc in data.get("historical_superseded_excerpts", []):
            doc_id = exc.get("document_id")
            assert doc_id not in CISA_DOC_IDS, f"CISA doc {doc_id} leaked into SEBI superseded"

        for claim in data.get("claim_citations", []):
            for cid in claim.get("supporting_chunk_ids", []):
                assert not cid.startswith("cisa_"), f"CISA chunk {cid} cited in SEBI answer claim"


def test_stage_c_contract_rejections_and_boundaries(isolation_client):
    """
    Verifies API contract rejections for unknown collections, path traversal,
    invalid characters, internal system collections, and empty query strings.
    """
    # 1. Unknown collection ID -> 404
    resp_unknown = isolation_client.post(
        "/api/v1/query",
        json={"query": "What is CISO reporting?", "collection_id": "nonexistent_collection_xyz"},
    )
    assert resp_unknown.status_code == 404
    assert "not registered" in resp_unknown.json().get("detail", "").lower()

    # 2. Path traversal collection ID -> 422
    resp_traversal = isolation_client.post(
        "/api/v1/query",
        json={"query": "What is CISO reporting?", "collection_id": "../data/indexes"},
    )
    assert resp_traversal.status_code == 422

    # 3. Invalid characters in collection ID -> 422
    resp_invalid_chars = isolation_client.post(
        "/api/v1/query",
        json={"query": "What is CISO reporting?", "collection_id": "sebi@cscrf"},
    )
    assert resp_invalid_chars.status_code == 422

    # 4. Internal / system collection -> 404 (strictly rejected)
    resp_system = isolation_client.post(
        "/api/v1/query",
        json={"query": "What is CISO reporting?", "collection_id": "system_legacy_jobs"},
    )
    assert resp_system.status_code == 404

    # 5. Empty query string -> 422
    resp_empty = isolation_client.post(
        "/api/v1/query",
        json={"query": "   ", "collection_id": SEBI_COLLECTION_ID},
    )
    assert resp_empty.status_code == 422


def test_stage_c_aggregate_isolation_benchmark(isolation_client):
    """
    Stage C Benchmark across 67 eligible executions (derived from 47 unique queries):
    - 32 SEBI queries -> sebi_cscrf (positive routing)
    - 10 CISA answerable queries -> cisa_playbooks (positive routing)
    - 5 CISA abstention queries -> cisa_playbooks (negative within-collection)
    - 10 SEBI queries -> cisa_playbooks (negative cross-collection routing)
    - 10 CISA queries -> sebi_cscrf (negative cross-collection routing)

    Measures and asserts:
    - Unique Natural-Language Queries: 47
    - Total Query Routing Executions: 67 (>= 50 required)
    - Returned Excerpt Leakage Rate: 0.0%
    - Citation Leakage Rate: 0.0%
    - Query-Level Failure Rate: 0.0%
    - Cross-Collection Contamination Rate: 0.0%
    """
    sebi_queries = load_sebi_gold_queries()
    cisa_queries = load_cisa_evaluation_queries()

    execution_plan: List[Dict[str, Any]] = []

    # 1. 32 SEBI -> sebi_cscrf
    for q in sebi_queries:
        execution_plan.append({
            "query": q["query_text"],
            "target_collection": SEBI_COLLECTION_ID,
            "forbidden_docs": CISA_DOC_IDS,
            "forbidden_chunk_prefix": "cisa_",
            "test_type": "sebi_positive",
        })

    # 2. 10 CISA answerable -> cisa_playbooks
    for q in [q for q in cisa_queries if q.get("expected_outcome") == "answerable"]:
        execution_plan.append({
            "query": q["question"],
            "target_collection": CISA_COLLECTION_ID,
            "forbidden_docs": SEBI_DOC_IDS,
            "forbidden_chunk_prefix": "sebi_",
            "test_type": "cisa_positive",
        })

    # 3. 5 CISA abstention -> cisa_playbooks
    for q in [q for q in cisa_queries if q.get("expected_outcome") == "abstention"]:
        execution_plan.append({
            "query": q["question"],
            "target_collection": CISA_COLLECTION_ID,
            "forbidden_docs": SEBI_DOC_IDS,
            "forbidden_chunk_prefix": "sebi_",
            "test_type": "cisa_abstention",
        })

    # 4. 10 SEBI -> cisa_playbooks (cross-collection)
    for q in sebi_queries[:10]:
        execution_plan.append({
            "query": q["query_text"],
            "target_collection": CISA_COLLECTION_ID,
            "forbidden_docs": SEBI_DOC_IDS,
            "forbidden_chunk_prefix": "sebi_",
            "test_type": "cross_sebi_on_cisa",
        })

    # 5. 10 CISA -> sebi_cscrf (cross-collection)
    for q in cisa_queries[:10]:
        execution_plan.append({
            "query": q["question"],
            "target_collection": SEBI_COLLECTION_ID,
            "forbidden_docs": CISA_DOC_IDS,
            "forbidden_chunk_prefix": "cisa_",
            "test_type": "cross_cisa_on_sebi",
        })

    total_executions = len(execution_plan)
    unique_queries = {item["query"] for item in execution_plan}
    unique_queries_count = len(unique_queries)

    assert total_executions == 67, f"Expected 67 executions, found {total_executions}"
    assert total_executions >= 50, f"Stage C benchmark requires >= 50 executions"
    assert unique_queries_count == 47, f"Expected 47 unique queries, found {unique_queries_count}"

    total_excerpts_examined = 0
    leaking_excerpts_count = 0
    total_citations_examined = 0
    leaking_citations_count = 0
    query_level_failures = 0
    cross_collection_contaminations = 0
    total_raw_candidates_reported = 0

    for item in execution_plan:
        query_text = item["query"]
        target_coll = item["target_collection"]
        forbidden_docs = item["forbidden_docs"]
        forbidden_chunk_prefix = item["forbidden_chunk_prefix"]

        resp = isolation_client.post(
            "/api/v1/query",
            json={"query": query_text, "collection_id": target_coll},
        )

        if resp.status_code != 200:
            query_level_failures += 1
            continue

        data = resp.json()
        query_contaminated = False
        total_raw_candidates_reported += data.get("retrieval_candidate_count", 0)

        # Check all returned excerpts (post-quarantine verified and historical superseded)
        all_excerpts = data.get("verified_excerpts", []) + data.get("historical_superseded_excerpts", [])
        for exc in all_excerpts:
            total_excerpts_examined += 1
            doc_id = exc.get("document_id")
            chunk_id = exc.get("source_chunk_id") or exc.get("chunk_id", "")
            if doc_id in forbidden_docs or (chunk_id and chunk_id.startswith(forbidden_chunk_prefix)):
                leaking_excerpts_count += 1
                query_contaminated = True

        # Check all claim citations
        for claim in data.get("claim_citations", []):
            for chunk_id in claim.get("supporting_chunk_ids", []):
                total_citations_examined += 1
                if chunk_id.startswith(forbidden_chunk_prefix):
                    leaking_citations_count += 1
                    query_contaminated = True

        if query_contaminated:
            cross_collection_contaminations += 1

    # Metric calculations
    excerpt_leakage_rate = (
        (leaking_excerpts_count / total_excerpts_examined)
        if total_excerpts_examined > 0
        else 0.0
    )
    citation_leakage_rate = (
        (leaking_citations_count / total_citations_examined)
        if total_citations_examined > 0
        else 0.0
    )
    query_failure_rate = (
        (query_level_failures / total_executions)
        if total_executions > 0
        else 0.0
    )
    cross_collection_contamination = (
        (cross_collection_contaminations / total_executions)
        if total_executions > 0
        else 0.0
    )

    excerpt_leakage_str = (
        f"{excerpt_leakage_rate:.4%}"
        if total_excerpts_examined > 0
        else "0.0000% (N/A: 0 excerpts examined)"
    )
    citation_leakage_str = (
        f"{citation_leakage_rate:.4%}"
        if total_citations_examined > 0
        else "0.0000% (N/A: 0 citations examined)"
    )

    print("\n================ STAGE C ISOLATION BENCHMARK RESULTS ================")
    print(f"Unique Natural-Language Query Strings: {unique_queries_count}")
    print(f"Total Execution Routing Trials:        {total_executions} (>= 50 required)")
    print(f"Total Raw Candidates Reported by API: {total_raw_candidates_reported}")
    print(f"Total Excerpts Examined (Returned):   {total_excerpts_examined}")
    print(f"Leaking Excerpts (Cross-Collection):  {leaking_excerpts_count}")
    print(f"Returned Excerpt Leakage Rate:        {excerpt_leakage_str}")
    print(f"Total Citations Examined:             {total_citations_examined}")
    print(f"Leaking Citations (Cross-Collection): {leaking_citations_count}")
    print(f"Citation Leakage Rate:                {citation_leakage_str}")
    print(f"Query Failures (HTTP Non-200):        {query_level_failures}")
    print(f"Query-Level Failure Rate:             {query_failure_rate:.4%}")
    print(f"Cross-Collection Contamination Rate:  {cross_collection_contamination:.4%}")
    print("======================================================================\n")

    # Authoritative Acceptance Criteria
    assert excerpt_leakage_rate == 0.0, f"Returned excerpt leakage rate {excerpt_leakage_rate} > 0.0%"
    assert citation_leakage_rate == 0.0, f"Citation leakage rate {citation_leakage_rate} > 0.0%"
    assert query_failure_rate == 0.0, f"Query failure rate {query_failure_rate} > 0.0%"
    assert cross_collection_contamination == 0.0, f"Contamination {cross_collection_contamination} > 0.0%"
