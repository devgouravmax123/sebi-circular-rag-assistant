"""
Unit and concurrency tests for Task 12.2C: Pin Snapshot Generation Per Query.

Verifies:
1. Hybrid search pins a single immutable SnapshotGeneration across both BM25 and dense retrieval stages.
2. A concurrent reload_if_stale() happening between BM25 search and Dense search does not cause
   cross-generation result mixing; the in-flight query executes Dense retrieval against its pinned generation.
3. Subsequent queries issued after the atomic pointer reload use the newly reloaded generation.
4. Failed or corrupted snapshot reloads preserve the existing valid active generation without service disruption.
5. Multi-collection generation isolation: per-collection generations are pinned and reloaded independently.
"""
import hashlib
import json
import threading
from pathlib import Path
import pytest
import numpy as np

from app.retrieval.service import RetrievalService, SnapshotGeneration
from app.schemas.document import DocumentRecord, ChunkRecord
from app.persistence.file_repo import FileStorageRepositoryFactory
from app.retrieval.publisher import IndexPublisher
from app.storage.local import LocalStorageAdapter


class MockFastEmbedder:
    """Mock dense embedder for deterministic, fast unit tests without ML model loading."""
    def __init__(self, dim: int = 384, model_name: str = "BAAI/bge-small-en-v1.5"):
        self.dim = dim
        self.model_name = model_name

    def embed_texts(self, texts):
        matrix = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            val = float(sum(ord(c) for c in text) % 50 + 1) / 50.0
            matrix[i, :] = val
            norm = np.linalg.norm(matrix[i, :])
            if norm > 0:
                matrix[i, :] /= norm
        return matrix

    def embed_query(self, query):
        vec = np.zeros(self.dim, dtype=np.float32)
        val = float(sum(ord(c) for c in query) % 50 + 1) / 50.0
        vec[:] = val
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec /= norm
        return vec


@pytest.fixture
def test_setup(tmp_path):
    processed_dir = tmp_path / "processed"
    indexes_dir = tmp_path / "indexes"
    storage_dir = tmp_path / "storage"
    processed_dir.mkdir(parents=True, exist_ok=True)
    indexes_dir.mkdir(parents=True, exist_ok=True)
    storage_dir.mkdir(parents=True, exist_ok=True)

    factory = FileStorageRepositoryFactory(processed_dir=processed_dir)
    embedder = MockFastEmbedder()
    storage = LocalStorageAdapter(root_dir=storage_dir)

    return {
        "processed_dir": processed_dir,
        "indexes_dir": indexes_dir,
        "storage": storage,
        "factory": factory,
        "embedder": embedder,
    }


def _publish_collection_snapshot(
    factory: FileStorageRepositoryFactory,
    indexes_dir: Path,
    processed_dir: Path,
    embedder: MockFastEmbedder,
    collection_id: str,
    doc_id: str,
    chunk_id: str,
    text: str,
) -> str:
    """Helper to register a document and publish a snapshot for a collection."""
    doc_repo = factory.get_document_repository()
    chunk_repo = factory.get_chunk_repository()

    doc = DocumentRecord(
        document_id=doc_id,
        title=f"Doc {doc_id}",
        circular_number="CIR/2025/01",
        issue_date="2025-01-01",
        source_url=f"https://example.com/{doc_id}.pdf",
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        pdf_path=f"raw/{doc_id}.pdf",
        collection_id=collection_id,
    )
    doc_repo.save_document(doc)

    chunk = ChunkRecord(
        chunk_id=chunk_id,
        document_id=doc_id,
        text=text,
        page_start=1,
        page_end=1,
        collection_id=collection_id,
    )
    chunk_repo.save_chunks([chunk])

    publisher = IndexPublisher(
        repository_factory=factory,
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
        embedder=embedder,
        collection_id=collection_id,
    )
    res = publisher.build_and_publish_snapshot()
    assert res.success is True, f"Failed to publish snapshot: {res.message}"
    return res.snapshot_id


