"""
Guarded Production Migration Runner for 0012_generalize_document_model.
Targets:
- Project: sebi-rag-phase7-validation
- Branch: production
- Host: ep-frosty-water-a1fhdxsf.ap-southeast-1.aws.neon.tech (direct/unpooled)
- Database: neondb
- Role: neondb_owner
"""
import os
import sys
import time
from urllib.parse import urlparse
from dotenv import dotenv_values
import psycopg

from alembic.config import Config
from alembic import command

# 1. Load configuration
env_main = dotenv_values("backend/.env")
env_prod = dotenv_values("backend/.env.production.local")

direct_url = env_prod.get("DIRECT_DATABASE_URL") or env_main.get("DIRECT_DATABASE_URL")

if not direct_url:
    print("FATAL: DIRECT_DATABASE_URL is not set in backend/.env or backend/.env.production.local", file=sys.stderr)
    sys.exit(1)

u = urlparse(direct_url)

print("=" * 70)
print("PHASE A: PRE-EXECUTION GUARD & TARGET VALIDATION")
print("=" * 70)
print(f"Target Scheme:   {u.scheme}")
print(f"Target User:     {u.username}")
print(f"Target Host:     {u.hostname}")
print(f"Target Port:     {u.port or 5432}")
print(f"Target Database: {u.path.lstrip('/')}")

# Safety assertions
if "ep-frosty-water-a1fhdxsf" not in (u.hostname or ""):
    print(f"FATAL: Host '{u.hostname}' does NOT match authorized production host 'ep-frosty-water-a1fhdxsf'!", file=sys.stderr)
    sys.exit(1)

if "ep-bitter-mountain" in (u.hostname or ""):
    print(f"FATAL: Host '{u.hostname}' is the disposable branch! Refusing execution.", file=sys.stderr)
    sys.exit(1)

if "-pooler" in (u.hostname or ""):
    print(f"FATAL: Host '{u.hostname}' is pooled! Migration requires direct unpooled connection.", file=sys.stderr)
    sys.exit(1)

if u.username != "neondb_owner":
    print(f"FATAL: Expected admin role 'neondb_owner', got '{u.username}'!", file=sys.stderr)
    sys.exit(1)

conn_str = direct_url
if "sslmode=" not in conn_str:
    sep = "&" if "?" in conn_str else "?"
    conn_str += f"{sep}sslmode=require"

# Inject transaction-local PostgreSQL timeouts (5s lock_timeout, 30s statement_timeout)
sep = "&" if "?" in conn_str else "?"
conn_str += f"{sep}options=-c%20lock_timeout%3D5000ms%20-c%20statement_timeout%3D30000ms"

print("\nVerifying current database state and Alembic revision via psycopg...")
with psycopg.connect(conn_str) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT current_database(), current_user, current_setting('server_version'), current_setting('lock_timeout'), current_setting('statement_timeout');")
        db_name, cur_user, srv_ver, lk_to, st_to = cur.fetchone()
        print(f"Connected to DB: '{db_name}' as user: '{cur_user}' (PostgreSQL {srv_ver})")
        print(f"Active timeouts: lock_timeout={lk_to}, statement_timeout={st_to}")

        cur.execute("SELECT version_num FROM alembic_version;")
        current_revs = [r[0] for r in cur.fetchall()]
        print(f"Current Alembic revision in DB: {current_revs}")

        if current_revs != ["0011_add_firebase_auth"]:
            print(f"FATAL: Expected revision ['0011_add_firebase_auth'], found {current_revs}!", file=sys.stderr)
            sys.exit(1)

        # Baseline record counts
        cur.execute("SELECT count(*) FROM public.users;")
        pre_users = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM public.workspaces;")
        pre_workspaces = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM public.collections;")
        pre_collections = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM public.regulatory_documents;")
        pre_docs = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM public.query_history;")
        pre_qh = cur.fetchone()[0]

        print(f"Pre-migration baseline records:")
        print(f"  - users               : {pre_users}")
        print(f"  - workspaces          : {pre_workspaces}")
        print(f"  - collections         : {pre_collections}")
        print(f"  - regulatory_documents: {pre_docs}")
        print(f"  - query_history       : {pre_qh}")

print("\nPhase A guard assertions PASSED.")

# 2. Phase B: Controlled Migration Execution
print("\n" + "=" * 70)
print("PHASE B: CONTROLLED MIGRATION EXECUTION")
print("=" * 70)
print("Target transition: 0011_add_firebase_auth -> 0012_generalize_document_model")

os.environ["DIRECT_DATABASE_URL"] = conn_str
os.environ["DATABASE_URL"] = conn_str
os.environ["PYTHONPATH"] = "backend"

alembic_cfg = Config("alembic.ini")
alembic_cfg.set_main_option("script_location", "backend/alembic")
# Escape '%' as '%%' for configparser interpolation safety
alembic_cfg.set_main_option("sqlalchemy.url", conn_str.replace("%", "%%"))

