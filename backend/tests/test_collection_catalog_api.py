"""
Tests for Phase 3.1: Collection Catalog API (GET /api/v1/collections).

Verifies:
1. Response schema and HTTP 200 status for queryable collections.
2. Inclusion of valid queryable collections (sebi_cscrf, amfi_nav / general_synthetic).
3. Exclusion of disabled collections.
4. Exclusion of unpublished collections (missing active snapshot pointer or directory).
5. Deterministic sorting / stable ordering of results.
6. Real repository integration returns both sebi_cscrf and general_synthetic when active.
"""
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.api.v1.query import get_collection_registry, get_retrieval_service
from app.schemas.manifest import CollectionStatus, GroundingPolicyType
from app.ingestion.collection_registry import CollectionRegistry


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def catalog_test_env(tmp_path):
    """Sets up a controlled test directory with various collection states."""
    manifests_dir = tmp_path / "manifests"
    indexes_dir = tmp_path / "indexes"
    manifests_dir.mkdir(parents=True)
    indexes_dir.mkdir(parents=True)

    # 1. Active sebi_cscrf with active_snapshot.json and snapshot directory
    sebi_dir = indexes_dir / "sebi_cscrf"
    sebi_snap = sebi_dir / "snapshots" / "snap_sebi_001"
    sebi_snap.mkdir(parents=True)
    (sebi_snap / "manifest.json").write_text(json.dumps({"snapshot_id": "snap_sebi_001"}), encoding="utf-8")
    (sebi_dir / "active_snapshot.json").write_text(
        json.dumps({"snapshot_id": "snap_sebi_001", "collection_id": "sebi_cscrf"}),
        encoding="utf-8",
    )
    sebi_manifest = {
        "collection_id": "sebi_cscrf",
        "name": "SEBI CSCRF Regulatory Intelligence",
        "description": "SEBI cybersecurity master circulars",
        "domain": "CSCRF",
        "regulator": "SEBI",
        "enabled": True,
        "policy": {
            "policy_type": "sebi_cscrf_strict",
            "enforce_regulatory_graph": True,
            "enforce_verbatim_citation": True,
            "enforce_retrieval_binding": True,
        },
    }
    (manifests_dir / "sebi_cscrf.json").write_text(json.dumps(sebi_manifest), encoding="utf-8")

    # 2. Active general collection 'general_synthetic' with published snapshot
    gen_dir = indexes_dir / "general_synthetic"
    gen_snap = gen_dir / "snapshots" / "snap_gen_001"
    gen_snap.mkdir(parents=True)
    (gen_snap / "manifest.json").write_text(json.dumps({"snapshot_id": "snap_gen_001"}), encoding="utf-8")
    (gen_dir / "active_snapshot.json").write_text(
        json.dumps({"snapshot_id": "snap_gen_001", "collection_id": "general_synthetic"}),
        encoding="utf-8",
    )
    gen_manifest = {
        "collection_id": "general_synthetic",
        "name": "Acme Technical Handbook",
        "description": "General systems documentation",
        "domain": "TECH",
        "regulator": None,
        "enabled": True,
        "policy": {
            "policy_type": "general_static",
            "enforce_regulatory_graph": False,
            "enforce_verbatim_citation": True,
            "enforce_retrieval_binding": True,
        },
    }
    (manifests_dir / "general_synthetic.json").write_text(json.dumps(gen_manifest), encoding="utf-8")

    # 3. Disabled collection 'disabled_coll'
    dis_dir = indexes_dir / "disabled_coll"
    dis_snap = dis_dir / "snapshots" / "snap_dis_001"
    dis_snap.mkdir(parents=True)
    (dis_dir / "active_snapshot.json").write_text(
        json.dumps({"snapshot_id": "snap_dis_001", "collection_id": "disabled_coll"}),
        encoding="utf-8",
    )
    dis_manifest = {
        "collection_id": "disabled_coll",
        "name": "Disabled Document Base",
        "description": "Should never appear in catalog",
        "domain": "DIS",
        "enabled": False,
        "policy": {
            "policy_type": "general_static",
            "enforce_regulatory_graph": False,
            "enforce_verbatim_citation": True,
            "enforce_retrieval_binding": True,
        },
    }
    (manifests_dir / "disabled_coll.json").write_text(json.dumps(dis_manifest), encoding="utf-8")

    # 4. Unpublished collection 'unpub_coll' (no active snapshot at all)
    unpub_manifest = {
        "collection_id": "unpub_coll",
        "name": "Unpublished Collection",
        "description": "No published index snapshot exists",
        "domain": "UNPUB",
        "enabled": True,
        "policy": {
            "policy_type": "general_static",
            "enforce_regulatory_graph": False,
            "enforce_verbatim_citation": True,
            "enforce_retrieval_binding": True,
        },
    }
    (manifests_dir / "unpub_coll.json").write_text(json.dumps(unpub_manifest), encoding="utf-8")

    # 5. Broken snapshot collection 'broken_snap_coll' (active_snapshot points to missing folder)
    broken_dir = indexes_dir / "broken_snap_coll"
    broken_dir.mkdir(parents=True)
    (broken_dir / "active_snapshot.json").write_text(
        json.dumps({"snapshot_id": "snap_missing_999", "collection_id": "broken_snap_coll"}),
        encoding="utf-8",
    )
    broken_manifest = {
        "collection_id": "broken_snap_coll",
        "name": "Broken Snapshot Collection",
        "description": "Active pointer exists but snapshot dir is missing",
        "domain": "BROKEN",
        "enabled": True,
        "policy": {
            "policy_type": "general_static",
            "enforce_regulatory_graph": False,
            "enforce_verbatim_citation": True,
            "enforce_retrieval_binding": True,
        },
    }
    (manifests_dir / "broken_snap_coll.json").write_text(json.dumps(broken_manifest), encoding="utf-8")

    registry = CollectionRegistry(manifests_dir=manifests_dir, indexes_dir=indexes_dir)

    mock_retrieval = MagicMock()
    # Mock get_active_snapshot_dir matching filesystem reality
    def mock_get_active_snap(cid):
        if cid == "sebi_cscrf":
            return sebi_snap
        if cid == "general_synthetic":
            return gen_snap
        if cid == "disabled_coll":
            return dis_snap
        return None

    mock_retrieval.get_active_snapshot_dir.side_effect = mock_get_active_snap

    return registry, mock_retrieval