def test_01_hybrid_search_pins_single_generation_across_bm25_and_dense(test_setup):
    """Verify both BM25 and dense stages evaluate against the exact same pinned generation."""
    indexes_dir = test_setup["indexes_dir"]
    processed_dir = test_setup["processed_dir"]
    factory = test_setup["factory"]
    embedder = test_setup["embedder"]
    cid = "sebi_cscrf"

    snap_id = _publish_collection_snapshot(
        factory=factory,
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
        embedder=embedder,
        collection_id=cid,
        doc_id="doc_v1",
        chunk_id="chunk_v1",
        text="Authentication and cyber resilience guidelines for qualified entities.",
    )

    service = RetrievalService(
        processed_dir=processed_dir,
        indexes_dir=indexes_dir,
        dense_embedder=embedder,
        storage_adapter=test_setup["storage"],
    )

    resp = service.search_hybrid("resilience authentication", top_k=5, collection_id=cid)
    assert resp.total_results > 0
    assert resp.results[0].chunk_id == "chunk_v1"
    assert resp.results[0].document_id == "doc_v1"

    gen = service.get_active_generation(collection_id=cid)
    assert gen is not None
    assert gen.snapshot_id == snap_id
    assert gen.collection_id == cid
    assert len(gen.chunks) == 1
    assert gen.chunks[0].chunk_id == "chunk_v1"


def test_02_reload_between_bm25_and_dense_does_not_mix_generations(test_setup):
    """
    Deterministic concurrency test:
    Pause hybrid search between BM25 and Dense retrieval.
    Concurrently reload active snapshot to snap_2.
    Assert the resumed search executes Dense retrieval against snap_1, NOT snap_2.
    """
    indexes_dir = test_setup["indexes_dir"]
    processed_dir = test_setup["processed_dir"]
    factory = test_setup["factory"]
    embedder = test_setup["embedder"]
    cid = "sebi_cscrf"

    # Publish Snapshot 1
    snap_1_id = _publish_collection_snapshot(
        factory=factory,
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
        embedder=embedder,
        collection_id=cid,
        doc_id="doc_v1",
        chunk_id="chunk_v1",
        text="Multi-factor authentication mandates for registered market entities.",
    )

    service = RetrievalService(
        processed_dir=processed_dir,
        indexes_dir=indexes_dir,
        dense_embedder=embedder,
        storage_adapter=test_setup["storage"],
    )

    # Initial load of snap_1
    gen_initial = service.get_active_generation(collection_id=cid)
    assert gen_initial.snapshot_id == snap_1_id

    # Synchronization primitives
    bm25_executed = threading.Event()
    reload_completed = threading.Event()

    query_results = []
    dense_executed_on_snap = []

    original_search_bm25 = gen_initial.search_bm25
    original_search_dense = gen_initial.search_dense

    def hooked_search_bm25(*args, **kwargs):
        res = original_search_bm25(*args, **kwargs)
        # Signal that BM25 completed and wait for reload to happen concurrently
        bm25_executed.set()
        assert reload_completed.wait(timeout=5.0), "Timed out waiting for concurrent reload"
        return res

    def hooked_search_dense(*args, **kwargs):
        dense_executed_on_snap.append(gen_initial.snapshot_id)
        return original_search_dense(*args, **kwargs)

    gen_initial.search_bm25 = hooked_search_bm25
    gen_initial.search_dense = hooked_search_dense

    def query_worker():
        resp = service.search_hybrid("authentication security", top_k=5, collection_id=cid)
        query_results.append(resp)

    # Launch query in separate thread
    t = threading.Thread(target=query_worker)
    t.start()

    # Wait until Thread 1 completes BM25
    assert bm25_executed.wait(timeout=5.0), "Timed out waiting for BM25 execution"

    # Now, while Thread 1 is suspended between BM25 and Dense search:
    # Publish and promote Snapshot 2
    snap_2_id = _publish_collection_snapshot(
        factory=factory,
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
        embedder=embedder,
        collection_id=cid,
        doc_id="doc_v2",
        chunk_id="chunk_v2",
        text="Cloud security architecture and supply chain incident management.",
    )
    assert snap_2_id != snap_1_id

    reloaded = service.reload_if_stale(collection_id=cid)
    assert reloaded is True
    assert service.active_snapshot_id == snap_2_id

    # Allow query worker to proceed with Dense search
    reload_completed.set()
    t.join(timeout=5.0)

    # Verification:
    # 1. Dense search executed on snap_1 (the pinned generation), NOT snap_2
    assert dense_executed_on_snap == [snap_1_id]

    # 2. Results returned to the in-flight query belong strictly to snap_1
    assert len(query_results) == 1
    resp = query_results[0]
    assert resp.total_results > 0
    for res in resp.results:
        assert res.document_id == "doc_v1"
        assert res.chunk_id == "chunk_v1"
        assert res.chunk_id != "chunk_v2"


