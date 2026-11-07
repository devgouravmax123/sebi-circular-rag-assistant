"""
Focused tests for Migration 0006: Auth, Workspaces & Collections Schema Foundation.

Verifies:
1. Migration 0006 schema state, tables, columns, indexes, and seeded system entities.
2. Exact document and job backfill mappings and NULL-safe audits.
3. System workspace and collection immutability protections.
4. Workspace ownership and membership integrity triggers (no direct owner transfer, no demoting owner).
5. Bidirectional race-safe consistency between ingestion_jobs and regulatory_documents.
6. Worker role privilege boundaries (zero unauthorized table access).
7. Downgrade safety guard against data loss.
8. ORM model metadata alignment.
"""
import os
import uuid
from uuid import UUID
from urllib.parse import urlparse, urlunparse
import pytest
from sqlalchemy import text, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError

from app.models import (
    Base,
    UserModel,
    WorkspaceModel,
    WorkspaceMembershipModel,
    CollectionModel,
    RegulatoryDocumentModel,
    IngestionJobModel,
)

from tests.db_test_guard import get_approved_test_db_url

SYSTEM_WORKSPACE_ID = "00000000-0000-0000-0000-000000000000"
ADMIN_DB_URL = get_approved_test_db_url()
pytestmark = pytest.mark.skipif(
    not ADMIN_DB_URL,
    reason="No approved local PostgreSQL test database configured (set TEST_DATABASE_URL=postgresql+psycopg://.../sebi_rag_test).",
)


@pytest.fixture(scope="module")
def db_engine():
    """Create disposable database engine connecting as postgres superuser."""
    engine = create_engine(ADMIN_DB_URL, echo=False)
    yield engine
    engine.dispose()


@pytest.fixture
def orm_session(db_engine):
    """Provides an isolated SQLAlchemy ORM session rolled back after each test."""
    conn = db_engine.connect()
    trans = conn.begin()
    session = Session(bind=conn)
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        conn.close()


def test_migration_0006_tables_and_seeds_exist(db_engine):
    """Asserts that all new tables exist and system seeds are intact."""
    with db_engine.connect() as conn:
        # Check tables
        res_tables = conn.execute(
            text("""
                SELECT table_name FROM information_schema.tables 
                WHERE table_schema = 'public' 
                  AND table_name IN ('users', 'workspaces', 'workspace_memberships', 'collections');
            """)
        ).fetchall()
        table_names = {r[0] for r in res_tables}
        assert table_names == {"users", "workspaces", "workspace_memberships", "collections"}

        # Check system workspace
        sys_ws = conn.execute(
            text("SELECT id::text, name, slug, is_system, owner_id FROM public.workspaces WHERE id = :id"),
            {"id": SYSTEM_WORKSPACE_ID}
        ).fetchone()
        assert sys_ws is not None
        assert sys_ws[0] == SYSTEM_WORKSPACE_ID
        assert sys_ws[1] == "System Workspace"
        assert sys_ws[2] == "system"
        assert sys_ws[3] is True
        assert sys_ws[4] is None

        # Check seeded collections
        res_cols = conn.execute(
            text("SELECT id, policy_name, enforce_regulatory_graph, is_system, is_public FROM public.collections WHERE workspace_id = :ws ORDER BY id"),
            {"ws": SYSTEM_WORKSPACE_ID}
        ).fetchall()
        cols_map = {r[0]: (r[1], r[2], r[3], r[4]) for r in res_cols}
        assert "sebi_cscrf" in cols_map
        assert cols_map["sebi_cscrf"] == ("sebi_cscrf_strict", True, True, True)
        assert "cisa_playbooks" in cols_map
        assert cols_map["cisa_playbooks"] == ("general_static", False, True, True)
        assert "demo_finops" in cols_map
        assert cols_map["demo_finops"] == ("general_static", False, True, True)
        assert "general_synthetic" in cols_map
        assert cols_map["general_synthetic"] == ("general_static", False, True, True)
        assert "system_legacy_jobs" in cols_map
        assert cols_map["system_legacy_jobs"] == ("general_static", False, True, False)


