"""
PostgreSQL + pgvector storage repository implementation using SQLAlchemy 2.x.
Provides thread-safe engine and session lifecycle management, parameterized queries,
explicit transaction control with rollback on error, and exact pgvector cosine similarity search.
"""
from typing import List, Dict, Optional, Tuple, Any
from datetime import datetime, timezone, date
from pathlib import Path
import logging
import uuid
import numpy as np

from sqlalchemy import select, and_, or_, delete, text, create_engine, func
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.exc import SQLAlchemyError, DBAPIError, IntegrityError
from pgvector.sqlalchemy import Vector

from app.schemas.document import DocumentRecord, ChunkRecord
from app.schemas.graph import (
    GraphEdge,
    RelationshipType,
    ScopeType,
    ReviewStatus,
    ConfidenceLevel,
    EdgeProvenance,
    EdgeReviewHistoryItem,
)
from app.schemas.ingestion_job import (
    IngestionJobRecord,
    JobStatus,
    ProcessingStage,
    StaleLeaseError,
    JobStateTransitionError,
    validate_status_transition,
)
from app.persistence.interfaces import (
    DocumentRepository,
    ChunkRepository,
    GraphEdgeRepository,
    IngestionJobRepository,
    WorkspaceRepository,
    CollectionRepository,
    QueryHistoryRepository,
    StorageRepositoryFactory,
    PersistenceError,
    CorruptedStorageError,
    ConnectionStorageError,
)
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
from app.models.auth import WorkspaceModel, WorkspaceMembershipModel, UserModel
from app.models.collection import CollectionModel
from app.models.query_history import QueryHistoryModel

logger = logging.getLogger(__name__)


def parse_date(date_str: Optional[str]) -> Optional[date]:
    """Parse YYYY-MM-DD string to date object, returning None if invalid or absent."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except Exception:
        return None


class PostgresDocumentRepository(DocumentRepository):
    """PostgreSQL-backed document metadata and version repository."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def get_document(self, document_id: str, workspace_id: Optional[uuid.UUID] = None) -> Optional[DocumentRecord]:
        try:
            with self.session_factory() as session:
                stmt = select(RegulatoryDocumentModel).where(RegulatoryDocumentModel.id == document_id)
                if workspace_id is not None:
                    stmt = stmt.where(RegulatoryDocumentModel.workspace_id == workspace_id)
                doc = session.execute(stmt).scalar_one_or_none()

                if not doc:
                    return None

                # Fetch latest version for physical provenance
                latest_version = session.execute(
                    select(DocumentVersionModel)
                    .where(DocumentVersionModel.document_id == document_id)
                    .order_by(DocumentVersionModel.version_number.desc())
                ).scalars().first()

                # Determine domain code (e.g. CSCRF from SEBI_CSCRF)
                domain_code = "CSCRF"
                if doc.domain_id:
                    domain_code = doc.domain_id.replace("SEBI_", "")

                return DocumentRecord(
                    document_id=doc.id,
                    circular_number=doc.circular_number,
                    title=doc.title,
                    department=doc.department,
                    issue_date=doc.first_issue_date.isoformat() if doc.first_issue_date else None,
                    effective_date=latest_version.effective_date.isoformat() if latest_version and latest_version.effective_date else None,
                    source_url=latest_version.source_url if latest_version else "",
                    retrieved_at=latest_version.retrieved_at.isoformat() if latest_version else datetime.now(timezone.utc).isoformat(),
                    sha256=latest_version.sha256 if latest_version else "",
                    pdf_path=latest_version.storage_path if latest_version else "",
                    extraction_status=(latest_version.extraction_status if (latest_version and latest_version.extraction_status) else "pending"),
                    regulator="SEBI",
                    domain=domain_code,
                    collection_id=doc.collection_id,
                )
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in get_document: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"SQLAlchemy query error in get_document: {e}") from e

    def list_documents(self, collection_id: str, workspace_id: Optional[uuid.UUID] = None) -> List[DocumentRecord]:
        if not collection_id or not isinstance(collection_id, str) or not collection_id.strip():
            raise ValueError("collection_id must be a non-empty string")
        return self._fetch_documents(collection_id=collection_id.strip(), workspace_id=workspace_id)

    def list_all_documents_global(self) -> List[DocumentRecord]:
        return self._fetch_documents(collection_id=None, workspace_id=None)

    def _fetch_documents(self, collection_id: Optional[str] = None, workspace_id: Optional[uuid.UUID] = None) -> List[DocumentRecord]:
        try:
            with self.session_factory() as session:
                stmt = select(RegulatoryDocumentModel)
                if collection_id is not None:
                    stmt = stmt.where(RegulatoryDocumentModel.collection_id == collection_id)
                if workspace_id is not None:
                    stmt = stmt.where(RegulatoryDocumentModel.workspace_id == workspace_id)
                docs = session.execute(
                    stmt.order_by(RegulatoryDocumentModel.id)
                ).scalars().all()

                results: List[DocumentRecord] = []
                for doc in docs:
                    latest_version = session.execute(
                        select(DocumentVersionModel)
                        .where(DocumentVersionModel.document_id == doc.id)
                        .order_by(DocumentVersionModel.version_number.desc())
                        .limit(1)
                    ).scalars().first()

                    domain_code = "CSCRF"
                    if doc.domain_id:
                        domain_code = doc.domain_id.replace("SEBI_", "")

                    results.append(
                        DocumentRecord(
                            document_id=doc.id,
                            circular_number=doc.circular_number,
                            title=doc.title,
                            department=doc.department,
                            issue_date=doc.first_issue_date.isoformat() if doc.first_issue_date else None,
                            effective_date=latest_version.effective_date.isoformat() if latest_version and latest_version.effective_date else None,
                            source_url=latest_version.source_url if latest_version else "",
                            retrieved_at=latest_version.retrieved_at.isoformat() if latest_version else datetime.now(timezone.utc).isoformat(),
                            sha256=latest_version.sha256 if latest_version else "",
                            pdf_path=latest_version.storage_path if latest_version else "",
                            extraction_status=(latest_version.extraction_status if (latest_version and latest_version.extraction_status) else "indexed"),
                            regulator="SEBI",
                            domain=domain_code,
                            collection_id=doc.collection_id,
                        )
                    )
                return results
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in list_documents: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"SQLAlchemy query error in list_documents: {e}") from e

    def save_document(self, document: DocumentRecord) -> None:
        try:
            with self.session_factory() as session:
                with session.begin():
                    # 1. Ensure default regulator and domain exist
                    regulator = session.execute(
                        select(RegulatorModel).where(RegulatorModel.id == "SEBI")
                    ).scalar_one_or_none()
                    if not regulator:
                        session.add(
                            RegulatorModel(
                                id="SEBI",
                                name="Securities and Exchange Board of India",
                                official_portal_url="https://www.sebi.gov.in",
                            )
                        )

                    domain = session.execute(
                        select(RegulatoryDomainModel).where(RegulatoryDomainModel.id == "SEBI_CSCRF")
                    ).scalar_one_or_none()
                    if not domain:
                        session.add(
                            RegulatoryDomainModel(
                                id="SEBI_CSCRF",
                                regulator_id="SEBI",
                                code="CSCRF",
                                title="Cybersecurity and Cyber Resilience Framework",
                            )
                        )

                    # 2. Upsert Logical Document
                    col_id = getattr(document, "collection_id", None) or "sebi_cscrf"
                    col_row = session.execute(
                        select(CollectionModel).where(func.lower(CollectionModel.id) == col_id.lower())
                    ).scalar_one_or_none()
                    ws_id = getattr(document, "workspace_id", None) or (col_row.workspace_id if col_row else uuid.UUID("00000000-0000-0000-0000-000000000000"))

                    doc_model = session.execute(
                        select(RegulatoryDocumentModel).where(RegulatoryDocumentModel.id == document.document_id)
                    ).scalar_one_or_none()

                    issue_d = parse_date(document.issue_date)
                    effective_d = parse_date(document.effective_date)

                    if not doc_model:
                        doc_model = RegulatoryDocumentModel(
                            id=document.document_id,
                            workspace_id=ws_id,
                            collection_id=col_id,
                            domain_id="SEBI_CSCRF",
                            circular_number=document.circular_number,
                            title=document.title,
                            department=document.department,
                            first_issue_date=issue_d,
                        )
                        session.add(doc_model)
                    else:
                        doc_model.title = document.title
                        doc_model.circular_number = document.circular_number
                        doc_model.department = document.department
                        doc_model.workspace_id = ws_id
                        doc_model.collection_id = col_id
                        if issue_d:
                            doc_model.first_issue_date = issue_d

                    # 3. Save Immutable Version
                    existing_version = session.execute(
                        select(DocumentVersionModel).where(
                            and_(
                                DocumentVersionModel.document_id == document.document_id,
                                DocumentVersionModel.sha256 == document.sha256,
                            )
                        )
                    ).scalar_one_or_none()

                    if not existing_version:
                        # Determine next version number
                        curr_max = session.execute(
                            select(DocumentVersionModel.version_number)
                            .where(DocumentVersionModel.document_id == document.document_id)
                            .order_by(DocumentVersionModel.version_number.desc())
                        ).scalars().first()
                        next_ver = (curr_max or 0) + 1

                        version_model = DocumentVersionModel(
                            id=f"{document.document_id}_v{next_ver}",
                            document_id=document.document_id,
                            version_number=next_ver,
                            source_url=document.source_url,
                            storage_path=document.pdf_path,
                            sha256=document.sha256,
                            issue_date=issue_d,
                            effective_date=effective_d,
                            extraction_status=document.extraction_status,
                        )
                        session.add(version_model)
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in save_document: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to save document in PostgreSQL: {e}") from e

    def get_document_by_sha256(self, sha256: str, collection_id: str, workspace_id: Optional[uuid.UUID] = None) -> Optional[DocumentRecord]:
        if not collection_id or not collection_id.strip():
            raise ValueError(
                "collection_id is required for collection-scoped SHA-256 lookup. "
                "Use get_document_by_sha256_global() for explicit unscoped lookup."
            )
        return self._lookup_document_by_sha256(sha256, collection_id=collection_id.strip(), workspace_id=workspace_id)

    def get_document_by_sha256_global(self, sha256: str) -> Optional[DocumentRecord]:
        return self._lookup_document_by_sha256(sha256, collection_id=None, workspace_id=None)

    def _lookup_document_by_sha256(self, sha256: str, collection_id: Optional[str] = None, workspace_id: Optional[uuid.UUID] = None) -> Optional[DocumentRecord]:
        try:
            with self.session_factory() as session:
                stmt = (
                    select(DocumentVersionModel)
                    .join(RegulatoryDocumentModel, DocumentVersionModel.document_id == RegulatoryDocumentModel.id)
                    .where(DocumentVersionModel.sha256 == sha256)
                )
                if collection_id is not None:
                    stmt = stmt.where(RegulatoryDocumentModel.collection_id == collection_id)
                if workspace_id is not None:
                    stmt = stmt.where(RegulatoryDocumentModel.workspace_id == workspace_id)

                version = session.execute(
                    stmt.order_by(DocumentVersionModel.version_number.desc())
                ).scalars().first()

                if not version:
                    return None

                return self.get_document(version.document_id, workspace_id=workspace_id)
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in get_document_by_sha256: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Query error in get_document_by_sha256: {e}") from e