def test_03_subsequent_query_uses_newly_reloaded_generation(test_setup):
    """Verify that after pointer promotion and reload, subsequent queries use the new generation."""
    indexes_dir = test_setup["indexes_dir"]
    processed_dir = test_setup["processed_dir"]
    factory = test_setup["factory"]
    embedder = test_setup["embedder"]
    cid = "sebi_cscrf"

    snap_1_id = _publish_collection_snapshot(
        factory, indexes_dir, processed_dir, embedder, cid, "doc_v1", "chunk_v1", "Old security rule"
    )

    service = RetrievalService(
        processed_dir=processed_dir,
        indexes_dir=indexes_dir,
        dense_embedder=embedder,
        storage_adapter=test_setup["storage"],
    )

    # Query 1 uses snap_1
    resp1 = service.search_hybrid("security rule", collection_id=cid)
    assert resp1.results[0].chunk_id == "chunk_v1"

    # Publish snap_2
    snap_2_id = _publish_collection_snapshot(
        factory, indexes_dir, processed_dir, embedder, cid, "doc_v2", "chunk_v2", "New cloud governance rule"
    )

    # Query 2 automatically detects staleness and reloads to snap_2
    resp2 = service.search_hybrid("cloud governance", collection_id=cid)
    assert any(r.chunk_id == "chunk_v2" for r in resp2.results)
    assert resp2.results[0].chunk_id == "chunk_v2"
    assert resp2.results[0].document_id == "doc_v2"
    assert service.active_snapshot_id == snap_2_id
    assert service.get_active_generation(collection_id=cid).snapshot_id == snap_2_id
    assert len(service.get_active_generation(collection_id=cid).chunks) == 2


def test_04_failed_reload_preserves_valid_active_generation(test_setup):
    """Verify that a corrupted snapshot reload preserves the existing active generation without disruption."""
    indexes_dir = test_setup["indexes_dir"]
    processed_dir = test_setup["processed_dir"]
    factory = test_setup["factory"]
    embedder = test_setup["embedder"]
    cid = "sebi_cscrf"

    snap_1_id = _publish_collection_snapshot(
        factory, indexes_dir, processed_dir, embedder, cid, "doc_v1", "chunk_v1", "Baseline valid index"
    )

    service = RetrievalService(
        processed_dir=processed_dir,
        indexes_dir=indexes_dir,
        dense_embedder=embedder,
        storage_adapter=test_setup["storage"],
    )

    gen1 = service.get_active_generation(collection_id=cid)
    assert gen1.snapshot_id == snap_1_id

    # Create corrupted Snapshot (invalid JSON in chunks.json)
    snap2_dir = indexes_dir / cid / "snapshots" / "snap_corrupted"
    snap2_dir.mkdir(parents=True, exist_ok=True)
    (snap2_dir / "chunks.json").write_text("NOT_VALID_JSON{{{", encoding="utf-8")
    (snap2_dir / "documents.json").write_text("{}", encoding="utf-8")
    manifest = {
        "snapshot_id": "snap_corrupted",
        "collection_id": cid,
        "schema_version": "1.0",
    }
    manifest_bytes = json.dumps(manifest).encode("utf-8")
    (snap2_dir / "manifest.json").write_bytes(manifest_bytes)
    manifest_chk = hashlib.sha256(manifest_bytes).hexdigest()

    pointer = {
        "snapshot_id": "snap_corrupted",
        "collection_id": cid,
        "activated_at": "2026-11-02T00:00:00Z",
        "manifest_checksum": manifest_chk,
    }
    (indexes_dir / cid / "active_snapshot.json").write_text(json.dumps(pointer), encoding="utf-8")

    # Attempt reload
    reloaded = service.reload_if_stale(collection_id=cid)
    assert reloaded is False

    # Active generation is still snap_1!
    gen_current = service.get_active_generation(collection_id=cid)
    assert gen_current.snapshot_id == snap_1_id
    assert gen_current is gen1

    # Search continues serving without error
    resp = service.search_hybrid("Baseline valid", collection_id=cid)
    assert resp.total_results > 0
    assert resp.results[0].chunk_id == "chunk_v1"


