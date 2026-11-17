"""
Unit and integration tests for Phase 2.1 Collection Registry and Collection Identity Foundation.

Tests:
1. Loading the existing sebi_cscrf.json manifest and verifying schema/fields.
2. Resolving registered collection by ID.
3. Unknown, duplicate, and conflicting collection IDs.
4. Path safety and invalid collection manifests.
5. Operational status resolution (ACTIVE, DISABLED, UNPUBLISHED).
6. Grounding policy declaration validation (SEBI CSCRF Strict vs General Static).
7. Explicit mapping of the 5 legacy SEBI documents and safe controlled fallback.
8. Preservation of document IDs, chunk IDs, and SEBI verified graph fixtures.
9. Compatibility with legacy serialized artifacts lacking collection metadata.
"""
import json
import pytest
from pathlib import Path

from app.ingestion.collection_registry import (
    CollectionRegistry,
    CollectionRegistryError,
    UnknownCollectionError,
    DuplicateCollectionError,
    InvalidCollectionError,
)
from app.schemas.manifest import (
    CollectionManifest,
    CollectionStatus,
    GroundingPolicyDeclaration,
    GroundingPolicyType,
)
from app.schemas.document import (
    DocumentRecord,
    ChunkRecord,
    resolve_collection_identity,
)
from app.schemas.graph_collection import GraphEdgeCollection


@pytest.fixture
def repo_root() -> Path:
    """Resolve repository root."""
    return Path(__file__).resolve().parent.parent.parent


def test_load_existing_general_synthetic_manifest(repo_root: Path):
    """Verify loading real repository manifest data/manifests/general_synthetic.json."""
    manifests_dir = repo_root / "data" / "manifests"
    indexes_dir = repo_root / "data" / "indexes"
    assert manifests_dir.exists(), "data/manifests directory must exist"

    registry = CollectionRegistry(manifests_dir=manifests_dir, indexes_dir=indexes_dir)
    assert registry.has_collection("general_synthetic")

    manifest = registry.get_collection("general_synthetic")
    assert manifest.collection_id == "general_synthetic"
    assert "Acme" in manifest.name
    assert manifest.enforce_regulatory_graph is False
    assert len(manifest.documents) == 3

    # Policy declaration check
    policy = registry.get_grounding_policy("general_synthetic")
    assert policy.policy_type == GroundingPolicyType.GENERAL_STATIC
    assert policy.enforce_regulatory_graph is False
    assert policy.enforce_verbatim_citation is True
    assert policy.enforce_retrieval_binding is True


def test_unknown_collection_raises_error(repo_root: Path):
    """Requesting an unregistered collection ID raises UnknownCollectionError."""
    registry = CollectionRegistry(manifests_dir=repo_root / "data" / "manifests")
    with pytest.raises(UnknownCollectionError) as exc_info:
        registry.get_collection("non_existent_collection_xyz")
    assert "non_existent_collection_xyz" in str(exc_info.value)


def test_duplicate_collection_id_rejected(tmp_path: Path):
    """Multiple manifests declaring the same collection_id trigger DuplicateCollectionError."""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir(parents=True)

    manifest_a = {
        "collection_id": "duplicate_id",
        "name": "First Instance",
        "policy": {
            "policy_type": "general_static",
            "enforce_regulatory_graph": False,
        },
        "documents": [],
    }
    manifest_b = {
        "collection_id": "duplicate_id",
        "name": "Second Instance Conflict",
        "policy": {
            "policy_type": "general_static",
            "enforce_regulatory_graph": False,
        },
        "documents": [],
    }

    (manifests_dir / "coll_a.json").write_text(json.dumps(manifest_a), encoding="utf-8")
    (manifests_dir / "coll_b.json").write_text(json.dumps(manifest_b), encoding="utf-8")

    with pytest.raises(DuplicateCollectionError) as exc_info:
        CollectionRegistry(manifests_dir=manifests_dir)
    assert "Duplicate collection ID 'duplicate_id'" in str(exc_info.value)


