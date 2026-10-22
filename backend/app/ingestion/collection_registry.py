"""
Collection Registry abstraction for managing, resolving, and validating document collections.

Provides safe discovery and resolution of declarative collection manifests from data/manifests/:
- Loads and validates manifests against CollectionManifest schema.
- Rejects duplicate, conflicting, or path-traversal collection IDs.
- Distinguishes unknown collections from known collections that are disabled or unindexed.
- Exposes collection metadata and GroundingPolicyDeclaration without instantiating graph objects.
"""
import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Set

from app.schemas.manifest import (
    CollectionManifest,
    CollectionStatus,
    GroundingPolicyDeclaration,
    GroundingPolicyType,
)

logger = logging.getLogger(__name__)


class CollectionRegistryError(Exception):
    """Base exception for collection registry operations."""
    pass


class UnknownCollectionError(CollectionRegistryError):
    """Raised when a requested collection_id is not registered."""
    pass


class DuplicateCollectionError(CollectionRegistryError):
    """Raised when multiple manifest files declare conflicting or identical collection IDs."""
    pass


class InvalidCollectionError(CollectionRegistryError):
    """Raised when a manifest file is malformed, structurally invalid, or fails security checks."""
    pass


class CollectionRegistry:
    """
    In-memory registry that discovers and indexes declarative collection manifests.
    """

    def __init__(
        self,
        manifests_dir: str | Path = "data/manifests",
        indexes_dir: str | Path = "data/indexes",
    ):
        self.manifests_dir = Path(manifests_dir).resolve()
        self.indexes_dir = Path(indexes_dir).resolve()
        self._collections: Dict[str, CollectionManifest] = {}
        self._manifest_paths: Dict[str, Path] = {}
        self.reload()

    def reload(self) -> int:
        """
        Scan manifests_dir, load, and index all valid collection manifests.
        Enforces unique collection IDs and detects conflicts.
        """
        self._collections.clear()
        self._manifest_paths.clear()

        if not self.manifests_dir.exists() or not self.manifests_dir.is_dir():
            return 0

        # Deterministically scan .json files in manifests_dir
        manifest_files = sorted(list(self.manifests_dir.glob("*.json")))

        for mf in manifest_files:
            # Traversal / safety check: file must be strictly within manifests_dir
            try:
                resolved_mf = mf.resolve()
                if self.manifests_dir not in resolved_mf.parents and resolved_mf != self.manifests_dir:
                    logger.warning(f"Skipping manifest outside approved manifests_dir: {mf}")
                    continue
            except Exception:
                continue

            try:
                with open(mf, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
            except Exception as e:
                raise InvalidCollectionError(f"Malformed JSON in collection manifest '{mf.name}': {e}") from e

            try:
                manifest = CollectionManifest(**raw_data)
            except Exception as e:
                raise InvalidCollectionError(f"Validation error in collection manifest '{mf.name}': {e}") from e

            coll_id = manifest.collection_id
            if coll_id in self._collections:
                existing_path = self._manifest_paths[coll_id]
                raise DuplicateCollectionError(
                    f"Duplicate collection ID '{coll_id}' found in '{mf.name}'; already defined in '{existing_path.name}'."
                )

            self._collections[coll_id] = manifest
            self._manifest_paths[coll_id] = mf

        return len(self._collections)

    def list_collections(self) -> List[CollectionManifest]:
        """Return all registered collection manifests sorted deterministically by collection_id."""
        return [self._collections[cid] for cid in sorted(self._collections.keys())]

    def list_collection_ids(self) -> List[str]:
        """Return list of all registered collection IDs."""
        return sorted(list(self._collections.keys()))

    def get_collection(self, collection_id: str) -> CollectionManifest:
        """
        Retrieve a registered collection manifest by ID.
        Raises UnknownCollectionError if not found.
        """
        cid = collection_id.strip()
        if cid not in self._collections:
            raise UnknownCollectionError(
                f"Collection '{cid}' is not registered. Registered collections: {self.list_collection_ids()}"
            )
        return self._collections[cid]

    def has_collection(self, collection_id: str) -> bool:
        """Check if a collection ID is registered."""
        return collection_id.strip() in self._collections

    def get_collection_status(self, collection_id: str) -> CollectionStatus:
        """
        Determines the operational status of a registered collection:
        - DISABLED: declared enabled=False in manifest
        - UNPUBLISHED: no active index snapshot found under indexes_dir (or collection partition)
        - ACTIVE: enabled and has published index snapshot
        """
        manifest = self.get_collection(collection_id)
        if not manifest.enabled:
            return CollectionStatus.DISABLED

        # Check for active index snapshot:
        # 1. Per-collection partition: data/indexes/{collection_id}/active_snapshot.json
        # 2. Legacy root snapshot: data/indexes/active_snapshot.json (if collection_id == 'sebi_cscrf')
        coll_snapshot = self.indexes_dir / collection_id / "active_snapshot.json"
        root_snapshot = self.indexes_dir / "active_snapshot.json"

        if coll_snapshot.exists():
            return CollectionStatus.ACTIVE
        if collection_id == "sebi_cscrf" and root_snapshot.exists():
            return CollectionStatus.ACTIVE

        return CollectionStatus.UNPUBLISHED

    def get_grounding_policy(self, collection_id: str) -> GroundingPolicyDeclaration:
        """
        Exposes the configured grounding policy declaration for the collection.
        Does NOT instantiate or parse graph objects.
        """
        manifest = self.get_collection(collection_id)
        return manifest.policy

    def register_manifest_in_memory(self, manifest: CollectionManifest, source_name: str = "in_memory") -> None:
        """Directly register a manifest object in memory (useful for testing and dynamic runners)."""
        coll_id = manifest.collection_id
        if coll_id in self._collections:
            existing = self._manifest_paths.get(coll_id, "in_memory")
            raise DuplicateCollectionError(f"Duplicate collection ID '{coll_id}'; already registered by '{existing}'.")
        self._collections[coll_id] = manifest
        self._manifest_paths[coll_id] = Path(source_name)