def test_05_collection_generations_strictly_isolated(test_setup):
    """Verify multi-collection generations are pinned and reloaded independently."""
    indexes_dir = test_setup["indexes_dir"]
    processed_dir = test_setup["processed_dir"]
    factory = test_setup["factory"]
    embedder = test_setup["embedder"]

    cid_a = "sebi_cscrf"
    cid_b = "amfi_nav"

    snap_a1 = _publish_collection_snapshot(
        factory, indexes_dir, processed_dir, embedder, cid_a, "doc_a", "chunk_a1", "SEBI cybersecurity circular"
    )
    snap_b1 = _publish_collection_snapshot(
        factory, indexes_dir, processed_dir, embedder, cid_b, "doc_b", "chunk_b1", "AMFI NAV computation rules"
    )

    service = RetrievalService(
        processed_dir=processed_dir,
        indexes_dir=indexes_dir,
        dense_embedder=embedder,
        storage_adapter=test_setup["storage"],
    )

    gen_a = service.get_active_generation(collection_id=cid_a)
    gen_b = service.get_active_generation(collection_id=cid_b)

    assert gen_a.collection_id == cid_a
    assert gen_a.snapshot_id == snap_a1
    assert gen_b.collection_id == cid_b
    assert gen_b.snapshot_id == snap_b1
    assert gen_a is not gen_b

    # Update only Collection A to snap_a2
    snap_a2 = _publish_collection_snapshot(
        factory, indexes_dir, processed_dir, embedder, cid_a, "doc_a", "chunk_a2", "SEBI updated policy"
    )

    service.reload_if_stale(collection_id=cid_a)

    # Collection A is updated, Collection B remains unchanged
    assert service.get_active_generation(collection_id=cid_a).snapshot_id == snap_a2
    assert service.get_active_generation(collection_id=cid_b).snapshot_id == snap_b1