def test_get_collections_schema_and_filtering(client, catalog_test_env):
    """GET /api/v1/collections returns 200 and excludes disabled/unpublished/broken collections."""
    registry, mock_retrieval = catalog_test_env

    app.dependency_overrides[get_collection_registry] = lambda: registry
    app.dependency_overrides[get_retrieval_service] = lambda: mock_retrieval

    try:
        response = client.get("/api/v1/collections")
        assert response.status_code == 200
        data = response.json()

        assert isinstance(data, list)
        assert len(data) == 2

        cids = [item["collection_id"] for item in data]
        # Deterministic sorting
        assert cids == ["general_synthetic", "sebi_cscrf"]

        # Validate schema fields
        gen = next(item for item in data if item["collection_id"] == "general_synthetic")
        assert gen["name"] == "Acme Technical Handbook"
        assert gen["description"] == "General systems documentation"
        assert gen["domain"] == "TECH"
        assert gen["regulator"] is None
        assert gen["policy_type"] == GroundingPolicyType.GENERAL_STATIC.value
        assert gen["status"] == CollectionStatus.ACTIVE.value

        sebi = next(item for item in data if item["collection_id"] == "sebi_cscrf")
        assert sebi["name"] == "SEBI CSCRF Regulatory Intelligence"
        assert sebi["domain"] == "CSCRF"
        assert sebi["regulator"] == "SEBI"
        assert sebi["policy_type"] == GroundingPolicyType.SEBI_CSCRF_STRICT.value
        assert sebi["status"] == CollectionStatus.ACTIVE.value

        # Confirm non-queryable collections are completely omitted
        assert "disabled_coll" not in cids
        assert "unpub_coll" not in cids
        assert "broken_snap_coll" not in cids
    finally:
        app.dependency_overrides.clear()


def test_get_collections_empty_when_no_active_collections(client, tmp_path):
    """When no collections have active published snapshots, returns empty list without error."""
    manifests_dir = tmp_path / "manifests"
    indexes_dir = tmp_path / "indexes"
    manifests_dir.mkdir(parents=True)
    indexes_dir.mkdir(parents=True)

    registry = CollectionRegistry(manifests_dir=manifests_dir, indexes_dir=indexes_dir)
    mock_retrieval = MagicMock()
    mock_retrieval.get_active_snapshot_dir.return_value = None

    app.dependency_overrides[get_collection_registry] = lambda: registry
    app.dependency_overrides[get_retrieval_service] = lambda: mock_retrieval

    try:
        response = client.get("/api/v1/collections")
        assert response.status_code == 200
        assert response.json() == []
    finally:
        app.dependency_overrides.clear()


def test_get_collections_live_repository_integration(client):
    """Verifies live repository data/manifests and data/indexes return expected collections."""
    response = client.get("/api/v1/collections")
    assert response.status_code == 200
    data = response.json()
    cids = [c["collection_id"] for c in data]

    # All three collections are actively tracked and have valid published snapshots
    assert "sebi_cscrf" in cids
    assert "general_synthetic" in cids
    assert "demo_finops" in cids

    finops = next(c for c in data if c["collection_id"] == "demo_finops")
    assert finops["name"] == "Acme Cloud FinOps & Governance Guide"
    assert finops["domain"] == "FINOPS"
    assert finops["policy_type"] == GroundingPolicyType.GENERAL_STATIC.value
    assert finops["status"] == CollectionStatus.ACTIVE.value