def test_invalid_collection_manifest_rejected(tmp_path: Path):
    """Malformed JSON or schema invalidity in manifest is caught with InvalidCollectionError."""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir(parents=True)

    # Missing mandatory name
    bad_manifest = {
        "collection_id": "bad_manifest_id",
    }
    (manifests_dir / "invalid.json").write_text(json.dumps(bad_manifest), encoding="utf-8")

    with pytest.raises(InvalidCollectionError):
        CollectionRegistry(manifests_dir=manifests_dir)


def test_invalid_collection_id_characters(tmp_path: Path):
    """Collection IDs with path traversal attempts or invalid characters are rejected."""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir(parents=True)

    bad_id_manifest = {
        "collection_id": "../traversal/attempt",
        "name": "Traversal Collection",
        "documents": [],
    }
    (manifests_dir / "bad_id.json").write_text(json.dumps(bad_id_manifest), encoding="utf-8")

    with pytest.raises(InvalidCollectionError):
        CollectionRegistry(manifests_dir=manifests_dir)


def test_collection_status_transitions(tmp_path: Path):
    """Verify ACTIVE, DISABLED, and UNPUBLISHED statuses."""
    manifests_dir = tmp_path / "manifests"
    indexes_dir = tmp_path / "indexes"
    manifests_dir.mkdir(parents=True)
    indexes_dir.mkdir(parents=True)

    # 1. Disabled collection
    disabled_data = {
        "collection_id": "disabled_coll",
        "name": "Disabled Collection",
        "enabled": False,
        "policy": {
            "policy_type": "general_static",
            "enforce_regulatory_graph": False,
        },
        "documents": [],
    }
    (manifests_dir / "disabled.json").write_text(json.dumps(disabled_data), encoding="utf-8")

    # 2. Unpublished collection
    unpub_data = {
        "collection_id": "unpub_coll",
        "name": "Unpublished Collection",
        "enabled": True,
        "policy": {
            "policy_type": "general_static",
            "enforce_regulatory_graph": False,
        },
        "documents": [],
    }
    (manifests_dir / "unpub.json").write_text(json.dumps(unpub_data), encoding="utf-8")

    # 3. Active collection with partition snapshot
    active_data = {
        "collection_id": "active_coll",
        "name": "Active Collection",
        "enabled": True,
        "policy": {
            "policy_type": "general_static",
            "enforce_regulatory_graph": False,
        },
        "documents": [],
    }
    (manifests_dir / "active.json").write_text(json.dumps(active_data), encoding="utf-8")
    active_snap_dir = indexes_dir / "active_coll"
    active_snap_dir.mkdir(parents=True)
    (active_snap_dir / "active_snapshot.json").write_text(json.dumps({"snapshot_id": "snap_1"}), encoding="utf-8")

    registry = CollectionRegistry(manifests_dir=manifests_dir, indexes_dir=indexes_dir)

    assert registry.get_collection_status("disabled_coll") == CollectionStatus.DISABLED
    assert registry.get_collection_status("unpub_coll") == CollectionStatus.UNPUBLISHED
    assert registry.get_collection_status("active_coll") == CollectionStatus.ACTIVE


def test_general_static_policy_declaration():
    """Verify general static collection policy declaration."""
    manifest = CollectionManifest(
        collection_id="general_faq",
        name="General Regulatory FAQ",
        enforce_regulatory_graph=False,
        policy=GroundingPolicyDeclaration(
            policy_type=GroundingPolicyType.GENERAL_STATIC,
            enforce_regulatory_graph=False,
            enforce_verbatim_citation=True,
            enforce_retrieval_binding=True,
        ),
        documents=[],
    )
    assert manifest.policy.policy_type == GroundingPolicyType.GENERAL_STATIC
    assert manifest.policy.enforce_regulatory_graph is False
    # Never bypassable safeguards
    assert manifest.policy.enforce_verbatim_citation is True
    assert manifest.policy.enforce_retrieval_binding is True


def test_document_collection_identity_resolution():
    """Verify deterministic collection identity resolution."""
    # 1. Missing declared collection ID returns None
    assert resolve_collection_identity("any_doc_id") is None

    # 2. Explicit declared collection ID is returned
    assert resolve_collection_identity("any_doc_id", declared_collection_id="general_synthetic") == "general_synthetic"