def test_06_lru_cache_capacity_enforced(test_setup):
    """Verify that when cache capacity is exceeded, least recently used collection state is evicted."""
    indexes_dir = test_setup["indexes_dir"]
    processed_dir = test_setup["processed_dir"]
    factory = test_setup["factory"]
    embedder = test_setup["embedder"]

    cids = ["coll_alpha", "coll_beta", "coll_gamma"]
    for cid in cids:
        _publish_collection_snapshot(
            factory, indexes_dir, processed_dir, embedder, cid, f"doc_{cid}", f"chk_{cid}", f"Content for {cid}"
        )

    # Max cached collections = 2
    service = RetrievalService(
        processed_dir=processed_dir,
        indexes_dir=indexes_dir,
        dense_embedder=embedder,
        storage_adapter=test_setup["storage"],
        max_cached_collections=2,
    )

    # Initial state contains default 'sebi_cscrf'
    assert service.cached_collection_count == 1
    assert "sebi_cscrf" in service.cached_collection_ids

    # Load coll_alpha: capacity reached (2) -> [sebi_cscrf, coll_alpha]
    service.get_active_generation(collection_id="coll_alpha")
    assert service.cached_collection_count == 2
    assert service.cached_collection_ids == ["sebi_cscrf", "coll_alpha"]

    # Load coll_beta: capacity exceeded -> sebi_cscrf is evicted -> [coll_alpha, coll_beta]
    service.get_active_generation(collection_id="coll_beta")
    assert service.cached_collection_count == 2
    assert service.cached_collection_ids == ["coll_alpha", "coll_beta"]
    assert "sebi_cscrf" not in service.cached_collection_ids

    # Load coll_gamma: capacity exceeded -> coll_alpha is evicted -> [coll_beta, coll_gamma]
    service.get_active_generation(collection_id="coll_gamma")
    assert service.cached_collection_count == 2
    assert service.cached_collection_ids == ["coll_beta", "coll_gamma"]
    assert "coll_alpha" not in service.cached_collection_ids


def test_07_lru_cache_access_updates_recency(test_setup):
    """Verify that accessing an existing collection updates its LRU recency position."""
    indexes_dir = test_setup["indexes_dir"]
    processed_dir = test_setup["processed_dir"]
    factory = test_setup["factory"]
    embedder = test_setup["embedder"]

    cids = ["coll_one", "coll_two", "coll_three"]
    for cid in cids:
        _publish_collection_snapshot(
            factory, indexes_dir, processed_dir, embedder, cid, f"doc_{cid}", f"chk_{cid}", f"Content for {cid}"
        )

    service = RetrievalService(
        processed_dir=processed_dir,
        indexes_dir=indexes_dir,
        dense_embedder=embedder,
        storage_adapter=test_setup["storage"],
        max_cached_collections=2,
    )

    # Load coll_one and coll_two (evicting sebi_cscrf)
    service.get_active_generation(collection_id="coll_one")
    service.get_active_generation(collection_id="coll_two")
    assert service.cached_collection_ids == ["coll_one", "coll_two"]

    # Re-access coll_one: it should move to the most recent position
    service.get_active_generation(collection_id="coll_one")
    assert service.cached_collection_ids == ["coll_two", "coll_one"]

    # Load coll_three: coll_two should now be evicted since coll_one was recently accessed
    service.get_active_generation(collection_id="coll_three")
    assert service.cached_collection_ids == ["coll_one", "coll_three"]
    assert "coll_two" not in service.cached_collection_ids


def test_08_evicted_collection_reloads_on_demand(test_setup):
    """Verify that an evicted collection transparently reloads from its published snapshot when queried again."""
    indexes_dir = test_setup["indexes_dir"]
    processed_dir = test_setup["processed_dir"]
    factory = test_setup["factory"]
    embedder = test_setup["embedder"]

    cids = ["coll_x", "coll_y", "coll_z"]
    snap_ids = {}
    for cid in cids:
        snap_ids[cid] = _publish_collection_snapshot(
            factory, indexes_dir, processed_dir, embedder, cid, f"doc_{cid}", f"chk_{cid}", f"Unique queryable text for {cid}"
        )

    service = RetrievalService(
        processed_dir=processed_dir,
        indexes_dir=indexes_dir,
        dense_embedder=embedder,
        storage_adapter=test_setup["storage"],
        max_cached_collections=2,
    )

    # Load coll_x
    gen_x1 = service.get_active_generation(collection_id="coll_x")
    assert gen_x1.snapshot_id == snap_ids["coll_x"]

    # Load coll_y and coll_z, which evicts coll_x from cache
    service.get_active_generation(collection_id="coll_y")
    service.get_active_generation(collection_id="coll_z")
    assert "coll_x" not in service.cached_collection_ids

    # Query coll_x again: should transparently reload and return full results
    gen_x2 = service.get_active_generation(collection_id="coll_x")
    assert gen_x2.snapshot_id == snap_ids["coll_x"]
    assert "coll_x" in service.cached_collection_ids

    resp = service.search_hybrid("Unique queryable text", collection_id="coll_x")
    assert resp.total_results > 0
    assert resp.results[0].chunk_id == "chk_coll_x"