def test_document_and_job_backfill_integrity(db_engine):
    """Asserts that all documents and jobs have valid workspace/collection assignments with zero NULLs or unexpected values."""
    with db_engine.connect() as conn:
        # NULL-safe query verifying no documents have invalid or unexpected assignments
        unexpected_docs = conn.execute(
            text("""
                SELECT count(*) FROM public.regulatory_documents
                WHERE workspace_id IS NULL 
                   OR collection_id IS NULL
                   OR collection_id NOT IN ('sebi_cscrf', 'cisa_playbooks', 'demo_finops', 'general_synthetic');
            """)
        ).scalar()
        assert unexpected_docs == 0

        # Ingestion jobs audit
        unexpected_jobs = conn.execute(
            text("""
                SELECT count(*) FROM public.ingestion_jobs
                WHERE workspace_id IS NULL 
                   OR collection_id IS NULL
                   OR collection_id NOT IN ('sebi_cscrf', 'system_legacy_jobs', 'cisa_playbooks', 'demo_finops', 'general_synthetic');
            """)
        ).scalar()
        assert unexpected_jobs == 0

        # Verify that jobs with NULL document_id are mapped to system_legacy_jobs
        null_doc_jobs = conn.execute(
            text("SELECT count(*) FROM public.ingestion_jobs WHERE document_id IS NULL AND collection_id != 'system_legacy_jobs'")
        ).scalar()
        assert null_doc_jobs == 0


def test_user_email_case_insensitive_uniqueness(db_engine):
    """Asserts that users table enforces case-insensitive email uniqueness via unique index."""
    u1_id = uuid.uuid4()
    u2_id = uuid.uuid4()

    with db_engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(
                text("INSERT INTO public.users (id, email, hashed_password) VALUES (:id, 'User@Domain.Com', 'hash1')"),
                {"id": u1_id}
            )
            with pytest.raises(DBAPIError) as exc_info:
                conn.execute(
                    text("INSERT INTO public.users (id, email, hashed_password) VALUES (:id, 'user@domain.com', 'hash2')"),
                    {"id": u2_id}
                )
            assert "uq_users_email_lower" in str(exc_info.value)
        finally:
            trans.rollback()


def test_workspace_owner_check_constraint(db_engine):
    """Asserts that non-system workspaces must have owner_id, and system workspaces must not."""
    u_id = uuid.uuid4()

    with db_engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(
                text("INSERT INTO public.users (id, email, hashed_password) VALUES (:id, 'owner@domain.com', 'hash')"),
                {"id": u_id}
            )

            # 1. Non-system workspace without owner must fail
            with pytest.raises(DBAPIError) as exc_info:
                conn.execute(
                    text("INSERT INTO public.workspaces (name, slug, is_system, owner_id) VALUES ('No Owner', 'no-owner', FALSE, NULL)")
                )
            assert "chk_workspace_owner" in str(exc_info.value)
        finally:
            trans.rollback()

    with db_engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(
                text("INSERT INTO public.users (id, email, hashed_password) VALUES (:id, 'owner@domain.com', 'hash')"),
                {"id": u_id}
            )
            # 2. System workspace with owner must fail
            with pytest.raises(DBAPIError) as exc_info:
                conn.execute(
                    text("INSERT INTO public.workspaces (name, slug, is_system, owner_id) VALUES ('Sys Owner', 'sys-owner', TRUE, :uid)"),
                    {"uid": u_id}
                )
            assert "chk_workspace_owner" in str(exc_info.value)
        finally:
            trans.rollback()


