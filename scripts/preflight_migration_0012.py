"""
Phase 2 & Phase 3: Read-Only Preflight Verification for Migration 0012.
Targets Neon Production Database:
- Project: sebi-rag-phase7-validation
- Branch: production
- Host: ep-frosty-water-a1fhdxsf.ap-southeast-1.aws.neon.tech
- Database: neondb
- Role: neondb_owner
"""
import sys
import os
from urllib.parse import urlparse
from dotenv import dotenv_values
import psycopg

# 1. Load credentials
env_prod = dotenv_values("backend/.env.production.local")
env_main = dotenv_values("backend/.env")

direct_url = env_prod.get("DIRECT_DATABASE_URL") or env_main.get("DIRECT_DATABASE_URL")

if not direct_url:
    print("FATAL: DIRECT_DATABASE_URL not found in backend/.env.production.local or backend/.env", file=sys.stderr)
    sys.exit(1)

u = urlparse(direct_url)

print("=" * 70)
print("PHASE 2: PRODUCTION TARGET IDENTITY VERIFICATION")
print("=" * 70)
print(f"Target Scheme:   {u.scheme}")
print(f"Target Username: {u.username}")
print(f"Target Hostname: {u.hostname}")
print(f"Target Port:     {u.port or 5432}")
print(f"Target Database: {u.path.lstrip('/')}")

# Safety check 1: Target Host
if "ep-frosty-water-a1fhdxsf" not in (u.hostname or ""):
    print(f"FATAL: Host '{u.hostname}' does NOT match authorized production host 'ep-frosty-water-a1fhdxsf'!", file=sys.stderr)
    sys.exit(1)

# Safety check 2: Disposable host rejection
if "ep-bitter-mountain" in (u.hostname or ""):
    print(f"FATAL: Target is the disposable branch 'phase7-disposable'! Refusing execution.", file=sys.stderr)
    sys.exit(1)

# Safety check 3: Unpooled connection check
if "-pooler" in (u.hostname or ""):
    print(f"FATAL: Target host '{u.hostname}' is pooled! Migration requires direct unpooled connection.", file=sys.stderr)
    sys.exit(1)

# Safety check 4: Admin user check
if u.username != "neondb_owner":
    print(f"FATAL: Expected admin role 'neondb_owner', got '{u.username}'!", file=sys.stderr)
    sys.exit(1)

print("[+] Phase 2 Preflight: Target identity strictly verified as production endpoint.")

conn_str = direct_url
if "sslmode=" not in conn_str:
    sep = "&" if "?" in conn_str else "?"
    conn_str += f"{sep}sslmode=require"

print("\n" + "=" * 70)
print("PHASE 3: READ-ONLY PRE-MIGRATION STATE AUDIT")
print("=" * 70)