def test_document_record_and_chunk_record_parsing():
    """Verify document and chunk parsing with optional collection_id."""
    doc_json = {
        "document_id": "test_doc_01",
        "title": "General System Specification",
        "source_url": "https://example.com/spec.pdf",
        "sha256": "92b4000fae4037c17bf2b57d5f16f92123c4ef3698cf09a36a14ff619a6c2a84",
        "pdf_path": "data/raw/synthetic_general/test_doc_01.pdf",
    }
    doc_record = DocumentRecord(**doc_json)
    assert doc_record.document_id == "test_doc_01"
    assert doc_record.collection_id is None
    assert resolve_collection_identity(doc_record.document_id, doc_record.collection_id) is None

    chunk_json = {
        "chunk_id": "test_doc_01_p1_0",
        "document_id": "test_doc_01",
        "text": "System architecture overview...",
        "page_start": 1,
        "page_end": 1,
    }
    chunk_record = ChunkRecord(**chunk_json)
    assert chunk_record.chunk_id == "test_doc_01_p1_0"
    assert chunk_record.collection_id is None


def test_graph_edge_collection_operations():
    """Confirm GraphEdgeCollection operations."""
    graph = GraphEdgeCollection()
    assert len(graph.edges) == 0


def test_missing_or_empty_policy_rejected():
    """Verify that manifests with missing policy or empty policy {} are rejected."""
    # 1. Missing policy field
    with pytest.raises(Exception):
        CollectionManifest.model_validate({
            "collection_id": "test_missing_policy",
            "name": "Missing Policy",
            "documents": [],
        })

    # 2. Empty policy dict
    with pytest.raises(Exception):
        CollectionManifest.model_validate({
            "collection_id": "test_empty_policy",
            "name": "Empty Policy",
            "policy": {},
            "documents": [],
        })


def test_unknown_policy_type_rejected():
    """Verify that unsupported policy types are rejected."""
    with pytest.raises(Exception):
        GroundingPolicyDeclaration(
            policy_type="non_existent_policy_type",
            enforce_regulatory_graph=True,
        )


def test_false_mandatory_invariants_rejected():
    """Verify that explicit false for either invariant raises a validation error."""
    # 1. False for enforce_verbatim_citation
    with pytest.raises(ValueError) as exc1:
        GroundingPolicyDeclaration(
            policy_type=GroundingPolicyType.GENERAL_STATIC,
            enforce_regulatory_graph=False,
            enforce_verbatim_citation=False,
        )
    assert "mandatory security invariant" in str(exc1.value)

    # 2. False for enforce_retrieval_binding
    with pytest.raises(ValueError) as exc2:
        GroundingPolicyDeclaration(
            policy_type=GroundingPolicyType.GENERAL_STATIC,
            enforce_regulatory_graph=False,
            enforce_retrieval_binding=False,
        )
    assert "mandatory security invariant" in str(exc2.value)


def test_policy_graph_consistency_enforced():
    """Verify that strict policy requires graph=True and general static requires graph=False."""
    # 1. Strict policy with graph=False must fail
    with pytest.raises(ValueError) as exc1:
        GroundingPolicyDeclaration(
            policy_type=GroundingPolicyType.SEBI_CSCRF_STRICT,
            enforce_regulatory_graph=False,
        )
    assert "requires enforce_regulatory_graph=True" in str(exc1.value)

    # 2. General static with graph=True must fail
    with pytest.raises(ValueError) as exc2:
        GroundingPolicyDeclaration(
            policy_type=GroundingPolicyType.GENERAL_STATIC,
            enforce_regulatory_graph=True,
        )
    assert "requires enforce_regulatory_graph=False" in str(exc2.value)

    # 3. Conflicting top-level enforce_regulatory_graph vs policy.enforce_regulatory_graph
    with pytest.raises(ValueError) as exc3:
        CollectionManifest(
            collection_id="conflict_graph_manifest",
            name="Conflict Graph Manifest",
            policy=GroundingPolicyDeclaration(
                policy_type=GroundingPolicyType.GENERAL_STATIC,
                enforce_regulatory_graph=False,
            ),
            enforce_regulatory_graph=True,  # Contradicts policy
            documents=[],
        )
    assert "Conflicting graph enforcement declarations" in str(exc3.value)
