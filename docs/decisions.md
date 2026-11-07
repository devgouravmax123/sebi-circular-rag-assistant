# Architecture Decision Records (ADRs)

This document records the architectural decisions, evaluation rationales, accepted constraints, and open questions governing the evolution of the platform from a specialized SEBI demonstration tool into a **generalized, multi-user Document Intelligence & Research Platform**.

---

## Index of Architectural Decisions

- **Historical Foundation (ADR-001 – ADR-014):** Core frameworks, hybrid retrieval, local embeddings, and refusal rubric.
- **Collection Awareness & Policy Pluggability (ADR-015 – ADR-020):** Declarative manifests, collection isolation, and temporal policy decoupling.
- **Evaluation & Benchmarking (ADR-026):** Stage D multi-domain evaluation baseline.
- **Generalized Multi-Tenant SaaS Decisions (ADR-027 – ADR-037):** Workspaces, Neon persistence, Backblaze B2, Groq LLM, lightweight Docker deployment, and $0/month worker execution.

---

## Historical ADRs (ADR-001 through ADR-014)

### ADR-001: React + TypeScript + Vite for Frontend
**Status:** Accepted (Implemented and Verified)
**Decision:** Build the client user interface using React, TypeScript, and Vite.
**Rationale:** Modern component model with strict typing and rapid local reload. Provides responsive layouts, expandable citations, and transparent state management.

### ADR-002: FastAPI for Backend API Services
**Status:** Accepted (Implemented and Verified)
**Decision:** Implement the API layer using Python and FastAPI with Pydantic v2 schemas.
**Rationale:** Native integration with Python scientific and retrieval libraries (PyMuPDF, sentence-transformers, NetworkX) while providing typed request/response contracts and OpenAPI documentation.

### ADR-003: Hybrid Retrieval (BM25 + Dense Vectors + RRF)
**Status:** Accepted (Implemented in Local/Stage Environments; Active Web Deployment uses Lexical BM25 per ADR-033)
**Decision:** Combine lexical search (BM25) with dense vector search (`BAAI/bge-small-en-v1.5`) using Reciprocal Rank Fusion ($k=60$).
**Rationale:** Regulatory queries require both exact alphanumeric precision (for circular numbers, section codes, compliance terms) and semantic generalization for high-level compliance queries. RRF merges rank positions without normalizing disparate score distributions.

### ADR-004: Local Embedding Model Selection (`BAAI/bge-small-en-v1.5`)
**Status:** Accepted (Implemented and Verified)
**Decision:** Standardize on `BAAI/bge-small-en-v1.5` (384-dimensional normalized vectors) evaluated via NumPy cosine similarity or PostgreSQL pgvector.
**Rationale:** Highly effective for English semantic retrieval, fast CPU inference, zero API cost, and proven benchmark performance.

### ADR-005: Standardized Graph Directionality & Provision Scoping
**Status:** Accepted (Implemented and Verified for SEBI CSCRF)
**Decision:** Standardize all graph edges strictly as `source_document_id (acting / newer) -> RELATIONSHIP -> target_document_id (affected / older)` with explicit provision scoping (`affected_provisions`).
**Rationale:** Eliminates directional ambiguity during graph traversal. Prevents partial amendments from incorrectly invalidating base master circulars.

### ADR-006: File-Based Pilot Baseline Evolving to Relational DB
**Status:** Superseded by ADR-028 (Neon PostgreSQL Adoption).

### ADR-007: Redis for Caching, Rate Limiting & Background Queues
**Status:** Deferred / Not Required Under $0/Month Architecture.

### ADR-008: Controlled Cost Management & Zero Mandatory Paid Tiers
**Status:** Required Constraint (Accepted)
**Decision:** The platform must remain fully developable, testable, and demonstrable in a zero-cost local environment. No mandatory paid APIs or billable cloud resources may be introduced without explicit approval.

