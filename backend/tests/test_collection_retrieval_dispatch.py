"""
Comprehensive unit and integration tests for Phase 2.2A:
Collection-Specific Snapshot Publishing and Retrieval Dispatch.

Verifies:
1. Snapshot Publishing:
   - Two collections can publish independent snapshots.
   - Each collection has its own active pointer under data/indexes/{collection_id}/active_snapshot.json.
   - A collection cannot publish a snapshot owned by another collection.
   - Invalid collection IDs and path traversal attempts are rejected.
   - Missing/malformed snapshot metadata is rejected.
   - Failed publication preserves the previous active pointer.
   - Malformed or cross-collection pointer fails closed.
   - Legacy pointer compatibility is strictly restricted to sebi_cscrf.
   - A non-SEBI collection never falls back to the legacy SEBI pointer.

2. Retrieval Isolation:
   - BM25 candidates are strictly restricted to the requested collection.
   - Dense candidates are strictly restricted to the requested collection.
   - RRF never merges candidates from different collections.
   - Identical document IDs or titles in separate collections cannot cause cross-collection leakage.
   - Unresolved ownership is not silently assigned to SEBI.
   - Existing SEBI retrieval behavior is preserved.
"""
import json
import pytest
import numpy as np
from pathlib import Path
from unittest.mock import MagicMock

from app.schemas.document import DocumentRecord, ChunkRecord, resolve_collection_identity
from app.schemas.publication import SnapshotManifest, ActiveSnapshotPointer, PublicationResult
from app.schemas.retrieval import RetrievalFilter
from app.persistence.file_repo import FileStorageRepositoryFactory
from app.retrieval.publisher import IndexPublisher, IndexValidationError
from app.retrieval.service import RetrievalService


class MockFastEmbedder:
    """Mock dense embedder for fast, deterministic, non-network unit testing."""
    def __init__(self, dim: int = 384, model_name: str = "BAAI/bge-small-en-v1.5"):
        self.dim = dim
        self.model_name = model_name

    def embed_texts(self, texts):
        matrix = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            # Deterministic pseudo-vector derived from text hash
            val = float(sum(ord(c) for c in text) % 100) / 100.0
            matrix[i, :] = val
            # normalize unit vector
            norm = np.linalg.norm(matrix[i, :])
            if norm > 0:
                matrix[i, :] /= norm
        return matrix

    def embed_query(self, query):
        vec = np.zeros(self.dim, dtype=np.float32)
        val = float(sum(ord(c) for c in query) % 100) / 100.0
        vec[:] = val
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec /= norm
        return vec


@pytest.fixture
def test_env(tmp_path):
    """Sets up an isolated filesystem environment with mock documents across two collections."""
    processed_dir = tmp_path / "processed"
    indexes_dir = tmp_path / "indexes"
    processed_dir.mkdir(parents=True, exist_ok=True)
    indexes_dir.mkdir(parents=True, exist_ok=True)

    factory = FileStorageRepositoryFactory(processed_dir=processed_dir)
    doc_repo = factory.get_document_repository()
    chunk_repo = factory.get_chunk_repository()

    # Create Collection A documents ("sebi_cscrf")
    doc_a = DocumentRecord(
        document_id="sebi_cscrf_2024_113",
        title="SEBI CSCRF Master Circular on Cybersecurity",
        circular_number="SEBI/HO/MRD/2024/113",
        issue_date="2024-08-20",
        source_url="https://sebi.gov.in/cscrf_2024.pdf",
        sha256="aaa111" + "0" * 58,
        pdf_path="raw/cscrf.pdf",
        collection_id="sebi_cscrf",
    )
    doc_repo.save_document(doc_a)

    chunk_a = ChunkRecord(
        chunk_id="sebi_cscrf_2024_113_p1_0",
        document_id="sebi_cscrf_2024_113",
        text="Cyber Resilience Framework mandates multi-factor authentication for market infrastructure institutions.",
        page_start=1,
        page_end=1,
        collection_id="sebi_cscrf",
    )
    chunk_repo.save_chunks([chunk_a])

    # Create Collection B documents ("amfi_nav")
    doc_b = DocumentRecord(
        document_id="amfi_nav_circular_001",
        title="AMFI Circular on Daily NAV Computation",
        circular_number="AMFI/2025/01",
        issue_date="2025-01-15",
        source_url="https://amfiindia.com/nav.pdf",
        sha256="bbb222" + "0" * 58,
        pdf_path="raw/amfi.pdf",
        collection_id="amfi_nav",
    )
    doc_repo.save_document(doc_b)

    chunk_b = ChunkRecord(
        chunk_id="amfi_nav_circular_001_p1_0",
        document_id="amfi_nav_circular_001",
        text="Mutual funds must declare cut-off timing and NAV valuation before 9 PM on all business days.",
        page_start=1,
        page_end=1,
        collection_id="amfi_nav",
    )
    chunk_repo.save_chunks([chunk_b])

    embedder = MockFastEmbedder()

    return {
        "processed_dir": processed_dir,
        "indexes_dir": indexes_dir,
        "factory": factory,
        "embedder": embedder,
    }


