"""
PostgreSQL Worker Procedure Repository.

Implements the database-side worker security foundation (Phase 7.2 Task 2A).
Calls the eight SECURITY DEFINER stored procedures created in schema app:
1. app.claim_ingestion_job
2. app.heartbeat_ingestion_job
3. app.worker_create_version
4. app.worker_write_chunks_batch
5. app.complete_ingestion_job
6. app.fail_ingestion_job
7. app.recover_stale_jobs
8. app.mark_duplicate_job

Strict Security Constraints:
- Raw claim tokens are kept as str (64-char hex strings) and never converted to UUIDs.
- Raw claim tokens are NEVER logged, printed, or included in error messages.
- Zero direct ORM table writes. All operations are mediated exclusively through app stored procedures.
"""
from dataclasses import dataclass
from datetime import datetime, date
import json
import logging
from typing import List, Dict, Optional, Any, Tuple
import uuid

import numpy as np
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.exc import SQLAlchemyError, DBAPIError

from app.persistence.interfaces import (
    PersistenceError,
    ConnectionStorageError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaimedJobResult:
    """
    Result of a successful job claim.
    Masks raw_claim_token in repr to prevent accidental log leaks.
    """
    job_id: uuid.UUID
    raw_claim_token: str
    document_id: Optional[str]
    source_url: str
    lease_expires_at: datetime

    def __repr__(self) -> str:
        masked_token = f"{self.raw_claim_token[:8]}...[REDACTED]" if self.raw_claim_token else "[NONE]"
        return (
            f"ClaimedJobResult(job_id={self.job_id}, raw_claim_token={masked_token}, "
            f"document_id={self.document_id}, source_url='{self.source_url}', "
            f"lease_expires_at={self.lease_expires_at})"
        )


@dataclass(frozen=True)
class ChunkBatchItem:
    """A single chunk with embedding to be written in a batch."""
    chunk_id: str
    chunk_index: int
    text: str
    pdf_page_start: int
    pdf_page_end: int
    embedding: List[float]
    printed_page_start: Optional[str] = None
    printed_page_end: Optional[str] = None
    is_ocr: bool = False
    chunk_type: str = "body"
    metadata_json: Optional[Dict[str, Any]] = None


class PostgresWorkerProcedureRepository:
    """
    Dedicated database adapter for ingestion worker procedures.
    Operates using synchronous SQLAlchemy 2.0 sessions / engine.
    """

    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def claim_job(
        self,
        instance_id: Optional[str] = None,
        lease_duration_seconds: int = 300,
    ) -> Optional[ClaimedJobResult]:
        """
        Claim the next eligible queued ingestion job via app.claim_ingestion_job.
        Returns ClaimedJobResult if a job was claimed, or None if the queue is empty.
        """
        if lease_duration_seconds <= 0:
            raise ValueError("lease_duration_seconds must be positive")

        stmt = text(
            """
            SELECT job_id, raw_claim_token, document_id, source_url, lease_expires_at
            FROM app.claim_ingestion_job(
                p_instance_id => :instance_id,
                p_lease_duration => make_interval(secs => :lease_secs)
            );
            """
        )

        try:
            with self.session_factory() as session:
                with session.begin():
                    row = session.execute(
                        stmt,
                        {
                            "instance_id": instance_id,
                            "lease_secs": lease_duration_seconds,
                        },
                    ).mappings().fetchone()

                    if not row or not row.get("job_id"):
                        return None

                    return ClaimedJobResult(
                        job_id=row["job_id"],
                        raw_claim_token=str(row["raw_claim_token"]),
                        document_id=row["document_id"],
                        source_url=row["source_url"],
                        lease_expires_at=row["lease_expires_at"],
                    )
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database error during claim_job: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to claim job: {e}") from e

    def heartbeat(
        self,
        job_id: uuid.UUID,
        raw_claim_token: str,
        stage: Optional[str] = None,
        lease_duration_seconds: int = 300,
    ) -> datetime:
        """
        Extend the active bounded lease and optionally update processing stage.
        Returns the new lease expiration timestamp.
        """
        if not isinstance(job_id, uuid.UUID):
            raise ValueError("job_id must be a UUID")
        if not raw_claim_token or not raw_claim_token.strip():
            raise ValueError("raw_claim_token must not be empty")

        stmt = text(
            """
            SELECT app.heartbeat_ingestion_job(
                p_job_id => :job_id,
                p_raw_claim_token => :raw_token,
                p_stage => :stage,
                p_lease_duration => make_interval(secs => :lease_secs)
            );
            """
        )

        try:
            with self.session_factory() as session:
                with session.begin():
                    new_expiry = session.execute(
                        stmt,
                        {
                            "job_id": job_id,
                            "raw_token": raw_claim_token,
                            "stage": stage,
                            "lease_secs": lease_duration_seconds,
                        },
                    ).scalar_one()
                    return new_expiry
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database error during heartbeat: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to heartbeat job: {e}") from e

    def create_version(
        self,
        job_id: uuid.UUID,
        raw_claim_token: str,
        version_number: int,
        sha256_hash: str,
        source_url: str,
        storage_path: str,
        issue_date: Optional[date] = None,
        effective_date: Optional[date] = None,
    ) -> str:
        """
        Atomically create a document version in 'processing' status via app.worker_create_version.
        Returns the assigned version_id.
        """
        if not isinstance(job_id, uuid.UUID):
            raise ValueError("job_id must be a UUID")
        if not raw_claim_token or not raw_claim_token.strip():
            raise ValueError("raw_claim_token must not be empty")
        if version_number <= 0:
            raise ValueError("version_number must be positive")
        if not sha256_hash or len(sha256_hash) != 64:
            raise ValueError("sha256_hash must be a 64-character hex string")

        stmt = text(
            """
            SELECT app.worker_create_version(
                p_job_id => :job_id,
                p_raw_claim_token => :raw_token,
                p_version_number => :version_num,
                p_sha256 => :sha256,
                p_source_url => :source_url,
                p_storage_path => :storage_path,
                p_issue_date => :issue_date,
                p_effective_date => :effective_date
            );
            """
        )

        try:
            with self.session_factory() as session:
                with session.begin():
                    version_id = session.execute(
                        stmt,
                        {
                            "job_id": job_id,
                            "raw_token": raw_claim_token,
                            "version_num": version_number,
                            "sha256": sha256_hash,
                            "source_url": source_url,
                            "storage_path": storage_path,
                            "issue_date": issue_date,
                            "effective_date": effective_date,
                        },
                    ).scalar_one()
                    return str(version_id)
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database error during create_version: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to create version: {e}") from e

    def write_chunks_batch(
        self,
        job_id: uuid.UUID,
        raw_claim_token: str,
        version_id: str,
        chunks: List[ChunkBatchItem],
    ) -> int:
        """
        Persist a batch of chunks and vector embeddings atomically via app.worker_write_chunks_batch.
        Validates 384-dimensional vector embeddings, array lengths, and non-empty inputs.
        Returns the count of chunks persisted.
        """
        if not isinstance(job_id, uuid.UUID):
            raise ValueError("job_id must be a UUID")
        if not raw_claim_token or not raw_claim_token.strip():
            raise ValueError("raw_claim_token must not be empty")
        if not version_id or not version_id.strip():
            raise ValueError("version_id must not be empty")
        if not chunks:
            return 0

        # Validate dimensions and parallel array consistency
        chunk_ids: List[str] = []
        chunk_indices: List[int] = []
        texts: List[str] = []
        pdf_starts: List[int] = []
        pdf_ends: List[int] = []
        printed_starts: List[Optional[str]] = []
        printed_ends: List[Optional[str]] = []
        is_ocrs: List[bool] = []
        chunk_types: List[str] = []
        metadatas: List[str] = []
        embeddings_formatted: List[str] = []

        for item in chunks:
            if len(item.embedding) != 384:
                raise ValueError(
                    f"Vector dimension mismatch for chunk {item.chunk_id}: expected 384, got {len(item.embedding)}"
                )
            chunk_ids.append(item.chunk_id)
            chunk_indices.append(item.chunk_index)
            texts.append(item.text)
            pdf_starts.append(item.pdf_page_start)
            pdf_ends.append(item.pdf_page_end)
            printed_starts.append(item.printed_page_start)
            printed_ends.append(item.printed_page_end)
            is_ocrs.append(item.is_ocr)
            chunk_types.append(item.chunk_type or "body")
            metadatas.append(json.dumps(item.metadata_json or {}))

            # Format vector as string literal bracketed float list: '[0.1,0.2,...]'
            vec_str = "[" + ",".join(str(float(x)) for x in item.embedding) + "]"
            embeddings_formatted.append(vec_str)

        # Build dynamic cast array statement for pgvector
        # Note: psycopg requires explicit CAST(:vec_param AS vector) or ARRAY[...::vector]
        # In Task 1E, ARRAY[CAST(:vec AS vector), ...] was verified across psycopg 3
        # Construct the SQL text using parameter expansion
        vector_elements = ", ".join(f"CAST(:emb_{i} AS vector)" for i in range(len(chunks)))

        stmt = text(
            f"""
            SELECT app.worker_write_chunks_batch(
                p_job_id => :job_id,
                p_raw_claim_token => :raw_token,
                p_version_id => :version_id,
                p_chunk_ids => :chunk_ids,
                p_chunk_indices => :chunk_indices,
                p_texts => :texts,
                p_pdf_starts => :pdf_starts,
                p_pdf_ends => :pdf_ends,
                p_printed_starts => :printed_starts,
                p_printed_ends => :printed_ends,
                p_is_ocrs => :is_ocrs,
                p_chunk_types => :chunk_types,
                p_metadatas => CAST(:metadatas AS jsonb[]),
                p_embeddings => ARRAY[{vector_elements}]
            );
            """
        )

        params: Dict[str, Any] = {
            "job_id": job_id,
            "raw_token": raw_claim_token,
            "version_id": version_id,
            "chunk_ids": chunk_ids,
            "chunk_indices": chunk_indices,
            "texts": texts,
            "pdf_starts": pdf_starts,
            "pdf_ends": pdf_ends,
            "printed_starts": printed_starts,
            "printed_ends": printed_ends,
            "is_ocrs": is_ocrs,
            "chunk_types": chunk_types,
            "metadatas": metadatas,
        }
        for i, emb_str in enumerate(embeddings_formatted):
            params[f"emb_{i}"] = emb_str

        try:
            with self.session_factory() as session:
                with session.begin():
                    persisted = session.execute(stmt, params).scalar_one()
                    return int(persisted)
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database error during write_chunks_batch: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to write chunks batch: {e}") from e

    def complete_job(
        self,
        job_id: uuid.UUID,
        raw_claim_token: str,
        expected_chunk_count: int,
        allow_empty: bool = False,
    ) -> bool:
        """
        Complete an ingestion job via app.complete_ingestion_job.
        Verifies 1:1 chunk to embedding coverage and expected chunk count.
        Returns True on clean completion.
        """
        if not isinstance(job_id, uuid.UUID):
            raise ValueError("job_id must be a UUID")
        if not raw_claim_token or not raw_claim_token.strip():
            raise ValueError("raw_claim_token must not be empty")

        stmt = text(
            """
            SELECT app.complete_ingestion_job(
                p_job_id => :job_id,
                p_raw_claim_token => :raw_token,
                p_expected_chunk_count => :expected_count,
                p_allow_empty => :allow_empty
            );
            """
        )

        try:
            with self.session_factory() as session:
                with session.begin():
                    result = session.execute(
                        stmt,
                        {
                            "job_id": job_id,
                            "raw_token": raw_claim_token,
                            "expected_count": expected_chunk_count,
                            "allow_empty": allow_empty,
                        },
                    ).scalar_one()
                    return bool(result)
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database error during complete_job: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to complete job: {e}") from e

    def fail_job(
        self,
        job_id: uuid.UUID,
        raw_claim_token: str,
        error_details: Dict[str, Any],
        is_retryable: bool = True,
    ) -> str:
        """
        Fail or reschedule retry for an ingestion job via app.fail_ingestion_job.
        Returns the resulting job status ('queued' or 'failed').
        """
        if not isinstance(job_id, uuid.UUID):
            raise ValueError("job_id must be a UUID")
        if not raw_claim_token or not raw_claim_token.strip():
            raise ValueError("raw_claim_token must not be empty")

        stmt = text(
            """
            SELECT app.fail_ingestion_job(
                p_job_id => :job_id,
                p_raw_claim_token => :raw_token,
                p_error_details => CAST(:err_details AS jsonb),
                p_is_retryable => :is_retryable
            );
            """
        )

        try:
            with self.session_factory() as session:
                with session.begin():
                    new_status = session.execute(
                        stmt,
                        {
                            "job_id": job_id,
                            "raw_token": raw_claim_token,
                            "err_details": json.dumps(error_details or {}),
                            "is_retryable": is_retryable,
                        },
                    ).scalar_one()
                    return str(new_status)
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database error during fail_job: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to fail job: {e}") from e

    def mark_duplicate_job(
        self,
        job_id: uuid.UUID,
        raw_claim_token: str,
        error_details: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Mark an ingestion job as a terminal duplicate via app.mark_duplicate_job.
        Cleans up any unindexed partial processing versions and cascades chunks/embeddings,
        while strictly preserving original indexed documents and versions.
        Returns the resulting job status ('duplicate').
        """
        if not isinstance(job_id, uuid.UUID):
            raise ValueError("job_id must be a UUID")
        if not raw_claim_token or not raw_claim_token.strip():
            raise ValueError("raw_claim_token must not be empty")

        stmt = text(
            """
            SELECT app.mark_duplicate_job(
                p_job_id => :job_id,
                p_raw_claim_token => :raw_token,
                p_error_details => CAST(:err_details AS jsonb)
            );
            """
        )

        try:
            with self.session_factory() as session:
                with session.begin():
                    new_status = session.execute(
                        stmt,
                        {
                            "job_id": job_id,
                            "raw_token": raw_claim_token,
                            "err_details": json.dumps(error_details or {}),
                        },
                    ).scalar_one()
                    return str(new_status)
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database error during mark_duplicate_job: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to mark duplicate job: {e}") from e

    def recover_stale_jobs(self, batch_limit: int = 50) -> int:
        """
        Recover orphaned or expired processing jobs via app.recover_stale_jobs.
        Returns the count of recovered jobs.
        """
        if batch_limit <= 0:
            raise ValueError("batch_limit must be positive")

        stmt = text(
            """
            SELECT app.recover_stale_jobs(p_batch_limit => :batch_limit);
            """
        )

        try:
            with self.session_factory() as session:
                with session.begin():
                    recovered = session.execute(stmt, {"batch_limit": batch_limit}).scalar_one()
                    return int(recovered)
        except DBAPIError as e:
            raise ConnectionStorageError(f"Database error during recover_stale_jobs: {e}") from e
        except SQLAlchemyError as e:
            raise PersistenceError(f"Failed to recover stale jobs: {e}") from e