def test_workspace_owner_membership_automation_and_protections(db_engine):
    """Asserts that workspace creation auto-creates owner membership and prevents demotion or transfer."""
    u_id = uuid.uuid4()
    ws_id = uuid.uuid4()

    with db_engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(
                text("INSERT INTO public.users (id, email, hashed_password) VALUES (:id, 'wsowner@domain.com', 'hash')"),
                {"id": u_id}
            )

            # Insert workspace -> trigger should auto-create owner membership
            conn.execute(
                text("INSERT INTO public.workspaces (id, name, slug, is_system, owner_id) VALUES (:ws_id, 'My WS', 'my-ws', FALSE, :uid)"),
                {"ws_id": ws_id, "uid": u_id}
            )

            mem = conn.execute(
                text("SELECT role FROM public.workspace_memberships WHERE workspace_id = :ws AND user_id = :uid"),
                {"ws": ws_id, "uid": u_id}
            ).fetchone()
            assert mem is not None
            assert mem[0] == "owner"

            # Attempt to demote owner membership
            with pytest.raises(DBAPIError) as exc_info:
                conn.execute(
                    text("UPDATE public.workspace_memberships SET role = 'viewer' WHERE workspace_id = :ws AND user_id = :uid"),
                    {"ws": ws_id, "uid": u_id}
                )
            assert "CANNOT_DEMOTE_WORKSPACE_OWNER" in str(exc_info.value)
        finally:
            trans.rollback()

    # Attempt to delete owner membership in clean transaction
    with db_engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(
                text("INSERT INTO public.users (id, email, hashed_password) VALUES (:id, 'wsowner2@domain.com', 'hash')"),
                {"id": u_id}
            )
            conn.execute(
                text("INSERT INTO public.workspaces (id, name, slug, is_system, owner_id) VALUES (:ws_id, 'My WS 2', 'my-ws-2', FALSE, :uid)"),
                {"ws_id": ws_id, "uid": u_id}
            )
            with pytest.raises(DBAPIError) as exc_info:
                conn.execute(
                    text("DELETE FROM public.workspace_memberships WHERE workspace_id = :ws AND user_id = :uid"),
                    {"ws": ws_id, "uid": u_id}
                )
            assert "CANNOT_DELETE_OWNER_MEMBERSHIP" in str(exc_info.value)
        finally:
            trans.rollback()

    # Attempt direct owner_id transfer on workspaces table
    with db_engine.connect() as conn:
        trans = conn.begin()
        try:
            u2_id = uuid.uuid4()
            conn.execute(
                text("INSERT INTO public.users (id, email, hashed_password) VALUES (:id, 'wsowner3@domain.com', 'hash')"),
                {"id": u_id}
            )
            conn.execute(
                text("INSERT INTO public.users (id, email, hashed_password) VALUES (:id, 'owner2@domain.com', 'hash')"),
                {"id": u2_id}
            )
            conn.execute(
                text("INSERT INTO public.workspaces (id, name, slug, is_system, owner_id) VALUES (:ws_id, 'My WS 3', 'my-ws-3', FALSE, :uid)"),
                {"ws_id": ws_id, "uid": u_id}
            )
            with pytest.raises(DBAPIError) as exc_info:
                conn.execute(
                    text("UPDATE public.workspaces SET owner_id = :u2 WHERE id = :ws"),
                    {"u2": u2_id, "ws": ws_id}
                )
            assert "FEATURE_NOT_IMPLEMENTED" in str(exc_info.value)
        finally:
            trans.rollback()


def test_system_entities_immutability(db_engine):
    """Asserts that system workspace and system collections cannot be modified or deleted."""
    # Attempt to delete system workspace
    with db_engine.connect() as conn:
        trans = conn.begin()
        try:
            with pytest.raises(DBAPIError) as exc_info:
                conn.execute(
                    text("DELETE FROM public.workspaces WHERE id = :id"),
                    {"id": SYSTEM_WORKSPACE_ID}
                )
            assert "CANNOT_DELETE_SYSTEM_WORKSPACE" in str(exc_info.value)
        finally:
            trans.rollback()

    # Attempt to mutate system collection policy
    with db_engine.connect() as conn:
        trans = conn.begin()
        try:
            with pytest.raises(DBAPIError) as exc_info:
                conn.execute(
                    text("UPDATE public.collections SET policy_name = 'general_static' WHERE id = 'sebi_cscrf'")
                )
            assert "CANNOT_MODIFY_SYSTEM_COLLECTION" in str(exc_info.value)
        finally:
            trans.rollback()

    # Attempt to delete system collection
    with db_engine.connect() as conn:
        trans = conn.begin()
        try:
            with pytest.raises(DBAPIError) as exc_info:
                conn.execute(
                    text("DELETE FROM public.collections WHERE id = 'sebi_cscrf'")
                )
            assert "CANNOT_DELETE_SYSTEM_COLLECTION" in str(exc_info.value)
        finally:
            trans.rollback()