### ADR-009: Fail-Closed Grounding & Explicit Refusal Rubric
**Status:** Accepted (Implemented and Verified)
**Decision:** Enforce mechanical claim-to-chunk provenance checks and verbatim quotation verification via `AnswerSafetyGate`, returning structured refusals (`refuse: true` or `disposition: CONTROLLED_ABSTENTION`) according to an explicit 5-point refusal rubric (`insufficient_evidence`, `citation_verification_failed`, `conflicting_evidence`, `out_of_domain`, `temporal_uncertainty`).

### ADR-010: Phased Dataset Progression & Regression Anchoring
**Status:** Accepted (Implemented and Verified)
**Decision:** The CSCRF pilot 5-circular corpus and its 32 golden queries (`evaluation/data/gold_evaluation_dataset.json`) are retained as a permanent regression anchor across all future platform expansions.

### ADR-011: Relational Persistence with PostgreSQL & pgvector
**Status:** Superseded and Expanded by ADR-028.

### ADR-012: Evidence-Constrained Hosted LLM Synthesis Layer
**Status:** Superseded and Expanded by ADR-031 (Groq LLM Synthesis).

### ADR-013: Pluggable Source Connectors for Regulatory Extensibility
**Status:** Proposed (Design Target for Future Automated Crawlers).

### ADR-014: Dual-Engine Storage Abstraction & Immutable Document Versioning
**Status:** Accepted (Implemented in `backend/app/persistence/`).

---

## Collection Awareness & Policy Pluggability ADRs (ADR-015 through ADR-020)

### ADR-015: Manifest-Driven Local Ingestion
**Status:** Accepted (Implemented)
**Decision:** Allow ingesting documents via declarative JSON/YAML manifests (`data/manifests/*.json`) without modifying application source code.

### ADR-016: Collection Abstraction & Mandatory Retrieval Isolation
**Status:** Accepted (Implemented)
**Decision:** Introduce `collection_id` as a first-class entity across ingestion, indexing, and query contracts. Cross-collection search without explicit collection scoping is strictly blocked ($0.0\%$ leakage tolerance).

### ADR-017: Pluggable Relationship & Temporal Policies
**Status:** Accepted (Implemented)
**Decision:** Decouple regulatory supersession from core grounding:
1. `RegulatorySupersessionPolicy`: Applies directed graph traversal (`AMENDS`, `RESCINDS`, `REFERENCES`) and temporal `as_of` date filtering (used for SEBI CSCRF).
2. `PassthroughPolicy`: Pure evidence-grounded evaluation without supersession graphs (used for general collections).

### ADR-018: Local Index Snapshots as Default Persistence Tier
**Status:** Reconciled with Cloud Storage via ADR-029.

### ADR-019: Shared Roadmap Standardization (Phases 0–4)
**Status:** Replaced by Generalized Product Implementation Roadmap (Milestones 1–6 in PRD).

### ADR-020: Conditional Metadata Database Adoption
**Status:** Reconciled with Production Multi-Tenancy in ADR-028.

---

## Evaluation & Benchmarking (ADR-026)

### ADR-026: Stage D Generalized Multi-Collection Benchmark Scope
**Status:** Accepted (Implemented and Verified — 100% Pass)
**Decision:** Formally adopt the 79-query multi-domain evaluation suite across `sebi_cscrf` (32 queries), `cisa_playbooks` (15 queries), and `general_synthetic` (32 queries) as the core acceptance baseline, deferring large-scale 200+ query regulatory expansions.

---

## Generalized Multi-Tenant SaaS Decision Records (ADR-027 through ADR-037)

### ADR-027: Workspace-Based Multi-Tenancy
**Status:** Accepted (Locally Implemented in `backend/app/models/auth.py`, `workspaces.py`)
**Context:** Transforming into a multi-user platform requires strict data segregation between users.
**Decision:** Implement workspace-based multi-tenancy. Every user owns a private workspace upon registration. Collections, documents, chunks, and query history carry a mandatory `workspace_id`. Cross-workspace access without membership returns HTTP 404.
**Consequences:** Prevents cross-tenant data leakage; requires `workspace_id` verification on all private routes.