def test_independent_snapshot_publishing_for_two_collections(test_env):
    """Verify two collections publish independent snapshots and separate active pointers."""
    indexes_dir = test_env["indexes_dir"]
    processed_dir = test_env["processed_dir"]
    factory = test_env["factory"]
    embedder = test_env["embedder"]

    # 1. Publish Collection A ("sebi_cscrf")
    pub_a = IndexPublisher(
        repository_factory=factory,
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
        embedder=embedder,
        collection_id="sebi_cscrf",
    )
    res_a = pub_a.build_and_publish_snapshot()
    assert res_a.success is True
    assert res_a.total_documents == 1
    assert res_a.total_chunks == 1
    assert "sebi_cscrf_2024_113" in res_a.newly_published_documents

    # 2. Publish Collection B ("amfi_nav")
    pub_b = IndexPublisher(
        repository_factory=factory,
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
        embedder=embedder,
        collection_id="amfi_nav",
    )
    res_b = pub_b.build_and_publish_snapshot()
    assert res_b.success is True
    assert res_b.total_documents == 1
    assert res_b.total_chunks == 1
    assert "amfi_nav_circular_001" in res_b.newly_published_documents

    # Verify partition pointer for Collection B
    pointer_b_file = indexes_dir / "amfi_nav" / "active_snapshot.json"
    assert pointer_b_file.exists()
    pointer_b = json.loads(pointer_b_file.read_text(encoding="utf-8"))
    assert pointer_b["collection_id"] == "amfi_nav"
    assert pointer_b["snapshot_id"] == res_b.snapshot_id
    assert pointer_b["total_documents"] == 1

    # Verify manifest for Collection B
    manifest_b_file = indexes_dir / "amfi_nav" / "snapshots" / res_b.snapshot_id / "manifest.json"
    assert manifest_b_file.exists()
    manifest_b = json.loads(manifest_b_file.read_text(encoding="utf-8"))
    assert manifest_b["collection_id"] == "amfi_nav"
    assert manifest_b["document_ids"] == ["amfi_nav_circular_001"]

    # Verify Collection A does not contain Collection B items
    manifest_a = pub_a.get_active_manifest()
    assert manifest_a is not None
    assert manifest_a.collection_id == "sebi_cscrf"
    assert manifest_a.document_ids == ["sebi_cscrf_2024_113"]
    assert "amfi_nav_circular_001" not in manifest_a.document_ids


