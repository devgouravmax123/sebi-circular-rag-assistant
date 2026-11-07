"""
Models package initialization exporting all declarative persistence models.
"""
from app.models.auth import UserModel, WorkspaceModel, WorkspaceMembershipModel
from app.models.collection import CollectionModel
from app.persistence.models import (
    Base,
    RegulatorModel,
    RegulatoryDomainModel,
    RegulatoryDocumentModel,
    DocumentVersionModel,
    ProvisionModel,
    ChunkModel,
    ChunkEmbeddingModel,
    RegulatoryEdgeModel,
    IngestionJobModel,
)

__all__ = [
    "Base",
    "UserModel",
    "WorkspaceModel",
    "WorkspaceMembershipModel",
    "CollectionModel",
    "RegulatorModel",
    "RegulatoryDomainModel",
    "RegulatoryDocumentModel",
    "DocumentVersionModel",
    "ProvisionModel",
    "ChunkModel",
    "ChunkEmbeddingModel",
    "RegulatoryEdgeModel",
    "IngestionJobModel",
]