### ADR-028: Neon Serverless PostgreSQL as Persistent Relational Database
**Status:** Accepted (Locally Implemented via Alembic Revisions `0001`–`0009`; Pending Production Branch Initialization)
**Context:** Multi-tenant user accounts, workspace memberships, dynamic collections, and query history require a managed relational database that operates at $0/month. Operator verification in the Neon SQL Editor revealed that the `sebi-rag-phase7-validation` project contains two branches:
1. `production`: Currently uninitialized (`neondb` database has no application tables or `alembic_version`).
2. `phase7-disposable`: Has full schema with Alembic revision `0009_provision_api_runtime_role` confirmed.
The Render backend service is currently configured with a pooled `DATABASE_URL` pointing to the `phase7-disposable` endpoint.
**Decision:** Standardize on Neon Serverless PostgreSQL on its free tier (0.5 GB storage cap). Execute Option A: initialize the existing `production` branch cleanly from `0001` through `0009` using `scripts/bootstrap_neon_roles.sql` and `DIRECT_DATABASE_URL` (`neondb_owner`), then rotate credentials and repoint Render. Enforce role segregation using pooled connections (`sebi_api`) for the web API, direct unpooled connections (`sebi_worker`) for the worker daemon, and `neondb_owner` exclusively for migrations.
**Trade-offs:** Neon free tier auto-suspends on inactivity; first connection after suspend experiences a 2–3s wake-up delay. Repointing Render from disposable to initialized production requires updating the Render environment variable.

### ADR-029: Backblaze B2 as Authoritative Durable Object Storage
**Status:** Accepted (Locally Implemented in `backend/app/storage/s3.py`)
**Context:** User-uploaded PDF files and published index snapshot bundles cannot reside solely on ephemeral web containers (which wipe disk on restart).
**Decision:** Use Backblaze B2 via its S3-compatible API (`STORAGE_PROVIDER=b2` or `s3`) for raw PDF storage and published snapshot bundles.
**Rationale:** Backblaze B2 provides 10 GB free storage, 1 GB/day free egress, and S3 API compatibility.
**Consequences:** Requires setting `S3_ENDPOINT_URL`, `S3_BUCKET_NAME`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, and `S3_REGION_NAME` in Render. Snapshot pointer promotion uses PostgreSQL advisory locks to coordinate atomic updates.

### ADR-030: Render Free Docker Deployment Architecture
**Status:** Accepted (Implemented and Live in Production)
**Context:** The API must run as a containerized web service at $0/month.
**Decision:** Deploy the backend on Render Free Web Service using Dockerfile `Dockerfile.demo`.
**Constraints:** 512 MB RAM ceiling, 0.1 vCPU, 15-minute inactivity sleep timer. Container disk is ephemeral.
**Mitigations:** Offload persistent storage to Backblaze B2 and Neon; load lightweight dependencies.

### ADR-031: Groq LLM for Answer Synthesis
**Status:** Accepted (Implemented and Verified)
**Decision:** Standardize on Groq (`llama-3.3-70b-versatile`) via direct HTTP requests (`httpx`) for answer synthesis.
**Rationale:** Groq provides ultra-fast inference, high quality, and a free tier with daily quotas, eliminating the need to run local generative LLMs.
**Mitigations:** Context prompts are strictly bounded; citation validator mechanically checks outputs; fails closed if quota is exhausted.

### ADR-032: Decoupled General vs. Regulatory Grounding Policies
**Status:** Accepted (Implemented in `backend/app/grounding/relationship_policy.py`)
**Context:** The original system assumed every document had circular numbers and amendment edges. General documents (e.g. employee handbooks) do not have regulatory amendment chains.
**Decision:** Decouple grounding policies:
- `RegulatorySupersessionPolicy`: Traverses directed amendment graphs and evaluates temporal dates for `sebi_cscrf`.
- `PassthroughPolicy`: Evaluates chunk evidence overlap and citation fidelity without regulatory graph logic for general collections.
**Rationale:** Enables general document intelligence without compromising rigorous SEBI compliance rules.