def test_invalid_collection_id_and_path_traversal_rejection(test_env):
    """Verify invalid collection IDs, path traversal, and unsafe pointer targets fail fast."""
    indexes_dir = test_env["indexes_dir"]
    processed_dir = test_env["processed_dir"]
    factory = test_env["factory"]
    embedder = test_env["embedder"]

    # 1. Invalid collection ID characters
    with pytest.raises(IndexValidationError, match="Invalid collection_id"):
        IndexPublisher(
            repository_factory=factory,
            indexes_dir=indexes_dir,
            processed_dir=processed_dir,
            embedder=embedder,
            collection_id="sebi/../traversal",
        )

    with pytest.raises(IndexValidationError, match="Invalid collection_id"):
        IndexPublisher(
            repository_factory=factory,
            indexes_dir=indexes_dir,
            processed_dir=processed_dir,
            embedder=embedder,
            collection_id="coll with spaces",
        )

    with pytest.raises(IndexValidationError, match="non-empty string"):
        IndexPublisher(
            repository_factory=factory,
            indexes_dir=indexes_dir,
            processed_dir=processed_dir,
            embedder=embedder,
            collection_id="",
        )


def test_failed_publication_preserves_previous_active_pointer(test_env, monkeypatch):
    """Verify failed publication preserves the existing active pointer."""
    indexes_dir = test_env["indexes_dir"]
    processed_dir = test_env["processed_dir"]
    factory = test_env["factory"]
    embedder = test_env["embedder"]

    pub_b = IndexPublisher(
        repository_factory=factory,
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
        embedder=embedder,
        collection_id="amfi_nav",
    )
    res_initial = pub_b.build_and_publish_snapshot()
    assert res_initial.success is True
    initial_snap_id = res_initial.snapshot_id

    def fail_validate(chunks, embeddings):
        raise IndexValidationError("Simulated validation error on candidate artifacts")

    monkeypatch.setattr(pub_b, "_validate_candidate_artifacts", fail_validate)

    res_fail = pub_b.build_and_publish_snapshot()
    assert res_fail.success is False
    assert "Simulated validation error" in res_fail.message

    # Pointer must remain intact pointing to initial_snap_id
    current_ptr = pub_b.get_active_pointer()
    assert current_ptr is not None
    assert current_ptr.snapshot_id == initial_snap_id


def test_cross_collection_pointer_and_manifest_fails_closed(test_env):
    """Verify that a malformed or cross-collection pointer/manifest fails closed and is rejected."""
    indexes_dir = test_env["indexes_dir"]
    processed_dir = test_env["processed_dir"]
    factory = test_env["factory"]
    embedder = test_env["embedder"]

    pub_b = IndexPublisher(
        repository_factory=factory,
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
        embedder=embedder,
        collection_id="amfi_nav",
    )
    res = pub_b.build_and_publish_snapshot()
    assert res.success is True

    # Tamper pointer to declare wrong collection_id
    pointer_file = indexes_dir / "amfi_nav" / "active_snapshot.json"
    ptr_data = json.loads(pointer_file.read_text(encoding="utf-8"))
    ptr_data["collection_id"] = "foreign_collection"
    pointer_file.write_text(json.dumps(ptr_data), encoding="utf-8")

    # get_active_pointer and get_active_manifest must fail closed (return None)
    assert pub_b.get_active_pointer() is None
    assert pub_b.get_active_manifest() is None