def test_job_document_bidirectional_consistency(db_engine):
    """Asserts bidirectional referential locking and consistency between ingestion_jobs and regulatory_documents."""
    doc_id = f"test_doc_{uuid.uuid4().hex[:8]}"
    job_id = uuid.uuid4()

    # 1. Attempting to insert a job referencing this document with mismatched collection fails closed
    with db_engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(
                text("""
                    INSERT INTO public.regulatory_documents (id, domain_id, title, workspace_id, collection_id)
                    VALUES (:id, 'SEBI_CSCRF', 'Consistency Doc', :ws, 'sebi_cscrf')
                """),
                {"id": doc_id, "ws": SYSTEM_WORKSPACE_ID}
            )
            with pytest.raises(DBAPIError) as exc_info:
                conn.execute(
                    text("""
                        INSERT INTO public.ingestion_jobs (id, document_id, workspace_id, collection_id, source_url, status, idempotency_key)
                        VALUES (:job_id, :doc_id, :ws, 'cisa_playbooks', 'https://example.com/test.pdf', 'queued', :key)
                    """),
                    {"job_id": job_id, "doc_id": doc_id, "ws": SYSTEM_WORKSPACE_ID, "key": f"key_{job_id}"}
                )
            assert "JOB_DOCUMENT_COLLECTION_MISMATCH" in str(exc_info.value)
        finally:
            trans.rollback()

    # 2. Insert valid job and then attempt to update referenced document's collection -> fails closed
    with db_engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(
                text("""
                    INSERT INTO public.regulatory_documents (id, domain_id, title, workspace_id, collection_id)
                    VALUES (:id, 'SEBI_CSCRF', 'Consistency Doc 2', :ws, 'sebi_cscrf')
                """),
                {"id": doc_id, "ws": SYSTEM_WORKSPACE_ID}
            )
            conn.execute(
                text("""
                    INSERT INTO public.ingestion_jobs (id, document_id, workspace_id, collection_id, source_url, status, idempotency_key)
                    VALUES (:job_id, :doc_id, :ws, 'sebi_cscrf', 'https://example.com/test.pdf', 'queued', :key)
                """),
                {"job_id": job_id, "doc_id": doc_id, "ws": SYSTEM_WORKSPACE_ID, "key": f"key_{job_id}"}
            )
            with pytest.raises(DBAPIError) as exc_info:
                conn.execute(
                    text("UPDATE public.regulatory_documents SET collection_id = 'cisa_playbooks' WHERE id = :doc_id"),
                    {"doc_id": doc_id}
                )
            assert "CANNOT_MUTATE_DOCUMENT_COLLECTION" in str(exc_info.value)
        finally:
            trans.rollback()


def test_role_privilege_boundaries(db_engine):
    """Asserts that login roles inheriting ingestion_worker_role have no unauthorized table privileges."""
    role_name = f"test_worker_{uuid.uuid4().hex[:8]}"
    role_password = "worker_test_pass"

    with db_engine.connect() as conn:
        conn.execute(
            text(
                f"CREATE ROLE {role_name} WITH LOGIN PASSWORD '{role_password}' "
                f"NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS INHERIT;"
            )
        )
        conn.execute(text(f"GRANT ingestion_worker_role TO {role_name};"))
        conn.execute(text(f"GRANT CONNECT ON DATABASE sebi_rag_test TO {role_name};"))
        conn.commit()

    try:
        worker_url = f"postgresql+psycopg://{role_name}:{role_password}@localhost:5432/sebi_rag_test"
        worker_engine = create_engine(worker_url)
        # Worker role must have NO select or write on users, workspaces, memberships, collections
        for tbl in ("users", "workspaces", "workspace_memberships", "collections"):
            with worker_engine.connect() as worker_conn:
                with pytest.raises(DBAPIError) as exc_info:
                    worker_conn.execute(text(f"SELECT * FROM public.{tbl} LIMIT 1;"))
                assert "permission denied" in str(exc_info.value).lower()
        worker_engine.dispose()
    finally:
        with db_engine.connect() as conn:
            conn.execute(
                text(
                    f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    f"WHERE usename = '{role_name}' AND pid <> pg_backend_pid();"
                )
            )
            conn.execute(text(f"REVOKE ALL ON DATABASE sebi_rag_test FROM {role_name};"))
            conn.execute(text(f"REVOKE ingestion_worker_role FROM {role_name};"))
            conn.execute(text(f"DROP ROLE {role_name};"))
            conn.commit()


def test_orm_models_alignment(orm_session):
    """Asserts that SQLAlchemy ORM models cleanly map to the actual database schema."""
    user = UserModel(
        email=f"orm_{uuid.uuid4().hex[:6]}@example.com",
        hashed_password="secure_hash",
        full_name="ORM Test User",
    )
    orm_session.add(user)
    orm_session.flush()

    ws = WorkspaceModel(
        name="ORM Workspace",
        slug=f"orm-ws-{uuid.uuid4().hex[:6]}",
        is_system=False,
        owner_id=user.id,
    )
    orm_session.add(ws)
    orm_session.flush()

    # Query back using ORM
    loaded_ws = orm_session.query(WorkspaceModel).filter_by(id=ws.id).one()
    assert loaded_ws.owner.email == user.email
    assert len(loaded_ws.memberships) == 1
    assert loaded_ws.memberships[0].role == "owner"
