"""
Tests for Phase 3.1: Collection Catalog API (GET /api/v1/collections).

Verifies:
1. Response schema and HTTP 200 status for queryable collections.
2. Inclusion of valid queryable collections (general_synthetic, demo_finops, cisa_playbooks).
3. Exclusion of disabled collections.
4. Exclusion of unpublished collections (missing active snapshot pointer or directory).
5. Deterministic sorting / stable ordering of results.
6. Real repository integration returns active collections.
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

    # 1. Active cisa_playbooks with active_snapshot.json and snapshot directory
    cisa_dir = indexes_dir / "cisa_playbooks"
    cisa_snap = cisa_dir / "snapshots" / "snap_cisa_001"
    cisa_snap.mkdir(parents=True)
    (cisa_snap / "manifest.json").write_text(json.dumps({"snapshot_id": "snap_cisa_001"}), encoding="utf-8")
    (cisa_dir / "active_snapshot.json").write_text(
        json.dumps({"snapshot_id": "snap_cisa_001", "collection_id": "cisa_playbooks"}),
        encoding="utf-8",
    )
    cisa_manifest = {
        "collection_id": "cisa_playbooks",
        "name": "CISA Cybersecurity Playbooks",
        "description": "CISA incident response and vulnerability playbooks",
        "domain": "CYBER",
        "regulator": None,
        "enabled": True,
        "policy": {
            "policy_type": "general_static",
            "enforce_regulatory_graph": False,
            "enforce_verbatim_citation": True,
            "enforce_retrieval_binding": True,
        },
    }
    (manifests_dir / "cisa_playbooks.json").write_text(json.dumps(cisa_manifest), encoding="utf-8")

    # 2. Active general_synthetic with active_snapshot.json and snapshot directory
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

    # 3. Disabled collection
    dis_manifest = {
        "collection_id": "disabled_coll",
        "name": "Disabled Collection",
        "description": "Not active",
        "domain": "TEST",
        "regulator": None,
        "enabled": False,
        "policy": {
            "policy_type": "general_static",
            "enforce_regulatory_graph": False,
            "enforce_verbatim_citation": True,
            "enforce_retrieval_binding": True,
        },
    }
    (manifests_dir / "disabled_coll.json").write_text(json.dumps(dis_manifest), encoding="utf-8")

    # 4. Unpublished collection (has manifest but no active snapshot pointer)
    unpub_manifest = {
        "collection_id": "unpub_coll",
        "name": "Unpublished Collection",
        "description": "Manifest only",
        "domain": "TEST",
        "regulator": None,
        "enabled": True,
        "policy": {
            "policy_type": "general_static",
            "enforce_regulatory_graph": False,
            "enforce_verbatim_citation": True,
            "enforce_retrieval_binding": True,
        },
    }
    (manifests_dir / "unpub_coll.json").write_text(json.dumps(unpub_manifest), encoding="utf-8")

    # 5. Broken snapshot pointer (active_snapshot.json points to non-existent dir)
    broken_dir = indexes_dir / "broken_snap_coll"
    broken_dir.mkdir(parents=True)
    (broken_dir / "active_snapshot.json").write_text(
        json.dumps({"snapshot_id": "snap_ghost", "collection_id": "broken_snap_coll"}),
        encoding="utf-8",
    )
    broken_manifest = {
        "collection_id": "broken_snap_coll",
        "name": "Broken Snapshot Collection",
        "description": "Points to missing snapshot",
        "domain": "TEST",
        "regulator": None,
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

    # Mock retrieval service returns snapshot dir only for valid active snapshots
    mock_retrieval = MagicMock()

    def mock_get_active_snap(coll_id):
        if coll_id == "cisa_playbooks":
            return cisa_snap
        if coll_id == "general_synthetic":
            return gen_snap
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
        assert cids == ["cisa_playbooks", "general_synthetic"]

        # Validate schema fields
        gen = next(item for item in data if item["collection_id"] == "general_synthetic")
        assert gen["name"] == "Acme Technical Handbook"
        assert gen["description"] == "General systems documentation"
        assert gen["domain"] == "TECH"
        assert gen["regulator"] is None
        assert gen["policy_type"] == GroundingPolicyType.GENERAL_STATIC.value
        assert gen["status"] == CollectionStatus.ACTIVE.value

        cisa = next(item for item in data if item["collection_id"] == "cisa_playbooks")
        assert cisa["name"] == "CISA Cybersecurity Playbooks"
        assert cisa["domain"] == "CYBER"
        assert cisa["regulator"] is None
        assert cisa["policy_type"] == GroundingPolicyType.GENERAL_STATIC.value
        assert cisa["status"] == CollectionStatus.ACTIVE.value

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

    assert "sebi_cscrf" not in cids
    assert "general_synthetic" in cids
    assert "demo_finops" in cids
    assert "cisa_playbooks" in cids

    finops = next(c for c in data if c["collection_id"] == "demo_finops")
    assert finops["name"] == "Acme Cloud FinOps & Governance Guide"
    assert finops["domain"] == "FINOPS"
    assert finops["policy_type"] == GroundingPolicyType.GENERAL_STATIC.value
    assert finops["status"] == CollectionStatus.ACTIVE.value