def test_09_inflight_generation_preserved_across_eviction(test_setup):
    """
    Verify in-flight query safety: when a generation is pinned locally for retrieval,
    an eviction of that collection's state from the LRU cache does not invalidate
    the pinned SnapshotGeneration or cause query failure.
    """
    indexes_dir = test_setup["indexes_dir"]
    processed_dir = test_setup["processed_dir"]
    factory = test_setup["factory"]
    embedder = test_setup["embedder"]

    cids = ["coll_pinned", "coll_filler_1", "coll_filler_2"]
    for cid in cids:
        _publish_collection_snapshot(
            factory, indexes_dir, processed_dir, embedder, cid, f"doc_{cid}", f"chk_{cid}", f"Dedicated search terms for {cid}"
        )

    service = RetrievalService(
        processed_dir=processed_dir,
        indexes_dir=indexes_dir,
        dense_embedder=embedder,
        storage_adapter=test_setup["storage"],
        max_cached_collections=2,
    )

    # Simulate beginning of in-flight hybrid search: pin the active generation
    pinned_gen = service.get_active_generation(collection_id="coll_pinned")
    assert pinned_gen.collection_id == "coll_pinned"

    # Concurrent operations load filler collections, causing coll_pinned to be evicted from LRU cache
    service.get_active_generation(collection_id="coll_filler_1")
    service.get_active_generation(collection_id="coll_filler_2")
    assert "coll_pinned" not in service.cached_collection_ids

    # The in-flight query continues executing against its pinned generation
    # BM25 stage
    bm25_resp = pinned_gen.search_bm25("Dedicated search terms", top_k=5)
    assert bm25_resp.total_results > 0
    assert bm25_resp.results[0].chunk_id == "chk_coll_pinned"

    # Dense stage
    dense_resp = pinned_gen.search_dense("Dedicated search terms", top_k=5)
    assert dense_resp.total_results > 0
    assert dense_resp.results[0].chunk_id == "chk_coll_pinned"

    # Full search execution directly on service returns correct result
    resp = service.search_hybrid("Dedicated search terms", collection_id="coll_pinned")
    assert resp.total_results > 0
    assert resp.results[0].chunk_id == "chk_coll_pinned"


def test_10_lru_cache_concurrent_access_safety(test_setup):
    """Verify thread-safety of LRU cache under concurrent multi-collection access."""
    indexes_dir = test_setup["indexes_dir"]
    processed_dir = test_setup["processed_dir"]
    factory = test_setup["factory"]
    embedder = test_setup["embedder"]

    cids = [f"coll_thread_{i}" for i in range(6)]
    for cid in cids:
        _publish_collection_snapshot(
            factory, indexes_dir, processed_dir, embedder, cid, f"doc_{cid}", f"chk_{cid}", f"Multi-thread text for {cid}"
        )

    service = RetrievalService(
        processed_dir=processed_dir,
        indexes_dir=indexes_dir,
        dense_embedder=embedder,
        storage_adapter=test_setup["storage"],
        max_cached_collections=3,
    )

    errors = []

    def worker(worker_id: int):
        try:
            for cycle in range(15):
                cid = cids[(worker_id + cycle) % len(cids)]
                gen = service.get_active_generation(collection_id=cid)
                assert gen.collection_id == cid
                # Verify bounded capacity invariant
                assert service.cached_collection_count <= 3
                resp = service.search_hybrid("Multi-thread text", collection_id=cid)
                assert resp.total_results > 0
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"Concurrent LRU errors: {errors}"
    assert service.cached_collection_count <= 3

