# Development Rules for Humans & AI Coding Agents

## 1. Engineering Principles & Operating Protocols

1. **Inspect Before Modifying:** Always inspect existing code, schema definitions, test suites, and Git status before making changes. Never assume an implementation exists based on documentation alone.
2. **Single-Task Focus:** Implement one scoped task or milestone at a time. Do not jump across roadmap milestones in a single engineering turn.
3. **Smallest Coherent Change:** Keep diffs minimal, clean, and tightly targeted to the specific objective. Never run repository-wide formatters or touch unrelated modules.
4. **Mandatory Test Verification:** Run relevant test suites before and after modifications. Always report exact test execution commands, exit codes, and test counts. Never fabricate or extrapolate test results.
5. **Preserve User Modifications:** Never overwrite, revert, reset, or discard unrelated pre-existing working-tree changes.
6. **Strict Secret Isolation:** Never commit `.env` files, API keys, database connection passwords, or cloud credentials. Never output secrets in CLI logs, test failures, or error responses.
7. **Stop and Clarify:** When encountering ambiguous requirements, conflicting architectural choices, or unbudgeted cloud infrastructure, pause and request user clarification before proceeding.

---

## 2. Multi-Tenant Security & Authorization Rules

1. **Non-Bypassable Workspace Authorization:**
   - Every API endpoint accessing or modifying non-public resources must enforce workspace membership verification (`require_workspace_reader` or `require_workspace_writer`).
   - Never trust client-supplied user IDs or tenant IDs passed in query parameters or request bodies. Resolve user identity strictly from cryptographically verified JWT claims.
2. **Non-Enumerating Fail-Closed Responses:**
   - Unauthorized attempts to read, query, update, or delete a private workspace or collection must return **HTTP 404 Not Found** (not HTTP 403 Forbidden). This completely prevents unauthorized callers from enumerating valid workspace IDs or collection names.
   - Unauthenticated requests must return **HTTP 401 Unauthorized**.
3. **Mandatory Storage Path Partitioning:**
   - All uploaded documents and generated artifacts in object storage (Backblaze B2 or local disk) must be strictly partitioned by workspace:
     `uploads/{workspace_id}/{collection_id}/{document_id}.pdf`
     `indexes/{collection_id}/snapshots/{snapshot_id}/`
   - Cross-workspace path traversal or bucket-level directory listing by users is strictly prohibited.
4. **Zero Cross-Tenant Evidence Leakage:**
   - Queries scoped to Collection $A$ must never retrieve, evaluate, rerank, or cite candidate passages from Collection $B$.
   - Any multi-tenant pull request must include negative test cases proving that User $B$ receives HTTP 404 when querying User $A$'s private collections.
5. **System Demonstration Collection Protection:**
   - Demonstration collections (`sebi_cscrf`, `cisa_playbooks`) are system-managed and read-only.
   - Any user upload, modification, or deletion attempt targeting system collections must be rejected with **HTTP 403 Forbidden**.

---

## 3. Evidence Grounding & Citation Integrity Rules

1. **Strict Evidence Provenance:**
   - Every factual claim assertion in a synthesized answer must map to one or more verified text chunks retrieved from the target collection.
   - Every citation must preserve physical 1-indexed PDF page numbers and printed page strings.
2. **Zero Hallucinated Citations:**
   - The platform strictly forbids generating answers with fabricated citations or imaginary circular reference codes.
   - If citation validation fails, the system must fail closed to verified verbatim excerpts or issue a structured controlled abstention.
3. **Fail-Closed Controlled Abstention:**
   - When evidence is insufficient, contradictory, out-of-domain, or ambiguous, the system must return `disposition: CONTROLLED_ABSTENTION` with an explicit reason from the 5-point refusal rubric.
   - Never weaken the refusal rubric or similarity thresholds to artificially inflate answer rates.
4. **Preservation of SEBI CSCRF Baseline:**
   - The 32 golden queries in `evaluation/data/gold_evaluation_dataset.json` constitute a permanent regression anchor. Any change that causes a regression on this benchmark is rejected.
   - General document collections use `PassthroughPolicy`; do not apply SEBI-specific circular amendment rules to non-regulatory documents.

---

## 4. Database Migration & PostgreSQL Security Rules

1. **Versioned Alembic Migrations:**
   - All schema modifications must be codified in reproducible Alembic migration scripts under `backend/alembic/versions/`.
   - Never execute manual or unversioned DDL statements against development or production databases.
2. **Role Segregation Triad:**
   - **`DIRECT_DATABASE_URL` (`neondb_owner`):** Direct unpooled connection (port 5432) reserved strictly for operator-run migrations and role bootstrap. Strictly prohibited in runtime containers.
   - **`DATABASE_URL` (`sebi_api`):** Pooled connection (port 6543) for the web API runtime. Granted CRUD on tenant entities and SELECT on chunks. Direct chunk insertion and worker procedure execution are revoked.
   - **`WORKER_DATABASE_URL` (`sebi_worker`):** Direct unpooled connection (port 5432) for the ingestion worker. Zero direct table write permissions; all state changes must execute through `SECURITY DEFINER` stored procedures owned by `worker_proc_owner`.
3. **No Silent URL Cross-Fallback:**
   - If `WORKER_DATABASE_URL` is missing, the worker must fail fast with an explicit error. Silent fallback to `DATABASE_URL` is strictly prohibited.
4. **Safe Test Database Purging:**
   - Destructive operations (`drop_all`) must strictly verify that the target database name contains `test` or that `ALLOW_TEST_DATABASE_PURGE=1` is explicitly set.

---

## 5. Free-Tier Compatibility & Dependency Management

1. **Strict 512 MB RAM Discipline on Render Free:**
   - The deployed web container runs under Render Free's 512 MB memory limit.
   - Do not add heavy machine learning frameworks (`torch`, `tensorflow`, `transformers`) to `backend/requirements-demo.txt` for the web service.
   - The deployed image must run in lightweight lexical retrieval mode (`RETRIEVAL_MODE=lexical_only`), which consumes ~80 MB RAM.
2. **Avoid Broad ImportError Masking:**
   - Never wrap core production dependencies (such as `sqlalchemy`, `alembic`, `pydantic`, `boto3`) in broad `try...except ImportError` blocks that silently fall back to mock objects or disable features in production.
   - If a dependency is required for a production feature, it must be explicitly declared in the container requirements. Broad import error handling masks missing dependencies and leads to deployment crashes.
3. **$0/Month Cost Discipline:**
   - Never introduce paid cloud services, billable API dependencies, or paid Render background workers.
   - Ingestion worker execution must use free compute (local CLI runner or external free runner like GitHub Actions).
   - Backblaze B2 and Neon usage must stay strictly within free-tier quotas.

---

## 6. Git Discipline & Safe Deployment Rules

1. **Explicit Preflight Inspections:**
   - Before staging or committing, inspect `git status` and review diffs file by file.
   - Verify that no untracked secrets, credentials, or unintended debug files are staged.
2. **No Unilateral Pushes or Deployments:**
   - Do not push to remote repositories or trigger production deployments without explicit user authorization.
   - Never force-push (`git push --force`) to remote branches.
3. **Sequential Staged Rollout:**
   - Follow the migration and release sequence:
     1. Migrate database schema first.
     2. Verify environment variables in cloud dashboards.
     3. Deploy and verify backend API.
     4. Deploy frontend UI to Vercel.