start_time = time.time()
print("Executing: alembic command.upgrade(alembic_cfg, '0012_generalize_document_model')...")
try:
    command.upgrade(alembic_cfg, "0012_generalize_document_model")
    duration = time.time() - start_time
    print(f"Alembic upgrade execution completed without exceptions in {duration:.3f}s.")
except Exception as e:
    duration = time.time() - start_time
    print(f"FATAL: Migration execution failed after {duration:.3f}s: {e}", file=sys.stderr)
    sys.exit(1)

# 3. Phase C: Post-Migration Verification
print("\n" + "=" * 70)
print("PHASE C: POST-MIGRATION VERIFICATION")
print("=" * 70)

with psycopg.connect(conn_str) as conn:
    with conn.cursor() as cur:
        # 1. Verify revision
        cur.execute("SELECT version_num FROM alembic_version;")
        post_revs = [r[0] for r in cur.fetchall()]
        print(f"1. Post-migration Alembic revision in DB: {post_revs}")
        if post_revs != ["0012_generalize_document_model"]:
            print(f"FATAL: Post-migration revision is {post_revs}, expected ['0012_generalize_document_model']!", file=sys.stderr)
            sys.exit(1)

        # 2. Verify domain_id nullability
        cur.execute("""
            SELECT is_nullable, data_type 
            FROM information_schema.columns 
            WHERE table_schema = 'public' AND table_name = 'regulatory_documents' AND column_name = 'domain_id';
        """)
        col_res = cur.fetchone()
        post_nullable, post_type = col_res if col_res else (None, None)
        print(f"2. regulatory_documents.domain_id: is_nullable='{post_nullable}', data_type='{post_type}'")
        if post_nullable != "YES":
            print(f"FATAL: Expected domain_id is_nullable='YES', found '{post_nullable}'!", file=sys.stderr)
            sys.exit(1)

        # 3. Verify Foreign key on domain_id remains intact
        cur.execute("""
            SELECT tc.constraint_name, ccu.table_name AS foreign_table_name, ccu.column_name AS foreign_column_name 
            FROM information_schema.table_constraints AS tc 
            JOIN information_schema.key_column_usage AS kcu
              ON tc.constraint_name = kcu.constraint_name
              AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage AS ccu
              ON ccu.constraint_name = tc.constraint_name
              AND ccu.table_schema = tc.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY' 
              AND tc.table_name = 'regulatory_documents' 
              AND kcu.column_name = 'domain_id';
        """)
        fk_rows = cur.fetchall()
        print(f"3. Foreign key on domain_id: {fk_rows}")
        if not fk_rows:
            print("FATAL: Foreign key on domain_id missing after migration!", file=sys.stderr)
            sys.exit(1)

        # 4. Verify trigger trg_default_document_workspace_collection remains enabled
        cur.execute("""
            SELECT tgname, tgenabled, relname 
            FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' 
              AND c.relname = 'regulatory_documents' 
              AND t.tgname = 'trg_default_document_workspace_collection';
        """)
        trg_row = cur.fetchone()
        print(f"4. Trigger: name='{trg_row[0]}', enabled='{trg_row[1]}', table='{trg_row[2]}'")
        if not trg_row or trg_row[1] != 'O':
            print("FATAL: Trigger trg_default_document_workspace_collection is not enabled!", file=sys.stderr)
            sys.exit(1)

        # 5. Verify trigger function body has general_synthetic fallback
        cur.execute("""
            SELECT pg_get_functiondef(p.oid)
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'app' AND p.proname = 'default_document_workspace_collection';
        """)
        fn_def = cur.fetchone()[0]
        has_general_synthetic = "general_synthetic" in fn_def
        has_sebi_branch = "sebi_cscrf" in fn_def
        print(f"5. Trigger function verified: contains 'general_synthetic'={has_general_synthetic}, contains 'sebi_cscrf'={has_sebi_branch}")
        if not has_general_synthetic or has_sebi_branch:
            print("FATAL: Trigger function does not contain expected general-purpose fallback!", file=sys.stderr)
            sys.exit(1)

        # 6. Verify record count invariants
        cur.execute("SELECT count(*) FROM public.users;")
        post_users = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM public.workspaces;")
        post_workspaces = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM public.collections;")
        post_collections = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM public.regulatory_documents;")
        post_docs = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM public.query_history;")
        post_qh = cur.fetchone()[0]

        print("\nPost-migration record counts:")
        print(f"  - users               : {post_users} (delta: {post_users - pre_users})")
        print(f"  - workspaces          : {post_workspaces} (delta: {post_workspaces - pre_workspaces})")
        print(f"  - collections         : {post_collections} (delta: {post_collections - pre_collections})")
        print(f"  - regulatory_documents: {post_docs} (delta: {post_docs - pre_docs})")
        print(f"  - query_history       : {post_qh} (delta: {post_qh - pre_qh})")

        if post_docs != pre_docs or post_collections != pre_collections:
            print("WARNING: Invariant count mismatch detected!", file=sys.stderr)

print("\n" + "=" * 70)
print("SUCCESS: Migration 0012 executed and post-migration verification PASSED.")
print("=" * 70)
