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
    LEGACY_SEBI_DOCUMENTS,
    resolve_collection_identity,
)
from app.schemas.cscrf_graph_data import create_verified_cscrf_graph


@pytest.fixture
def repo_root() -> Path:
    """Resolve repository root."""
    return Path(__file__).resolve().parent.parent.parent


def test_load_existing_sebi_cscrf_manifest(repo_root: Path):
    """Verify loading real repository manifest data/manifests/sebi_cscrf.json."""
    manifests_dir = repo_root / "data" / "manifests"
    indexes_dir = repo_root / "data" / "indexes"
    assert manifests_dir.exists(), "data/manifests directory must exist"

    registry = CollectionRegistry(manifests_dir=manifests_dir, indexes_dir=indexes_dir)
    assert registry.has_collection("sebi_cscrf")

    manifest = registry.get_collection("sebi_cscrf")
    assert manifest.collection_id == "sebi_cscrf"
    assert manifest.name == "SEBI Cybersecurity and Cyber Resilience Framework"
    assert manifest.regulator == "SEBI"
    assert manifest.domain == "CSCRF"
    assert manifest.enforce_regulatory_graph is True
    assert len(manifest.documents) == 5

    # Policy declaration check
    policy = registry.get_grounding_policy("sebi_cscrf")
    assert policy.policy_type == GroundingPolicyType.SEBI_CSCRF_STRICT
    assert policy.enforce_regulatory_graph is True
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


def test_legacy_sebi_document_mapping_and_backward_compatibility():
    """Verify explicit mapping of 5 SEBI circulars and rejection of ambiguous assignment."""
    # 1. Five canonical SEBI documents map deterministically
    for doc_id in LEGACY_SEBI_DOCUMENTS:
        resolved = resolve_collection_identity(doc_id)
        assert resolved == "sebi_cscrf", f"Legacy document {doc_id} must resolve to sebi_cscrf"

    # 2. Arbitrary missing document does NOT resolve to sebi_cscrf
    unknown_doc_id = "rbi_kyc_master_direction_2024"
    assert resolve_collection_identity(unknown_doc_id) is None

    # 3. Explicitly declared collection ID always takes precedence
    custom_resolved = resolve_collection_identity(unknown_doc_id, declared_collection_id="rbi_banking")
    assert custom_resolved == "rbi_banking"

    # 4. Explicit override even on a legacy SEBI ID if intentionally reassigned
    override = resolve_collection_identity("sebi_cscrf_2024_113", declared_collection_id="custom_collection")
    assert override == "custom_collection"


def test_backward_compatibility_with_legacy_serialized_records():
    """Legacy JSON records without collection_id parse cleanly into DocumentRecord and ChunkRecord."""
    legacy_doc_json = {
        "document_id": "sebi_cscrf_2024_113",
        "circular_number": "SEBI/HO/ITD-1/ITD_CSC_EXT/P/CIR/2024/113",
        "title": "Cybersecurity and Cyber Resilience Framework (CSCRF)",
        "source_url": "https://www.sebi.gov.in/legal/circulars/aug-2024/cscrf.html",
        "sha256": "bd9ddb68bb49b9a92771ff01ed3138e01f0962b383e9ff643008b729290fc85d",
        "pdf_path": "data/raw/CSCRF_2024_113.pdf",
    }
    doc_record = DocumentRecord(**legacy_doc_json)
    assert doc_record.document_id == "sebi_cscrf_2024_113"
    assert doc_record.collection_id is None
    # Resolution helper confirms controlled compatibility
    assert resolve_collection_identity(doc_record.document_id, doc_record.collection_id) == "sebi_cscrf"

    legacy_chunk_json = {
        "chunk_id": "sebi_cscrf_2024_113_p1_0",
        "document_id": "sebi_cscrf_2024_113",
        "text": "Cybersecurity framework provisions...",
        "page_start": 1,
        "page_end": 1,
    }
    chunk_record = ChunkRecord(**legacy_chunk_json)
    assert chunk_record.chunk_id == "sebi_cscrf_2024_113_p1_0"
    assert chunk_record.collection_id is None


def test_verified_sebi_graph_fixtures_preserved():
    """Confirm verified SEBI CSCRF relationship graph fixtures remain 100% intact."""
    graph = create_verified_cscrf_graph()
    assert graph is not None
    # Total verified edges in verified graph
    assert len(graph.edges) == 9

    # Check that edges involve the 5 verified SEBI documents
    involved_docs = {e.source_document_id for e in graph.edges.values()} | {e.target_document_id for e in graph.edges.values()}
    assert involved_docs.issubset(LEGACY_SEBI_DOCUMENTS)

    # Check known edges
    timeline_amendments = [
        e for e in graph.get_outgoing_edges("sebi_cscrf_2025_045")
        if e.target_document_id == "sebi_cscrf_2024_113"
    ]
    assert len(timeline_amendments) >= 2


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