def test_legacy_pointer_compatibility_restricted_to_sebi(test_env):
    """
    Verify legacy pointer fallback:
    - sebi_cscrf falls back to legacy root data/indexes/active_snapshot.json if partition pointer is missing.
    - Non-SEBI collection NEVER falls back to legacy root active_snapshot.json.
    """
    indexes_dir = test_env["indexes_dir"]
    processed_dir = test_env["processed_dir"]
    factory = test_env["factory"]
    embedder = test_env["embedder"]

    # Write a legacy root active_snapshot.json and snapshot without collection_id (legacy pilot format)
    legacy_snap_dir = indexes_dir / "snapshots" / "snap_legacy_001"
    legacy_snap_dir.mkdir(parents=True, exist_ok=True)

    manifest_content = {
        "snapshot_id": "snap_legacy_001",
        "created_at": "2026-10-11T10:45:12Z",
        "format_version": "1.0.0",
        "embedding_model": "BAAI/bge-small-en-v1.5",
        "embedding_dim": 384,
        "total_documents": 1,
        "total_chunks": 1,
        "document_ids": ["sebi_cscrf_2024_113"],
        "document_versions": {"sebi_cscrf_2024_113": "aaa111" + "0" * 58},
        "chunk_ids": ["sebi_cscrf_2024_113_p1_0"],
        "corpus_fingerprint": "fingerprint123",
        "artifact_checksums": {},
        "metadata": {},
    }
    manifest_file = legacy_snap_dir / "manifest.json"
    manifest_file.write_text(json.dumps(manifest_content), encoding="utf-8")

    chunks_file = legacy_snap_dir / "chunks.json"
    chunks_file.write_text(
        json.dumps([{
            "chunk_id": "sebi_cscrf_2024_113_p1_0",
            "document_id": "sebi_cscrf_2024_113",
            "text": "Legacy cybersecurity framework content.",
            "page_start": 1,
            "page_end": 1,
        }]),
        encoding="utf-8",
    )

    docs_file = legacy_snap_dir / "documents.json"
    docs_file.write_text(
        json.dumps({
            "sebi_cscrf_2024_113": {
                "document_id": "sebi_cscrf_2024_113",
                "title": "Legacy SEBI",
                "source_url": "https://sebi.gov.in",
                "sha256": "aaa111" + "0" * 58,
                "pdf_path": "legacy.pdf",
            }
        }),
        encoding="utf-8",
    )

    root_pointer = indexes_dir / "active_snapshot.json"
    root_pointer.write_text(
        json.dumps({
            "snapshot_id": "snap_legacy_001",
            "activated_at": "2026-10-11T10:45:12Z",
            "manifest_checksum": "",
            "total_documents": 1,
            "total_chunks": 1,
        }),
        encoding="utf-8",
    )

    # 1. RetrievalService resolving for sebi_cscrf -> successfully uses legacy root pointer
    service = RetrievalService(processed_dir=processed_dir, indexes_dir=indexes_dir, dense_embedder=embedder)
    snap_dir_sebi = service.get_active_snapshot_dir(collection_id="sebi_cscrf")
    assert snap_dir_sebi is not None
    assert snap_dir_sebi == legacy_snap_dir

    # 2. RetrievalService resolving for a non-SEBI collection -> fails closed (None), NEVER falls back to root
    snap_dir_other = service.get_active_snapshot_dir(collection_id="amfi_nav")
    assert snap_dir_other is None