### ADR-033: Lexical BM25 Retrieval for Live Free-Tier Web Deployment
**Status:** Accepted (Implemented in `Dockerfile.demo` / Live on Render)
**Context:** Loading dense embedding libraries (`torch` + `sentence-transformers`) inside Python consumes 350–450 MB RAM, causing Render Free (512 MB) to crash with Out-Of-Memory (OOM) errors during startup or concurrent requests.
**Decision:** The live Render Free Docker container runs in lexical retrieval mode (`RETRIEVAL_MODE=lexical_only`), using pure-Python BM25Okapi scoring (~80 MB RAM).
**Rationale:** Guarantees 100% stable uptime, fast response times, and zero OOM kills within the 512 MB limit.

### ADR-034: Hybrid Retrieval as a Target Capability
**Status:** Accepted as Forward Target (Requires Resource-Compatible Architecture)
**Context:** Hybrid retrieval (BM25 + Dense Vectors) achieves superior semantic recall (MRR 0.9375 vs. 0.9315), but local PyTorch dense inference cannot fit safely inside 512 MB RAM.
**Decision:** Hybrid retrieval remains a target capability. It will be enabled in production either:
1. Via external API-based embedding generation (e.g. Groq/HuggingFace inference API), or
2. By executing dense vector generation exclusively on a separated external worker runner and querying pre-computed vectors in pgvector.

### ADR-035: Durable Asynchronous Ingestion Architecture
**Status:** Accepted (Implemented in `backend/app/api/v1/collections.py` and `worker.py`)
**Decision:** PDF upload writes the file to B2 and inserts an `IngestionJobModel` record with `status="queued"`. Ingestion processing is completely asynchronous and pull-based, mediated by database stored procedures.
**Rationale:** Keeps the HTTP upload handler fast (<500ms), prevents browser request timeouts, and provides resilient retry handling.

### ADR-036: Worker Execution Alternatives Under a $0 Budget
**Status:** **OPEN / UNDER EVALUATION**
**Context:** Render Background Workers cost $7+/month and cannot be used under a $0/month budget.
**Options Evaluated:**
1. **External GitHub Actions Runner:** Scheduled or repository_dispatch workflow executing `app.ingestion.worker`. (Free, 7 GB RAM, but has 30–60s start latency).
2. **Local Operator CLI Daemon:** Running `python -m app.ingestion.worker` locally against Neon when new uploads occur. (Free, instantaneous, but requires operator intervention).
3. **Bounded In-Process API Execution:** Processing jobs sequentially inside FastAPI using `BackgroundTasks`. (Free, autonomous, but risks memory spikes if PDFs are large).
**Decision:** Option 2 remains the operational default for local development. Option 1 is the primary candidate for autonomous production execution.

### ADR-037: Safe Staged Release, Database Branch Transition and Migration Sequencing
**Status:** Accepted (Option A Production Branch Transition Adopted)
**Decision:** The rollout to production must follow a strict, non-destructive sequence:
1. Initialize the existing `production` Neon branch cleanly by running `scripts/bootstrap_neon_roles.sql` as `neondb_owner` over `DIRECT_DATABASE_URL` (direct port 5432).
2. Provision uncompromised fresh passwords for `sebi_api` and `sebi_worker`.
3. Apply Alembic migrations `0001`–`0009` to the `production` branch using `DIRECT_DATABASE_URL` (`neondb_owner`).
4. Configure Render environment variables (`DATABASE_URL` pointing to initialized `production` pooled endpoint, rotated `AUTH_SECRET_KEY`, and B2 credentials).
5. Push and deploy backend code to Render.
6. Verify backend health, liveness, and database connectivity.
7. Deploy frontend updates to Vercel.
8. Verify end-to-end user journey on production, then decommission the legacy `phase7-disposable` branch.