class PostgresChunkRepository(ChunkRepository):
    """PostgreSQL + pgvector chunk and embedding repository."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def get_chunk(self, chunk_id: str) -> Optional[ChunkRecord]:
        try:
            with self.session_factory() as session:
                chunk = session.execute(
                    select(ChunkModel).where(ChunkModel.id == chunk_id)
                ).scalar_one_or_none()

                if not chunk:
                    return None

                # Get document_id via parent version
                version = session.execute(
                    select(DocumentVersionModel).where(DocumentVersionModel.id == chunk.version_id)
                ).scalar_one()

                return ChunkRecord(
                    chunk_id=chunk.id,
                    document_id=version.document_id,
                    text=chunk.text,
                    page_start=chunk.pdf_page_start,
                    page_end=chunk.pdf_page_end,
                    is_ocr=chunk.is_ocr,
                    chunk_type=chunk.chunk_type,
                    metadata=chunk.metadata_json or {},
                )
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in get_chunk: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Query error in get_chunk: {e}") from e

    def list_chunks_for_document(self, document_id: str) -> List[ChunkRecord]:
        try:
            with self.session_factory() as session:
                latest_version = session.execute(
                    select(DocumentVersionModel)
                    .where(DocumentVersionModel.document_id == document_id)
                    .order_by(DocumentVersionModel.version_number.desc())
                ).scalars().first()

                if not latest_version:
                    return []

                chunks = session.execute(
                    select(ChunkModel)
                    .where(ChunkModel.version_id == latest_version.id)
                    .order_by(ChunkModel.chunk_index.asc())
                ).scalars().all()

                return [
                    ChunkRecord(
                        chunk_id=c.id,
                        document_id=document_id,
                        text=c.text,
                        page_start=c.pdf_page_start,
                        page_end=c.pdf_page_end,
                        is_ocr=c.is_ocr,
                        chunk_type=c.chunk_type,
                        metadata=c.metadata_json or {},
                    )
                    for c in chunks
                ]
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in list_chunks_for_document: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Query error in list_chunks_for_document: {e}") from e

    def list_all_chunks(self, collection_id: str) -> List[ChunkRecord]:
        if not collection_id or not isinstance(collection_id, str) or not collection_id.strip():
            raise ValueError("collection_id must be a non-empty string")
        return self._fetch_all_chunks(collection_id=collection_id.strip())

    def list_all_chunks_global(self) -> List[ChunkRecord]:
        return self._fetch_all_chunks(collection_id=None)

    def _fetch_all_chunks(self, collection_id: Optional[str] = None) -> List[ChunkRecord]:
        try:
            with self.session_factory() as session:
                stmt = (
                    select(ChunkModel, DocumentVersionModel.document_id, RegulatoryDocumentModel.collection_id)
                    .join(DocumentVersionModel, ChunkModel.version_id == DocumentVersionModel.id)
                    .outerjoin(RegulatoryDocumentModel, DocumentVersionModel.document_id == RegulatoryDocumentModel.id)
                )
                if collection_id is not None:
                    stmt = stmt.where(RegulatoryDocumentModel.collection_id == collection_id)

                chunks = session.execute(
                    stmt.order_by(DocumentVersionModel.document_id, ChunkModel.chunk_index)
                ).all()

                return [
                    ChunkRecord(
                        chunk_id=c.id,
                        document_id=doc_id,
                        collection_id=col_id,
                        text=c.text,
                        page_start=c.pdf_page_start,
                        page_end=c.pdf_page_end,
                        is_ocr=c.is_ocr,
                        chunk_type=c.chunk_type,
                        metadata=c.metadata_json or {},
                    )
                    for c, doc_id, col_id in chunks
                ]
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in list_all_chunks: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Query error in list_all_chunks: {e}") from e

    def save_chunks(self, chunks: List[ChunkRecord]) -> None:
        if not chunks:
            return
        document_id = chunks[0].document_id
        try:
            with self.session_factory() as session:
                with session.begin():
                    latest_version = session.execute(
                        select(DocumentVersionModel)
                        .where(DocumentVersionModel.document_id == document_id)
                        .order_by(DocumentVersionModel.version_number.desc())
                    ).scalars().first()

                    if not latest_version:
                        raise PersistenceError(
                            f"Cannot save chunks: Document '{document_id}' has no registered version in database."
                        )

                    for idx, c in enumerate(chunks):
                        existing = session.execute(
                            select(ChunkModel).where(ChunkModel.id == c.chunk_id)
                        ).scalar_one_or_none()

                        if not existing:
                            session.add(
                                ChunkModel(
                                    id=c.chunk_id,
                                    version_id=latest_version.id,
                                    chunk_index=idx,
                                    text=c.text,
                                    pdf_page_start=c.page_start,
                                    pdf_page_end=c.page_end,
                                    is_ocr=c.is_ocr,
                                    chunk_type=c.chunk_type,
                                    metadata_json=c.metadata,
                                )
                            )
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in save_chunks: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to save chunks: {e}") from e

    def save_embeddings(self, embeddings: Dict[str, np.ndarray], model_name: str = "BAAI/bge-small-en-v1.5") -> None:
        if not embeddings:
            return
        try:
            with self.session_factory() as session:
                with session.begin():
                    for chunk_id, vec in embeddings.items():
                        arr = np.asarray(vec, dtype=np.float32)
                        if arr.shape != (384,):
                            raise ValueError(f"Vector for chunk '{chunk_id}' has shape {arr.shape}, expected (384,)")

                        existing = session.execute(
                            select(ChunkEmbeddingModel).where(ChunkEmbeddingModel.chunk_id == chunk_id)
                        ).scalar_one_or_none()

                        if existing:
                            existing.embedding = arr
                            existing.model_name = model_name
                            existing.embedding_dim = 384
                        else:
                            session.add(
                                ChunkEmbeddingModel(
                                    chunk_id=chunk_id,
                                    model_name=model_name,
                                    embedding_dim=384,
                                    embedding=arr,
                                )
                            )
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in save_embeddings: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to persist pgvector embeddings: {e}") from e

    def search_similar_chunks(
        self,
        collection_id: str,
        query_vector: np.ndarray,
        top_k: int = 10,
        model_name: str = "BAAI/bge-small-en-v1.5",
    ) -> List[Tuple[str, float]]:
        """
        Exact vector cosine distance search scoped to collection_id using pgvector <=> operator:
        cosine distance <=> is defined as 1 - cosine_similarity.
        Therefore cosine similarity = 1 - (embedding <=> query_vec).
        """
        if not collection_id or not isinstance(collection_id, str) or not collection_id.strip():
            raise ValueError("collection_id must be a non-empty string")
        cid = collection_id.strip()

        arr = np.asarray(query_vector, dtype=np.float32)
        if arr.shape != (384,):
            raise ValueError(f"Query vector must be 384 dimensions, got {arr.shape}")

        try:
            with self.session_factory() as session:
                # Explicitly cast parameter to vector so PostgreSQL / psycopg correctly binds <=> operator
                stmt = text(
                    """
                    SELECT ce.chunk_id, (1.0 - (ce.embedding <=> (:query_vec)::vector)) AS similarity
                    FROM chunk_embeddings ce
                    JOIN chunks c ON c.id = ce.chunk_id
                    JOIN document_versions dv ON dv.id = c.version_id
                    JOIN regulatory_documents rd ON rd.id = dv.document_id
                    WHERE rd.collection_id = :collection_id
                      AND ce.model_name = :model_name
                    ORDER BY ce.embedding <=> (:query_vec)::vector ASC
                    LIMIT :top_k
                    """
                )
                # pgvector vector representation as string literal [x1, x2, ...]
                vec_str = "[" + ",".join(str(float(x)) for x in arr) + "]"
                rows = session.execute(
                    stmt,
                    {
                        "query_vec": vec_str,
                        "collection_id": cid,
                        "model_name": model_name,
                        "top_k": top_k,
                    },
                ).all()

                return [(r[0], float(r[1])) for r in rows]
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in search_similar_chunks: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Vector search failed in PostgreSQL: {e}") from e


class PostgresGraphEdgeRepository(GraphEdgeRepository):
    """PostgreSQL-backed regulatory relationship edge repository."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def get_edge(self, edge_id: str) -> Optional[GraphEdge]:
        try:
            with self.session_factory() as session:
                edge = session.execute(
                    select(RegulatoryEdgeModel).where(RegulatoryEdgeModel.id == edge_id)
                ).scalar_one_or_none()

                if not edge:
                    return None

                return self._to_schema(edge)
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in get_edge: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Query error in get_edge: {e}") from e

    def list_all_edges(self) -> List[GraphEdge]:
        try:
            with self.session_factory() as session:
                edges = session.execute(
                    select(RegulatoryEdgeModel).order_by(RegulatoryEdgeModel.id)
                ).scalars().all()
                return [self._to_schema(e) for e in edges]
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in list_all_edges: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Query error in list_all_edges: {e}") from e

    def get_outgoing_edges(self, document_id: str) -> List[GraphEdge]:
        try:
            with self.session_factory() as session:
                edges = session.execute(
                    select(RegulatoryEdgeModel)
                    .where(RegulatoryEdgeModel.source_document_id == document_id)
                    .order_by(RegulatoryEdgeModel.id)
                ).scalars().all()
                return [self._to_schema(e) for e in edges]
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in get_outgoing_edges: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Query error in get_outgoing_edges: {e}") from e

    def get_incoming_edges(self, document_id: str) -> List[GraphEdge]:
        try:
            with self.session_factory() as session:
                edges = session.execute(
                    select(RegulatoryEdgeModel)
                    .where(RegulatoryEdgeModel.target_document_id == document_id)
                    .order_by(RegulatoryEdgeModel.id)
                ).scalars().all()
                return [self._to_schema(e) for e in edges]
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in get_incoming_edges: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Query error in get_incoming_edges: {e}") from e

    def list_edges(
        self,
        status: Optional[str] = None,
        relationship: Optional[str] = None,
        source_document_id: Optional[str] = None,
        target_document_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[GraphEdge]:
        try:
            with self.session_factory() as session:
                query = select(RegulatoryEdgeModel)
                if status is not None:
                    query = query.where(RegulatoryEdgeModel.review_status == status)
                if relationship is not None:
                    query = query.where(RegulatoryEdgeModel.relationship == relationship)
                if source_document_id is not None:
                    query = query.where(RegulatoryEdgeModel.source_document_id == source_document_id)
                if target_document_id is not None:
                    query = query.where(RegulatoryEdgeModel.target_document_id == target_document_id)

                query = query.order_by(RegulatoryEdgeModel.id).offset(offset).limit(limit)
                edges = session.execute(query).scalars().all()
                return [self._to_schema(e) for e in edges]
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in list_edges: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Query error in list_edges: {e}") from e

    def save_edge(self, edge: GraphEdge) -> None:
        try:
            with self.session_factory() as session:
                with session.begin():
                    existing = session.execute(
                        select(RegulatoryEdgeModel).where(RegulatoryEdgeModel.id == edge.edge_id)
                    ).scalar_one_or_none()

                    eff_d = parse_date(edge.effective_date)
                    history_data = [item.model_dump(mode="json") for item in edge.review_history]

                    if existing:
                        existing.relationship = edge.relationship.value
                        existing.scope = edge.scope.value
                        existing.affected_provisions = edge.affected_provisions
                        existing.evidence_text = edge.evidence_text
                        existing.evidence_pdf_page = edge.evidence_page
                        existing.confidence = edge.confidence.value
                        existing.review_status = edge.review_status.value
                        existing.effective_date = eff_d
                        existing.provenance = edge.provenance.value
                        existing.updated_at = edge.updated_at or datetime.now(timezone.utc)
                        existing.reviewed_by = edge.reviewed_by
                        existing.reviewed_at = edge.reviewed_at
                        existing.review_rationale = edge.review_rationale
                        existing.review_history = history_data
                    else:
                        session.add(
                            RegulatoryEdgeModel(
                                id=edge.edge_id,
                                source_document_id=edge.source_document_id,
                                target_document_id=edge.target_document_id,
                                relationship=edge.relationship.value,
                                scope=edge.scope.value,
                                affected_provisions=edge.affected_provisions,
                                evidence_text=edge.evidence_text,
                                evidence_pdf_page=edge.evidence_page,
                                confidence=edge.confidence.value,
                                review_status=edge.review_status.value,
                                effective_date=eff_d,
                                provenance=edge.provenance.value,
                                created_at=edge.created_at,
                                updated_at=edge.updated_at,
                                reviewed_by=edge.reviewed_by,
                                reviewed_at=edge.reviewed_at,
                                review_rationale=edge.review_rationale,
                                review_history=history_data,
                            )
                        )
        except IntegrityError as e:
            raise PersistenceError(f"Integrity constraint violation saving edge '{edge.edge_id}': {e}") from e
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in save_edge: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to persist edge: {e}") from e

    def propose_edge(self, edge: GraphEdge) -> GraphEdge:
        existing = self.get_edge(edge.edge_id)
        if existing:
            raise PersistenceError(f"Edge with ID '{edge.edge_id}' already exists")

        now = datetime.now(timezone.utc)
        edge.review_status = ReviewStatus.PROPOSED
        edge.provenance = EdgeProvenance.USER_PROPOSED
        edge.created_at = now
        edge.updated_at = now
        history_item = EdgeReviewHistoryItem(
            action="PROPOSE",
            from_status="NONE",
            to_status=ReviewStatus.PROPOSED.value,
            reviewer_id=edge.reviewed_by or "system_user",
            reviewer_name=None,
            rationale=edge.review_rationale or "Initial proposal submitted for human review",
            timestamp=now,
        )
        edge.review_history = [history_item]
        self.save_edge(edge)
        return edge

    def review_edge(
        self,
        edge_id: str,
        decision: str,
        reviewer_id: str,
        rationale: str,
        reviewer_name: Optional[str] = None,
    ) -> GraphEdge:
        edge = self.get_edge(edge_id)
        if not edge:
            raise PersistenceError(f"Edge '{edge_id}' not found")

        decision_clean = decision.lower().strip()
        if decision_clean not in ("verify", "reject"):
            raise ValueError(f"Invalid review decision '{decision}'. Must be 'verify' or 'reject'")

        if edge.review_status == ReviewStatus.REVOKED:
            raise ValueError(f"Cannot review edge '{edge_id}': edge has already been REVOKED and cannot be modified without re-proposing.")

        new_status = ReviewStatus.VERIFIED if decision_clean == "verify" else ReviewStatus.REJECTED
        old_status = edge.review_status.value
        now = datetime.now(timezone.utc)

        history_item = EdgeReviewHistoryItem(
            action="VERIFY" if decision_clean == "verify" else "REJECT",
            from_status=old_status,
            to_status=new_status.value,
            reviewer_id=reviewer_id,
            reviewer_name=reviewer_name,
            rationale=rationale,
            timestamp=now,
        )

        edge.review_status = new_status
        edge.reviewed_by = reviewer_id
        edge.reviewed_at = now
        edge.review_rationale = rationale
        edge.updated_at = now
        edge.review_history.append(history_item)

        self.save_edge(edge)
        return edge

    def revoke_edge(
        self,
        edge_id: str,
        reviewer_id: str,
        rationale: str,
        reviewer_name: Optional[str] = None,
    ) -> GraphEdge:
        edge = self.get_edge(edge_id)
        if not edge:
            raise PersistenceError(f"Edge '{edge_id}' not found")

        if edge.review_status != ReviewStatus.VERIFIED:
            raise ValueError(f"Cannot revoke edge '{edge_id}' with status '{edge.review_status.value}': only VERIFIED edges can be revoked.")

        old_status = edge.review_status.value
        now = datetime.now(timezone.utc)

        history_item = EdgeReviewHistoryItem(
            action="REVOKE",
            from_status=old_status,
            to_status=ReviewStatus.REVOKED.value,
            reviewer_id=reviewer_id,
            reviewer_name=reviewer_name,
            rationale=rationale,
            timestamp=now,
        )

        edge.review_status = ReviewStatus.REVOKED
        edge.reviewed_by = reviewer_id
        edge.reviewed_at = now
        edge.review_rationale = rationale
        edge.updated_at = now
        edge.review_history.append(history_item)

        self.save_edge(edge)
        return edge

    def reconcile_baseline_edges(
        self,
        baseline_edges: List[GraphEdge],
        allow_overwrite: bool = False,
    ) -> Dict[str, Any]:
        inserted = 0
        preserved = 0
        updated = 0

        for b_edge in baseline_edges:
            existing = self.get_edge(b_edge.edge_id)
            if not existing:
                self.save_edge(b_edge)
                inserted += 1
            else:
                if existing.review_status == ReviewStatus.VERIFIED:
                    preserved += 1
                elif allow_overwrite:
                    self.save_edge(b_edge)
                    updated += 1
                else:
                    preserved += 1

        return {
            "total_baseline": len(baseline_edges),
            "inserted": inserted,
            "preserved": preserved,
            "updated": updated,
        }

    @staticmethod
    def _to_schema(edge: RegulatoryEdgeModel) -> GraphEdge:
        history = [
            EdgeReviewHistoryItem(**h) for h in (edge.review_history or [])
        ]
        return GraphEdge(
            edge_id=edge.id,
            source_document_id=edge.source_document_id,
            target_document_id=edge.target_document_id,
            relationship=RelationshipType(edge.relationship),
            scope=ScopeType(edge.scope),
            affected_provisions=edge.affected_provisions or [],
            evidence_text=edge.evidence_text,
            evidence_page=edge.evidence_pdf_page,
            confidence=ConfidenceLevel(edge.confidence),
            review_status=ReviewStatus(edge.review_status),
            effective_date=edge.effective_date.isoformat() if edge.effective_date else None,
            provenance=EdgeProvenance(edge.provenance or "imported"),
            created_at=edge.created_at,
            updated_at=edge.updated_at,
            reviewed_by=edge.reviewed_by,
            reviewed_at=edge.reviewed_at,
            review_rationale=edge.review_rationale,
            review_history=history,
        )


class PostgresIngestionJobRepository(IngestionJobRepository):
    """
    PostgreSQL-backed ingestion job repository.
    Enforces atomic job claiming via FOR UPDATE SKIP LOCKED, lease token verification,
    heartbeat updates, exponential backoff retries, and stale job recovery.
    """

    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    @staticmethod
    def _to_record(model: IngestionJobModel) -> IngestionJobRecord:
        return IngestionJobRecord(
            id=model.id,
            source_url=model.source_url,
            idempotency_key=model.idempotency_key,
            status=JobStatus(model.status),
            stage=ProcessingStage(model.stage),
            document_id=model.document_id,
            workspace_id=model.workspace_id,
            collection_id=model.collection_id,
            sha256=model.sha256,
            attempt_count=model.attempt_count,
            max_attempts=model.max_attempts,
            claim_worker_id=model.claim_worker_id,
            claim_token=model.claim_token,
            heartbeat_at=model.heartbeat_at,
            next_retry_at=model.next_retry_at,
            error_details=model.error_details,
            created_at=model.created_at,
            started_at=model.started_at,
            completed_at=model.completed_at,
        )

    def create_job(
        self,
        source_url: str,
        idempotency_key: str,
        document_id: Optional[str] = None,
        max_attempts: int = 3,
        workspace_id: Optional[uuid.UUID] = None,
        collection_id: Optional[str] = None,
    ) -> IngestionJobRecord:
        try:
            with self.session_factory() as session:
                with session.begin():
                    # Check for existing job by idempotency key
                    existing = session.execute(
                        select(IngestionJobModel).where(IngestionJobModel.idempotency_key == idempotency_key)
                    ).scalar_one_or_none()
                    if existing:
                        return self._to_record(existing)

                    actual_ws = workspace_id
                    actual_col = collection_id or "sebi_cscrf"
                    if document_id:
                        doc = session.execute(
                            select(RegulatoryDocumentModel).where(RegulatoryDocumentModel.id == document_id)
                        ).scalar_one_or_none()
                        if doc:
                            actual_ws = actual_ws or doc.workspace_id
                            actual_col = doc.collection_id
                        else:
                            if not actual_ws:
                                col_row = session.execute(
                                    select(CollectionModel).where(func.lower(CollectionModel.id) == actual_col.lower())
                                ).scalar_one_or_none()
                                actual_ws = col_row.workspace_id if col_row else uuid.UUID("00000000-0000-0000-0000-000000000000")

                            # Ensure default regulator and domain exist
                            regulator = session.execute(
                                select(RegulatorModel).where(RegulatorModel.id == "SEBI")
                            ).scalar_one_or_none()
                            if not regulator:
                                session.add(
                                    RegulatorModel(
                                        id="SEBI",
                                        name="Securities and Exchange Board of India",
                                        official_portal_url="https://www.sebi.gov.in",
                                    )
                                )
                            domain = session.execute(
                                select(RegulatoryDomainModel).where(RegulatoryDomainModel.id == "SEBI_CSCRF")
                            ).scalar_one_or_none()
                            if not domain:
                                session.add(
                                    RegulatoryDomainModel(
                                        id="SEBI_CSCRF",
                                        regulator_id="SEBI",
                                        code="CSCRF",
                                        title="Cybersecurity and Cyber Resilience Framework",
                                    )
                                )

                            doc = RegulatoryDocumentModel(
                                id=document_id,
                                workspace_id=actual_ws,
                                collection_id=actual_col,
                                domain_id="SEBI_CSCRF",
                                title=document_id,
                                created_at=datetime.now(timezone.utc),
                            )
                            session.add(doc)
                            session.flush()

                    actual_ws = actual_ws or uuid.UUID("00000000-0000-0000-0000-000000000000")

                    job = IngestionJobModel(
                        id=uuid.uuid4(),
                        source_url=source_url,
                        idempotency_key=idempotency_key,
                        document_id=document_id,
                        workspace_id=actual_ws,
                        collection_id=actual_col,
                        status=JobStatus.QUEUED.value,
                        stage=ProcessingStage.PENDING.value,
                        attempt_count=0,
                        max_attempts=max_attempts,
                        created_at=datetime.now(timezone.utc),
                    )
                    session.add(job)
                    session.flush()
                    return self._to_record(job)
        except IntegrityError:
            # Handle race condition where another worker inserted with same idempotency key
            with self.session_factory() as session:
                existing = session.execute(
                    select(IngestionJobModel).where(IngestionJobModel.idempotency_key == idempotency_key)
                ).scalar_one_or_none()
                if existing:
                    return self._to_record(existing)
                raise PersistenceError(f"Unique constraint violated on idempotency_key '{idempotency_key}'")
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in create_job: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to create ingestion job: {e}") from e

    def get_job(self, job_id: uuid.UUID) -> Optional[IngestionJobRecord]:
        try:
            with self.session_factory() as session:
                job = session.execute(
                    select(IngestionJobModel).where(IngestionJobModel.id == job_id)
                ).scalar_one_or_none()
                return self._to_record(job) if job else None
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in get_job: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to retrieve ingestion job: {e}") from e

    def get_job_by_idempotency_key(self, idempotency_key: str) -> Optional[IngestionJobRecord]:
        try:
            with self.session_factory() as session:
                job = session.execute(
                    select(IngestionJobModel).where(IngestionJobModel.idempotency_key == idempotency_key)
                ).scalar_one_or_none()
                return self._to_record(job) if job else None
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in get_job_by_idempotency_key: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to retrieve job by idempotency key: {e}") from e

    def claim_next_job(
        self,
        worker_id: str,
        claim_token: Optional[uuid.UUID] = None,
        now: Optional[datetime] = None,
    ) -> Optional[IngestionJobRecord]:
        current_time = now or datetime.now(timezone.utc)
        token = claim_token or uuid.uuid4()

        try:
            with self.session_factory() as session:
                with session.begin():
                    # Select next eligible queued job using FOR UPDATE SKIP LOCKED
                    stmt = (
                        select(IngestionJobModel)
                        .where(
                            and_(
                                IngestionJobModel.status == JobStatus.QUEUED.value,
                                or_(
                                    IngestionJobModel.next_retry_at.is_(None),
                                    IngestionJobModel.next_retry_at <= current_time,
                                ),
                            )
                        )
                        .order_by(IngestionJobModel.created_at.asc())
                        .with_for_update(skip_locked=True)
                        .limit(1)
                    )
                    job = session.execute(stmt).scalar_one_or_none()
                    if not job:
                        return None

                    # Atomic transition to PROCESSING
                    validate_status_transition(JobStatus(job.status), JobStatus.PROCESSING)
                    job.status = JobStatus.PROCESSING.value
                    job.stage = ProcessingStage.PENDING.value
                    job.claim_worker_id = worker_id
                    job.claim_token = token
                    job.heartbeat_at = current_time
                    if not job.started_at:
                        job.started_at = current_time

                    session.flush()
                    return self._to_record(job)
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in claim_next_job: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to claim next ingestion job: {e}") from e

    def update_stage_and_heartbeat(
        self,
        job_id: uuid.UUID,
        claim_token: uuid.UUID,
        stage: ProcessingStage,
        now: Optional[datetime] = None,
    ) -> IngestionJobRecord:
        current_time = now or datetime.now(timezone.utc)
        try:
            with self.session_factory() as session:
                with session.begin():
                    job = session.execute(
                        select(IngestionJobModel)
                        .where(IngestionJobModel.id == job_id)
                        .with_for_update()
                    ).scalar_one_or_none()
                    if not job:
                        raise PersistenceError(f"Ingestion job '{job_id}' not found")

                    # Verify claim ownership and status
                    if job.claim_token != claim_token or job.status != JobStatus.PROCESSING.value:
                        raise StaleLeaseError(
                            f"Worker lease expired or superseded for job '{job_id}'. "
                            f"Current status: '{job.status}', current token: '{job.claim_token}'"
                        )

                    job.stage = stage.value
                    job.heartbeat_at = current_time
                    session.flush()
                    return self._to_record(job)
        except StaleLeaseError:
            raise
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in update_stage_and_heartbeat: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to update stage and heartbeat: {e}") from e

    def schedule_retry(
        self,
        job_id: uuid.UUID,
        claim_token: uuid.UUID,
        error_details: Dict[str, Any],
        retry_delay_seconds: float,
        now: Optional[datetime] = None,
    ) -> IngestionJobRecord:
        current_time = now or datetime.now(timezone.utc)
        try:
            with self.session_factory() as session:
                with session.begin():
                    job = session.execute(
                        select(IngestionJobModel)
                        .where(IngestionJobModel.id == job_id)
                        .with_for_update()
                    ).scalar_one_or_none()
                    if not job:
                        raise PersistenceError(f"Ingestion job '{job_id}' not found")

                    if job.claim_token != claim_token or job.status != JobStatus.PROCESSING.value:
                        raise StaleLeaseError(
                            f"Worker lease expired or superseded for job '{job_id}'. Cannot schedule retry."
                        )

                    attempts = job.attempt_count + 1
                    job.attempt_count = attempts
                    job.error_details = error_details

                    if attempts >= job.max_attempts:
                        # Terminal failure
                        # Administrative transition: non-terminal -> failed
                        job.status = JobStatus.FAILED.value
                        job.completed_at = current_time
                    else:
                        # Re-queue with future retry timestamp
                        # Administrative transition: failed -> queued
                        job.status = JobStatus.QUEUED.value
                        retry_dt = datetime.fromtimestamp(
                            current_time.timestamp() + retry_delay_seconds, tz=timezone.utc
                        )
                        job.next_retry_at = retry_dt
                        job.claim_token = None
                        job.claim_worker_id = None

                    session.flush()
                    return self._to_record(job)
        except StaleLeaseError:
            raise
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in schedule_retry: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to schedule retry for job: {e}") from e

    def mark_completed(
        self,
        job_id: uuid.UUID,
        claim_token: uuid.UUID,
        document_id: str,
        sha256: str,
        now: Optional[datetime] = None,
    ) -> IngestionJobRecord:
        current_time = now or datetime.now(timezone.utc)
        try:
            with self.session_factory() as session:
                with session.begin():
                    job = session.execute(
                        select(IngestionJobModel)
                        .where(IngestionJobModel.id == job_id)
                        .with_for_update()
                    ).scalar_one_or_none()
                    if not job:
                        raise PersistenceError(f"Ingestion job '{job_id}' not found")

                    if job.claim_token != claim_token or job.status != JobStatus.PROCESSING.value:
                        raise StaleLeaseError(f"Worker lease expired or superseded for job '{job_id}'.")

                    validate_status_transition(JobStatus(job.status), JobStatus.COMPLETED)
                    job.status = JobStatus.COMPLETED.value
                    job.stage = ProcessingStage.FINISHED.value
                    job.document_id = document_id
                    job.sha256 = sha256
                    job.completed_at = current_time
                    session.flush()
                    return self._to_record(job)
        except StaleLeaseError:
            raise
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in mark_completed: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to mark job as completed: {e}") from e

    def mark_duplicate(
        self,
        job_id: uuid.UUID,
        claim_token: uuid.UUID,
        sha256: str,
        document_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> IngestionJobRecord:
        current_time = now or datetime.now(timezone.utc)
        try:
            with self.session_factory() as session:
                with session.begin():
                    job = session.execute(
                        select(IngestionJobModel)
                        .where(IngestionJobModel.id == job_id)
                        .with_for_update()
                    ).scalar_one_or_none()
                    if not job:
                        raise PersistenceError(f"Ingestion job '{job_id}' not found")

                    if job.claim_token != claim_token or job.status != JobStatus.PROCESSING.value:
                        raise StaleLeaseError(f"Worker lease expired or superseded for job '{job_id}'.")

                    validate_status_transition(JobStatus(job.status), JobStatus.DUPLICATE)
                    job.status = JobStatus.DUPLICATE.value
                    job.stage = ProcessingStage.FINISHED.value
                    job.sha256 = sha256
                    if document_id:
                        job.document_id = document_id
                    job.completed_at = current_time
                    session.flush()
                    return self._to_record(job)
        except StaleLeaseError:
            raise
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in mark_duplicate: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to mark job as duplicate: {e}") from e

    def mark_failed(
        self,
        job_id: uuid.UUID,
        claim_token: uuid.UUID,
        error_details: Dict[str, Any],
        now: Optional[datetime] = None,
    ) -> IngestionJobRecord:
        current_time = now or datetime.now(timezone.utc)
        try:
            with self.session_factory() as session:
                with session.begin():
                    job = session.execute(
                        select(IngestionJobModel)
                        .where(IngestionJobModel.id == job_id)
                        .with_for_update()
                    ).scalar_one_or_none()
                    if not job:
                        raise PersistenceError(f"Ingestion job '{job_id}' not found")

                    if job.claim_token != claim_token or job.status != JobStatus.PROCESSING.value:
                        raise StaleLeaseError(f"Worker lease expired or superseded for job '{job_id}'.")

                    # Administrative transition: non-terminal -> failed
                    job.status = JobStatus.FAILED.value
                    job.error_details = error_details
                    job.completed_at = current_time
                    session.flush()
                    return self._to_record(job)
        except StaleLeaseError:
            raise
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in mark_failed: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to mark job as failed: {e}") from e

    def recover_stale_jobs(
        self,
        lease_timeout_seconds: float = 60.0,
        now: Optional[datetime] = None,
    ) -> int:
        current_time = now or datetime.now(timezone.utc)
        recovered_count = 0
        try:
            with self.session_factory() as session:
                with session.begin():
                    # Find all jobs in PROCESSING whose heartbeat is older than timeout
                    cutoff = datetime.fromtimestamp(
                        current_time.timestamp() - lease_timeout_seconds, tz=timezone.utc
                    )
                    stale_jobs = session.execute(
                        select(IngestionJobModel)
                        .where(
                            and_(
                                IngestionJobModel.status == JobStatus.PROCESSING.value,
                                IngestionJobModel.heartbeat_at < cutoff,
                            )
                        )
                        .with_for_update(skip_locked=True)
                    ).scalars().all()

                    for job in stale_jobs:
                        attempts = job.attempt_count + 1
                        job.attempt_count = attempts
                        if attempts >= job.max_attempts:
                            # Administrative transition: non-terminal -> failed
                            job.status = JobStatus.FAILED.value
                            job.completed_at = current_time
                            job.error_details = {"reason": "Heartbeat lease expired; max attempts reached"}
                        else:
                            # Administrative transition: failed -> queued
                            job.status = JobStatus.QUEUED.value
                            job.claim_token = None
                            job.claim_worker_id = None
                            job.error_details = {"reason": "Recovered from stale worker lease timeout"}
                        recovered_count += 1

                    session.flush()
            return recovered_count
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in recover_stale_jobs: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to recover stale jobs: {e}") from e

    def list_jobs(
        self,
        status: Optional[JobStatus] = None,
        limit: int = 50,
        offset: int = 0,
        workspace_id: Optional[uuid.UUID] = None,
        collection_id: Optional[str] = None,
    ) -> List[IngestionJobRecord]:
        try:
            with self.session_factory() as session:
                stmt = select(IngestionJobModel).order_by(IngestionJobModel.created_at.desc())
                if status is not None:
                    stmt = stmt.where(IngestionJobModel.status == status.value)
                if workspace_id is not None:
                    stmt = stmt.where(IngestionJobModel.workspace_id == workspace_id)
                if collection_id is not None:
                    stmt = stmt.where(func.lower(IngestionJobModel.collection_id) == collection_id.lower())
                stmt = stmt.limit(limit).offset(offset)
                jobs = session.execute(stmt).scalars().all()
                return [self._to_record(j) for j in jobs]
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in list_jobs: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to list ingestion jobs: {e}") from e

    def retry_job(
        self,
        job_id: uuid.UUID,
        workspace_id: Optional[uuid.UUID] = None,
        now: Optional[datetime] = None,
    ) -> IngestionJobRecord:
        current_time = now or datetime.now(timezone.utc)
        try:
            with self.session_factory() as session:
                with session.begin():
                    stmt = select(IngestionJobModel).where(IngestionJobModel.id == job_id).with_for_update()
                    if workspace_id is not None:
                        stmt = stmt.where(IngestionJobModel.workspace_id == workspace_id)
                    job = session.execute(stmt).scalar_one_or_none()
                    if not job:
                        raise PersistenceError(f"Ingestion job '{job_id}' not found")
                    if job.status != JobStatus.FAILED.value:
                        raise PersistenceError(
                            f"Job '{job_id}' is in status '{job.status}'. Only jobs in 'failed' status may be retried."
                        )
                    # Administrative transition: failed -> queued
                    job.status = JobStatus.QUEUED.value
                    job.stage = ProcessingStage.PENDING.value
                    job.claim_token = None
                    job.claim_worker_id = None
                    job.next_retry_at = current_time
                    session.flush()
                    return self._to_record(job)
        except PersistenceError:
            raise
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in retry_job: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to retry job '{job_id}': {e}") from e

    def cancel_job(
        self,
        job_id: uuid.UUID,
        workspace_id: Optional[uuid.UUID] = None,
        reason: str = "cancelled_by_user",
        now: Optional[datetime] = None,
    ) -> IngestionJobRecord:
        current_time = now or datetime.now(timezone.utc)
        try:
            with self.session_factory() as session:
                with session.begin():
                    stmt = select(IngestionJobModel).where(IngestionJobModel.id == job_id).with_for_update()
                    if workspace_id is not None:
                        stmt = stmt.where(IngestionJobModel.workspace_id == workspace_id)
                    job = session.execute(stmt).scalar_one_or_none()
                    if not job:
                        raise PersistenceError(f"Ingestion job '{job_id}' not found")
                    if job.status in (JobStatus.COMPLETED.value, JobStatus.DUPLICATE.value, JobStatus.FAILED.value):
                        raise PersistenceError(f"Job '{job_id}' is already in terminal status '{job.status}'.")
                    # Administrative transition: non-terminal -> failed
                    job.status = JobStatus.FAILED.value
                    job.stage = ProcessingStage.FINISHED.value
                    job.completed_at = current_time
                    job.error_details = {"reason": reason, "cancelled_at": current_time.isoformat()}
                    session.flush()
                    return self._to_record(job)
        except PersistenceError:
            raise
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in cancel_job: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to cancel job '{job_id}': {e}") from e


class PostgresWorkspaceRepository(WorkspaceRepository):
    """PostgreSQL-backed workspace and membership repository."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def get_workspace(self, workspace_id: uuid.UUID) -> Optional[WorkspaceModel]:
        try:
            with self.session_factory() as session:
                return session.execute(
                    select(WorkspaceModel).where(WorkspaceModel.id == workspace_id)
                ).scalar_one_or_none()
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in get_workspace: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to retrieve workspace: {e}") from e

    def get_workspace_by_slug(self, slug: str) -> Optional[WorkspaceModel]:
        try:
            with self.session_factory() as session:
                return session.execute(
                    select(WorkspaceModel).where(func.lower(WorkspaceModel.slug) == slug.lower())
                ).scalar_one_or_none()
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in get_workspace_by_slug: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to retrieve workspace by slug: {e}") from e

    def list_workspaces_for_user(self, user_id: uuid.UUID) -> List[WorkspaceModel]:
        try:
            with self.session_factory() as session:
                stmt = (
                    select(WorkspaceModel)
                    .join(WorkspaceMembershipModel, WorkspaceMembershipModel.workspace_id == WorkspaceModel.id)
                    .where(WorkspaceMembershipModel.user_id == user_id)
                    .order_by(WorkspaceModel.name.asc())
                )
                return list(session.execute(stmt).scalars().all())
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in list_workspaces_for_user: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to list workspaces: {e}") from e

    def create_workspace(
        self,
        name: str,
        slug: str,
        owner_id: Optional[uuid.UUID] = None,
        is_system: bool = False,
    ) -> WorkspaceModel:
        try:
            with self.session_factory() as session:
                with session.begin():
                    ws = WorkspaceModel(
                        id=uuid.uuid4(),
                        name=name.strip(),
                        slug=slug.strip().lower(),
                        owner_id=owner_id,
                        is_system=is_system,
                    )
                    session.add(ws)
                    session.flush()
                    return ws
        except IntegrityError as e:
            raise PersistenceError(f"Workspace with slug '{slug}' already exists or violates constraint: {e}") from e
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in create_workspace: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to create workspace: {e}") from e

    def get_membership(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> Optional[WorkspaceMembershipModel]:
        try:
            with self.session_factory() as session:
                return session.execute(
                    select(WorkspaceMembershipModel).where(
                        and_(
                            WorkspaceMembershipModel.workspace_id == workspace_id,
                            WorkspaceMembershipModel.user_id == user_id,
                        )
                    )
                ).scalar_one_or_none()
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in get_membership: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to retrieve workspace membership: {e}") from e

    def add_member(self, workspace_id: uuid.UUID, user_id: uuid.UUID, role: str) -> WorkspaceMembershipModel:
        try:
            with self.session_factory() as session:
                with session.begin():
                    mem = WorkspaceMembershipModel(
                        id=uuid.uuid4(),
                        workspace_id=workspace_id,
                        user_id=user_id,
                        role=role,
                    )
                    session.add(mem)
                    session.flush()
                    return mem
        except IntegrityError as e:
            raise PersistenceError(f"User is already a member of workspace: {e}") from e
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in add_member: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to add workspace member: {e}") from e


class PostgresCollectionRepository(CollectionRepository):
    """PostgreSQL-backed collection repository enforcing multi-tenant isolation."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def get_collection(self, collection_id: str, workspace_id: Optional[uuid.UUID] = None) -> Optional[CollectionModel]:
        try:
            with self.session_factory() as session:
                stmt = select(CollectionModel).where(func.lower(CollectionModel.id) == collection_id.lower())
                if workspace_id is not None:
                    stmt = stmt.where(CollectionModel.workspace_id == workspace_id)
                return session.execute(stmt).scalar_one_or_none()
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in get_collection: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to retrieve collection: {e}") from e

    def list_collections(self, workspace_id: uuid.UUID, include_public: bool = True) -> List[CollectionModel]:
        try:
            with self.session_factory() as session:
                stmt = select(CollectionModel)
                if include_public:
                    stmt = stmt.where(
                        or_(
                            CollectionModel.workspace_id == workspace_id,
                            CollectionModel.is_public.is_(True),
                            CollectionModel.is_system.is_(True),
                        )
                    )
                else:
                    stmt = stmt.where(CollectionModel.workspace_id == workspace_id)
                stmt = stmt.order_by(CollectionModel.name.asc())
                return list(session.execute(stmt).scalars().all())
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in list_collections: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to list collections: {e}") from e

    def create_collection(
        self,
        id: str,
        workspace_id: uuid.UUID,
        name: str,
        policy_name: str,
        description: Optional[str] = None,
        enforce_regulatory_graph: bool = False,
        is_system: bool = False,
        is_public: bool = False,
    ) -> CollectionModel:
        try:
            with self.session_factory() as session:
                with session.begin():
                    col = CollectionModel(
                        id=id.strip().lower(),
                        workspace_id=workspace_id,
                        name=name.strip(),
                        policy_name=policy_name,
                        description=description,
                        enforce_regulatory_graph=enforce_regulatory_graph,
                        is_system=is_system,
                        is_public=is_public,
                    )
                    session.add(col)
                    session.flush()
                    return col
        except IntegrityError as e:
            raise PersistenceError(f"Collection '{id}' conflict in workspace {workspace_id}: {e}") from e
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in create_collection: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to create collection: {e}") from e

    def update_collection(
        self,
        collection_id: str,
        workspace_id: uuid.UUID,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Optional[CollectionModel]:
        try:
            with self.session_factory() as session:
                with session.begin():
                    stmt = select(CollectionModel).where(
                        and_(
                            func.lower(CollectionModel.id) == collection_id.lower(),
                            CollectionModel.workspace_id == workspace_id,
                        )
                    ).with_for_update()
                    col = session.execute(stmt).scalar_one_or_none()
                    if not col:
                        return None
                    if name is not None:
                        col.name = name.strip()
                    if description is not None:
                        col.description = description
                    session.flush()
                    return col
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in update_collection: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to update collection: {e}") from e

    def delete_collection(self, collection_id: str, workspace_id: uuid.UUID) -> bool:
        try:
            with self.session_factory() as session:
                with session.begin():
                    stmt = select(CollectionModel).where(
                        and_(
                            func.lower(CollectionModel.id) == collection_id.lower(),
                            CollectionModel.workspace_id == workspace_id,
                        )
                    ).with_for_update()
                    col = session.execute(stmt).scalar_one_or_none()
                    if not col:
                        return False
                    if col.is_system:
                        raise PersistenceError("System collections cannot be deleted.")
                    session.delete(col)
                    session.flush()
                    return True
        except PersistenceError:
            raise
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in delete_collection: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to delete collection: {e}") from e


class PostgresQueryHistoryRepository(QueryHistoryRepository):
    """PostgreSQL-backed query history repository."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def create_entry(
        self,
        user_id: uuid.UUID,
        collection_id: str,
        query_text: str,
        disposition: str,
        workspace_id: Optional[uuid.UUID] = None,
        query_options: Optional[Dict[str, Any]] = None,
        summary_narrative: Optional[str] = None,
        answer_text: Optional[str] = None,
        claim_citations: Optional[List[Dict[str, Any]]] = None,
        verified_excerpts_count: int = 0,
        technical_validation_passed: bool = False,
    ) -> QueryHistoryModel:
        try:
            with self.session_factory() as session:
                with session.begin():
                    entry = QueryHistoryModel(
                        id=uuid.uuid4(),
                        user_id=user_id,
                        workspace_id=workspace_id,
                        collection_id=collection_id,
                        query_text=query_text,
                        query_options=query_options or {},
                        disposition=disposition,
                        summary_narrative=summary_narrative,
                        answer_text=answer_text,
                        claim_citations=claim_citations or [],
                        verified_excerpts_count=verified_excerpts_count,
                        technical_validation_passed=technical_validation_passed,
                    )
                    session.add(entry)
                    session.flush()
                    return entry
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in create_entry: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to create query history entry: {e}") from e

    def get_entry(self, history_id: uuid.UUID, user_id: uuid.UUID) -> Optional[QueryHistoryModel]:
        try:
            with self.session_factory() as session:
                stmt = select(QueryHistoryModel).where(
                    and_(
                        QueryHistoryModel.id == history_id,
                        QueryHistoryModel.user_id == user_id,
                    )
                )
                return session.execute(stmt).scalar_one_or_none()
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in get_entry: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to retrieve query history entry: {e}") from e

    def list_entries(
        self,
        user_id: uuid.UUID,
        workspace_id: Optional[uuid.UUID] = None,
        collection_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[QueryHistoryModel]:
        try:
            with self.session_factory() as session:
                stmt = select(QueryHistoryModel).where(QueryHistoryModel.user_id == user_id)
                if workspace_id is not None:
                    stmt = stmt.where(QueryHistoryModel.workspace_id == workspace_id)
                if collection_id is not None:
                    stmt = stmt.where(func.lower(QueryHistoryModel.collection_id) == collection_id.lower())
                stmt = stmt.order_by(QueryHistoryModel.created_at.desc()).limit(limit).offset(offset)
                return list(session.execute(stmt).scalars().all())
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in list_entries: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to list query history entries: {e}") from e

    def delete_entry(self, history_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        try:
            with self.session_factory() as session:
                with session.begin():
                    stmt = select(QueryHistoryModel).where(
                        and_(
                            QueryHistoryModel.id == history_id,
                            QueryHistoryModel.user_id == user_id,
                        )
                    ).with_for_update()
                    entry = session.execute(stmt).scalar_one_or_none()
                    if not entry:
                        return False
                    session.delete(entry)
                    session.flush()
                    return True
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database connection error in delete_entry: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to delete query history entry: {e}") from e


class PostgresStorageRepositoryFactory(StorageRepositoryFactory):
    """Factory creating PostgreSQL + pgvector repository instances."""

    def __init__(
        self,
        database_url: str,
        pool_size: Optional[int] = None,
        max_overflow: Optional[int] = None,
        pool_timeout: Optional[float] = None,
        pool_recycle: Optional[int] = None,
    ):
        self.database_url = database_url

        # Check if settings provide pool parameters
        from app.core.config import get_settings
        settings = get_settings()

        p_size = pool_size if pool_size is not None else settings.db_pool_size
        m_overflow = max_overflow if max_overflow is not None else settings.db_max_overflow
        p_timeout = pool_timeout if pool_timeout is not None else settings.db_pool_timeout
        p_recycle = pool_recycle if pool_recycle is not None else settings.db_pool_recycle

        # SQLite does not support QueuePool arguments like pool_size/max_overflow
        if database_url.startswith("sqlite"):
            self.engine = create_engine(database_url, pool_pre_ping=True)
        else:
            self.engine = create_engine(
                database_url,
                pool_pre_ping=True,
                pool_size=p_size,
                max_overflow=m_overflow,
                pool_timeout=p_timeout,
                pool_recycle=p_recycle,
            )
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def get_document_repository(self) -> DocumentRepository:
        return PostgresDocumentRepository(self.session_factory)

    def get_chunk_repository(self) -> ChunkRepository:
        return PostgresChunkRepository(self.session_factory)

    def get_graph_edge_repository(self) -> GraphEdgeRepository:
        return PostgresGraphEdgeRepository(self.session_factory)

    def get_ingestion_job_repository(self) -> IngestionJobRepository:
        return PostgresIngestionJobRepository(self.session_factory)

    def get_workspace_repository(self) -> WorkspaceRepository:
        return PostgresWorkspaceRepository(self.session_factory)

    def get_collection_repository(self) -> CollectionRepository:
        return PostgresCollectionRepository(self.session_factory)

    def get_query_history_repository(self) -> QueryHistoryRepository:
        return PostgresQueryHistoryRepository(self.session_factory)


class PostgresStorageAdapter(PostgresStorageRepositoryFactory):
    """
    Unified PostgreSQL Storage Adapter integrating repositories for:
    - Workspaces and memberships (PostgresWorkspaceRepository)
    - Collections (PostgresCollectionRepository)
    - Regulatory documents and versions (PostgresDocumentRepository)
    - Ingestion jobs and queue lifecycle (PostgresIngestionJobRepository)
    - Query history (PostgresQueryHistoryRepository)
    - Graph edges (PostgresGraphEdgeRepository)
    - Vector search over chunk embeddings (PostgresChunkRepository)
    """
    pass