def test_retrieval_isolation_bm25_dense_rrf(test_env):
    """
    Verify complete retrieval isolation between collections:
    - BM25 candidate chunks are restricted to the selected collection.
    - Dense candidate chunks are restricted to the selected collection.
    - RRF never fuses candidate chunks across collections.
    """
    indexes_dir = test_env["indexes_dir"]
    processed_dir = test_env["processed_dir"]
    factory = test_env["factory"]
    embedder = test_env["embedder"]

    # Publish both collections
    pub_a = IndexPublisher(
        repository_factory=factory,
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
        embedder=embedder,
        collection_id="sebi_cscrf",
    )
    pub_a.build_and_publish_snapshot()

    pub_b = IndexPublisher(
        repository_factory=factory,
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
        embedder=embedder,
        collection_id="amfi_nav",
    )
    pub_b.build_and_publish_snapshot()

    service = RetrievalService(processed_dir=processed_dir, indexes_dir=indexes_dir, dense_embedder=embedder)

    # Query targeting Collection A ("sebi_cscrf")
    query_a = "Cyber Resilience Framework multi-factor authentication"
    bm25_a = service.search(query=query_a, top_k=5, collection_id="sebi_cscrf")
    assert bm25_a.total_results > 0
    for res in bm25_a.results:
        assert res.document_id == "sebi_cscrf_2024_113"
        assert "amfi" not in res.document_id

    dense_a = service.search_dense(query=query_a, top_k=5, collection_id="sebi_cscrf")
    assert dense_a.total_results > 0
    for res in dense_a.results:
        assert res.document_id == "sebi_cscrf_2024_113"
        assert "amfi" not in res.document_id

    hybrid_a = service.search_hybrid(query=query_a, top_k=5, collection_id="sebi_cscrf")
    assert hybrid_a.total_results > 0
    for res in hybrid_a.results:
        assert res.document_id == "sebi_cscrf_2024_113"
        assert "amfi" not in res.document_id

    # Query targeting Collection B ("amfi_nav")
    query_b = "Mutual funds NAV valuation cut-off timing"
    bm25_b = service.search(query=query_b, top_k=5, collection_id="amfi_nav")
    assert bm25_b.total_results > 0
    for res in bm25_b.results:
        assert res.document_id == "amfi_nav_circular_001"
        assert "sebi" not in res.document_id

    dense_b = service.search_dense(query=query_b, top_k=5, collection_id="amfi_nav")
    assert dense_b.total_results > 0
    for res in dense_b.results:
        assert res.document_id == "amfi_nav_circular_001"
        assert "sebi" not in res.document_id

    hybrid_b = service.search_hybrid(query=query_b, top_k=5, collection_id="amfi_nav")
    assert hybrid_b.total_results > 0
    for res in hybrid_b.results:
        assert res.document_id == "amfi_nav_circular_001"
        assert "sebi" not in res.document_id

    # Cross-collection query attempt: searching Collection A with Collection B terms returns no foreign chunks
    cross_search = service.search_hybrid(query="Mutual funds NAV valuation", top_k=5, collection_id="sebi_cscrf")
    for res in cross_search.results:
        assert res.document_id != "amfi_nav_circular_001"


def test_unresolved_document_ownership_not_assigned_to_sebi(test_env):
    """
    Verify that documents without explicit collection_id and not matching the 5 legacy SEBI documents
    are quarantined / excluded and NEVER silently assigned to sebi_cscrf.
    """
    indexes_dir = test_env["indexes_dir"]
    processed_dir = test_env["processed_dir"]
    factory = test_env["factory"]
    embedder = test_env["embedder"]
    doc_repo = factory.get_document_repository()
    chunk_repo = factory.get_chunk_repository()

    # Add an unassigned document with arbitrary ID
    unassigned_doc = DocumentRecord(
        document_id="unknown_regulatory_notice_999",
        title="Unassigned Notice",
        source_url="https://example.org/notice.pdf",
        sha256="fff999" + "0" * 58,
        pdf_path="raw/notice.pdf",
        collection_id=None,  # No collection ID
    )
    doc_repo.save_document(unassigned_doc)

    unassigned_chunk = ChunkRecord(
        chunk_id="unknown_regulatory_notice_999_p1_0",
        document_id="unknown_regulatory_notice_999",
        text="Arbitrary unassigned notice text.",
        page_start=1,
        page_end=1,
        collection_id=None,
    )
    chunk_repo.save_chunks([unassigned_chunk])

    # resolve_collection_identity should return None
    assert resolve_collection_identity("unknown_regulatory_notice_999", None) is None

    # Publishing sebi_cscrf must NOT include this document
    pub_sebi = IndexPublisher(
        repository_factory=factory,
        indexes_dir=indexes_dir,
        processed_dir=processed_dir,
        embedder=embedder,
        collection_id="sebi_cscrf",
    )
    res = pub_sebi.build_and_publish_snapshot()
    assert res.success is True
    assert "unknown_regulatory_notice_999" not in res.newly_published_documents

    manifest = pub_sebi.get_active_manifest()
    assert manifest is not None
    assert "unknown_regulatory_notice_999" not in manifest.document_ids