with psycopg.connect(conn_str) as conn:
    with conn.cursor() as cur:
        # Check current connection metadata
        cur.execute("SELECT current_database(), current_user, current_setting('server_version'), inet_server_addr();")
        db_name, cur_user, srv_ver, srv_addr = cur.fetchone()
        print(f"Connected to DB: '{db_name}', user: '{cur_user}', PostgreSQL version: {srv_ver}")

        # 1. Alembic version check
        cur.execute("SELECT version_num FROM alembic_version;")
        rev_rows = [r[0] for r in cur.fetchall()]
        print(f"1. Current Alembic revision in DB: {rev_rows}")
        if rev_rows != ["0011_add_firebase_auth"]:
            print(f"FATAL: Expected DB revision ['0011_add_firebase_auth'], found {rev_rows}!", file=sys.stderr)
            sys.exit(1)

        # 2. regulatory_documents table exists
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables 
                WHERE table_schema = 'public' AND table_name = 'regulatory_documents'
            );
        """)
        table_exists = cur.fetchone()[0]
        print(f"2. public.regulatory_documents exists: {table_exists}")
        if not table_exists:
            print("FATAL: Table public.regulatory_documents does not exist!", file=sys.stderr)
            sys.exit(1)

        # 3. domain_id is currently NOT NULL
        cur.execute("""
            SELECT is_nullable, data_type 
            FROM information_schema.columns 
            WHERE table_schema = 'public' AND table_name = 'regulatory_documents' AND column_name = 'domain_id';
        """)
        col_res = cur.fetchone()
        is_nullable, data_type = col_res if col_res else (None, None)
        print(f"3. domain_id column in regulatory_documents: is_nullable='{is_nullable}', data_type='{data_type}'")
        if is_nullable != "NO":
            print(f"WARNING: Expected domain_id is_nullable='NO' before migration 0012, got '{is_nullable}'")

        # 4. Foreign key to regulatory_domains exists
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
        print(f"4. Foreign key on domain_id: {fk_rows}")
        if not fk_rows:
            print("FATAL: Expected foreign key on domain_id to regulatory_domains not found!", file=sys.stderr)
            sys.exit(1)

        # 5. System workspace exists: 00000000-0000-0000-0000-000000000000
        cur.execute("SELECT id, name, is_system FROM public.workspaces WHERE id = '00000000-0000-0000-0000-000000000000'::UUID;")
        sys_ws = cur.fetchone()
        print(f"5. System workspace: id={sys_ws[0]}, name='{sys_ws[1]}', is_system={sys_ws[2]}")
        if not sys_ws:
            print("FATAL: System workspace 00000000-0000-0000-0000-000000000000 not found!", file=sys.stderr)
            sys.exit(1)

        # 6. general_synthetic collection exists
        cur.execute("SELECT id, workspace_id, name, policy_name, is_system FROM public.collections WHERE id = 'general_synthetic';")
        gen_col = cur.fetchone()
        print(f"6. general_synthetic collection: id='{gen_col[0]}', workspace_id={gen_col[1]}, policy='{gen_col[3]}', is_system={gen_col[4]}")
        if not gen_col:
            print("FATAL: Collection 'general_synthetic' not found in public.collections!", file=sys.stderr)
            sys.exit(1)

        # 7. Trigger trg_default_document_workspace_collection exists and is enabled
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
        print(f"7. Existing trigger: name='{trg_row[0]}', enabled='{trg_row[1]}', table='{trg_row[2]}'")
        if not trg_row or trg_row[1] != 'O':  # 'O' = origin/enabled
            print("FATAL: Trigger trg_default_document_workspace_collection not enabled!", file=sys.stderr)
            sys.exit(1)

        # 8. Trigger function app.default_document_workspace_collection exists
        cur.execute("""
            SELECT p.proname, n.nspname, pg_get_functiondef(p.oid)
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'app' AND p.proname = 'default_document_workspace_collection';
        """)
        fn_row = cur.fetchone()
        print(f"8. Trigger function: app.{fn_row[0]} exists. Return type: TRIGGER.")

        # 9. Migration role privileges
        cur.execute("SELECT pg_catalog.pg_get_userbyid(relowner) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = 'regulatory_documents';")
        tbl_owner = cur.fetchone()[0]
        cur.execute("SELECT has_schema_privilege('neondb_owner', 'app', 'CREATE');")
        has_schema_create = cur.fetchone()[0]
        print(f"9. regulatory_documents table owner: '{tbl_owner}', neondb_owner CREATE in app schema={has_schema_create}")
        if not (tbl_owner in ('neondb_owner', cur_user) and has_schema_create):
            print("FATAL: neondb_owner lacks necessary DDL privileges!", file=sys.stderr)
            sys.exit(1)

        # 10. Baseline counts
        cur.execute("SELECT count(*) FROM public.users;")
        user_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM public.workspaces;")
        ws_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM public.collections;")
        col_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM public.regulatory_documents;")
        doc_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM public.query_history;")
        qh_count = cur.fetchone()[0]

        print("\n" + "=" * 70)
        print("BASELINE ENTITY RECORD COUNTS:")
        print("=" * 70)
        print(f"  - Users:                {user_count}")
        print(f"  - Workspaces:           {ws_count}")
        print(f"  - Collections:          {col_count}")
        print(f"  - Regulatory Documents: {doc_count}")
        print(f"  - Query History Rows:   {qh_count}")
        print("=" * 70)
        print("[+] Phase 3 Preflight: All preconditions met. Ready for execution safety check.")
