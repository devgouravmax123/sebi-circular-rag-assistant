# Implementation Task Tracker

## Working Agreement & Constraints
- [x] Read all project documentation before coding.
- [x] Keep required spend at ₹0 during local pilot; no paid APIs or billable cloud resources without explicit approval.
- [x] Keep API keys and secrets strictly on the backend via environment variables. Never expose in client code, logs, or Git.
- [x] Complete one milestone at a time; report changed files, test results, commands run, and blockers.
- [x] Do not fabricate test results, benchmark metrics, citations, or legal relationships.
- [x] Ground all regulatory status evaluations in verified source evidence; never infer currency solely from publication dates.

---

## Completed Foundational Milestones (Phases 0–8A)

### Phase 0 — Environment & Repository Setup
- [x] Inspect folder structure and establish root workspace.
- [x] Check Python 3.11, Node.js, npm, Git versions.
- [x] Configure `.gitignore` and `.env.example`.
- [x] Setup Python virtual environment (`.venv`) for Windows PowerShell.
- [x] Record installed dependency baselines.
*Status: `IMPLEMENTED AND TESTED`*

### Phase 1 — Minimal Vertical Slice
- [x] Create FastAPI backend with `GET /health`.
- [x] Add automated test for health check endpoint.
- [x] Scaffold React + TypeScript + Vite frontend.
- [x] Implement initial query form and answer container.
- [x] Wire frontend API client to backend environment URL.
*Status: `IMPLEMENTED AND TESTED`*

### Phase 2 — Data Models & Schemas
- [x] Define Pydantic models for document metadata, chunks, graph edges (`Newer -> RELATIONSHIP -> Older`), and citations.
- [x] Enforce provision-level scoping (`affected_provisions`) on partial relationships.
- [x] Add synthetic fixture records for deterministic unit testing.
- [x] Enforce page provenance (PDF 1-indexed and printed page) and SHA-256 checksum tracking.
*Status: `IMPLEMENTED AND TESTED`*

### Phase 3 — Ingestion Prototype & CSCRF Baseline Corpus
- [x] Register five official SEBI CSCRF circulars (`sebi_cscrf_2024_113`, `2025_045`, `2025_060`, `2025_096`, `2025_119`).
- [x] Implement deterministic PyMuPDF text and page extraction.
- [x] Implement chunking engine producing 168 structured chunks across 5 circular directories.
- [x] Complete source-backed relationship audit ([`docs/cscrf-relationship-audit.md`](file:///c:/Users/Raja/OneDrive/Desktop/sebi-rag-assistant/docs/cscrf-relationship-audit.md)) with verbatim evidence quotes, dual-page provenance, and 9 verified graph edges in `cscrf_graph.json`.
*Status: `IMPLEMENTED AND TESTED`*

### Phase 4 — Hybrid Retrieval Engine
- [x] Build in-memory BM25 lexical index over chunk corpus.
- [x] Build NumPy cosine-similarity dense vector index with normalized embeddings (`BAAI/bge-small-en-v1.5`, 384-dim).
- [x] Implement Reciprocal Rank Fusion (RRF, $k=60$) combiner.
- [x] Validate retrieval baseline using 32 golden queries (28 answerable ranking queries, 4 unanswerable refusal queries).
*Status: `IMPLEMENTED AND TESTED`*

### Phase 5 — Supersession & Temporal Status Engine
- [x] Implement NetworkX directed graph store with `AMENDS` and `REFERENCES` edges.
- [x] Enforce `is_supersession_inducing` edge logic (only `AMENDS` and `RESCINDS` alter legal currency).
- [x] Implement multi-hop `status_as_of(doc_id, date, provision)` resolution.
- [x] Add unit tests covering partial supersession, circular edge cycles, historical date queries, and unlinked documents.
*Status: `IMPLEMENTED AND TESTED`*

### Phase 6 — Grounding, Safety Gate & Abstention
- [x] Build `AnswerSafetyGate` validating candidate chunk presence in authorized retrieval set.
- [x] Enforce verbatim substring quotation checking to prevent extractive hallucination.
- [x] Implement structured refusal generation (`refuse: true`) according to explicit refusal rubric.
- [x] Ensure upstream infrastructure errors (4xx/5xx) are separated from domain refusals.
*Status: `IMPLEMENTED AND TESTED`*

### Phase 7 / 7A–7E — API, UI & Test-Driven Relevance Alignment
- [x] Implement `POST /api/v1/query` with comprehensive `QueryRequest` and `QueryResponse` schemas.
- [x] Connect React frontend with expandable citations, date pickers, and disclaimer cards.
- [x] Conduct Phase 7B relevance audit on ISO 27001 Qualified RE requirements.
- [x] Implement Phase 7C targeted relevance filter and clarification-first ordering in `backend/app/grounding/relevance.py`.
- [x] Pass full test suite (149 passing tests across unit, integration, and grounding modules).
*Status: `IMPLEMENTED AND TESTED`*

### Phase 8A — Persistence Foundation (PostgreSQL + pgvector)
- [x] **8A.1 Persistence Foundation Design:** Normalized schema design for regulators, domains, logical documents, immutable document versions, provisions, chunks, embeddings, edges, and ingestion jobs.
- [x] **8A.2 Core Persistence Foundation:** SQLAlchemy 2.x models (`backend/app/persistence/models.py`), Alembic migration scaffolding (`backend/alembic/versions/0001_initial_persistence_foundation.py`), Alembic version table width hardening (`_ensure_version_table_capacity`), repository interfaces (`backend/app/persistence/interfaces.py`), and default file-mode settings configuration.
- [x] **8A.3 File-Backed Repository Adapter & Factory:** Implemented `backend/app/persistence/file_repo.py` supporting robust file-backed read/write operations with JSON validation, error taxonomy mapping, and factory selection.
- [x] **8A.4 PostgreSQL + pgvector Adapter:** Implemented `backend/app/persistence/postgres.py` with pgvector exact cosine distance search (`<=>`), repository interface conformance, connection error handling, and vector dimension validation.
- [x] **8A.5 Idempotent Corpus Importer:** Implemented `backend/app/persistence/importer.py` and CLI `cli_importer.py` transferring file-backed pilot corpus into PostgreSQL with `--dry-run`, `--all-or-nothing`, SHA-256 conflict handling, immutable versioning, and verified edge protection.
- [x] **8A.6 Dual-Engine Persistence Parity Suite:** Implemented comprehensive parity tests (`backend/tests/test_persistence_parity.py`) and live PostgreSQL + pgvector integration tests (`backend/tests/test_postgres_live_integration.py`).
- [x] **8A.7 Persistence Documentation & Operational Runbook:** Documented storage configuration, migration procedures, safe test database guardrails, importer commands, and operational runbooks across repository documentation.
*Status: `IMPLEMENTED AND TESTED` (203 total passing tests: 200 offline unit/parity + 3 live PostgreSQL/pgvector integration tests)*

---

## Active Milestone: Phase 8B — Dynamic Document Ingestion Engine

### Sub-Milestones & Work Breakdown
- [x] **Phase 8B.1 — Dynamic Ingestion Architecture and Design:**
  - Designed end-to-end dynamic ingestion lifecycle (Registration -> Download -> Checksum Validation -> Parsing -> Chunking -> Embedding -> Persistence -> Search Index Sync).
  - Defined explicit lifecycle states (`queued`, `downloading`, `parsing`, `chunking`, `embedding`, `persisting`, `indexing`, `completed`, `failed`, `duplicate`), terminal failure rules, and legal transitions.
  - Specified secure source-fetching boundaries: HTTPS enforcement, SSRF prevention (RFC 1918/3927 private IP blocking), redirect re-validation, magic bytes signature validation (`%PDF-`), size caps (30MB), and timeouts.
  - Designed minimal schema additions for `ingestion_jobs` (idempotency key, stage, worker ID, heartbeat, retry schedule).
  - Selected PostgreSQL `FOR UPDATE SKIP LOCKED` transactional job queue for zero-cost operational simplicity, eliminating mandatory Redis/Celery dependencies in Phase 8B.
  - Defined comprehensive acceptance criteria and regression protection for pilot corpus (5 CSCRF circulars, 168 chunks, 9 edges, 32 golden queries).
  *Status: `COMPLETED (Design Only)`*
- [x] **Phase 8B.2 — Secure Source Fetcher & PDF Validator:**
  - Implemented `SecurePDFDownloader` in `backend/app/ingestion/downloader.py` with URL validation, HTTPS enforcement, SSRF defenses (blocking private, loopback, link-local, multicast, reserved, and IPv4-mapped IPv6 ranges), redirect revalidation, chunked streaming bounded to 30 MiB, magic byte `%PDF-` signature check, incremental SHA-256 calculation, and guaranteed temporary-file cleanup on all failure paths.
  - Added comprehensive unit tests in `backend/tests/test_secure_downloader.py` (15 passing tests using mock transports with zero external internet traffic).
  *Status: `IMPLEMENTED AND TESTED` (218 total passing tests in repository)*
- [x] **Phase 8B.3 — Ingestion Job Repository & Database State Machine:**
  - Implemented `IngestionJobRepository` interface in `backend/app/persistence/interfaces.py` and factory integration in `StorageRepositoryFactory`.
  - Defined explicit job state machine (`JobStatus`: `queued`, `processing`, `completed`, `duplicate`, `failed`) and processing sub-stages (`ProcessingStage`: `pending`, `downloading`, `validating`, `parsing`, `chunking`, `embedding`, `persisting`, `indexing`, `finished`) in `backend/app/schemas/ingestion_job.py`.
  - Added strict transition validation rejecting invalid transitions and preventing terminal jobs from leaving terminal states.
  - Extended `IngestionJobModel` in `backend/app/persistence/models.py` with `idempotency_key`, `stage`, `claim_worker_id`, `claim_token`, `heartbeat_at`, and `next_retry_at`, backed by database CHECK constraints and queue/heartbeat indexes.
  - Created reversible Alembic migration `0002_add_ingestion_job_queue_fields.py` chained from `0001_initial_persistence_foundation`.
  - Implemented PostgreSQL adapter `PostgresIngestionJobRepository` in `backend/app/persistence/postgres.py` with atomic claiming via `FOR UPDATE SKIP LOCKED`, unique claim tokens, heartbeat tracking, exponential backoff retries, and stale job recovery.
  - Implemented lease safety rejecting stale worker writes with `StaleLeaseError` when ownership changes or lease expires.
  - Implemented file-backed adapter `FileIngestionJobRepository` in `backend/app/persistence/file_repo.py` managing `data/processed/ingestion_jobs.json`.
  - Added comprehensive test suites: 9 unit/state tests in `test_ingestion_job_state.py` and 3 live PostgreSQL integration tests in `test_postgres_live_integration.py`.
  *Status: `IMPLEMENTED AND TESTED` (230 total passing tests: 227 offline unit/parity + 3 live PostgreSQL/pgvector integration tests)*
- [x] **Phase 8B.4 — Ingestion Worker & Orchestrator:**
  - Implemented `IngestionOrchestrator` in `backend/app/ingestion/orchestrator.py` coordinating the full lifecycle: download via `SecurePDFDownloader`, extraction via `PDFExtractor`, chunking via `TextChunker`, dense embedding via `DenseEmbedder`, atomic storage repository persistence, and completion.
  - Implemented background `HeartbeatThread` ensuring active lease renewal during network and embedding stages, with immediate execution halt upon lease loss (`StaleLeaseError`).
  - Implemented failure classification separating non-retryable errors (SSRF violations, corrupt/invalid PDFs, schema errors) from retryable transient errors (network timeouts, transient database disconnections) backed by persisted exponential backoff.
  - Implemented SHA-256 content deduplication avoiding redundant re-extraction and re-embedding when identical content is ingested.
  - Implemented `IngestionWorker` with configurable polling interval, graceful shutdown (`stop()`), automatic periodic stale job recovery, and bounded concurrency.
  - Added temporary staging file cleanup on all exit paths (success, error, and cancellation).
  - Documented search-readiness boundary: live in-memory index hot-reloading is decoupled from worker transactions to protect concurrent retrieval queries from read locks and mutation hazards.
  - Added 9 unit/lifecycle tests in `backend/tests/test_ingestion_worker.py` and live PostgreSQL + pgvector integration tests in `backend/tests/test_postgres_live_integration.py`.
  *Status: `IMPLEMENTED AND TESTED` (240 total passing tests: 233 offline unit/parity + 7 live PostgreSQL/pgvector integration tests)*
- [x] **Phase 8B.4.1 — Crash Recovery & Search Publication Gate:**
  - Verified crash-after-persistence recovery: IngestionOrchestrator recognizes when a retried job's document and SHA-256 were already persisted, completing safely without duplicate versions, chunks, or embeddings.
  - Verified lease-loss safety: zombie or superseded workers attempting state mutations or completions after lease loss are rejected by `StaleLeaseError`.
  - Documented active production retrieval path: queries in `POST /api/v1/query` use pre-indexed flat files (`data/processed` and `data/indexes/dense_index.npy`); PostgreSQL durable persistence and search-index publication are decoupled to preserve query concurrency and prevent unverified live index mutations.
  - Added regression test in `test_cscrf_corpus.py` verifying the 5 pilot CSCRF documents, exactly 168 chunks, and 9 verified graph edges remain intact.
  - Executed 32-query golden evaluation benchmark (`evaluate_retrieval.py`): BM25 Doc MRR 0.8958, Dense Doc MRR 0.9226, Hybrid RRF Doc MRR 0.9226 with 0% false abstentions across all 28 answerable queries.
  *Status: `IMPLEMENTED AND TESTED` (244 total passing tests: 236 offline unit/parity + 8 live PostgreSQL/pgvector integration tests)*
- [x] **Phase 8B.5 — Administrative REST API & CLI:**
  - Implemented versioned administrative REST API router (`/api/v1/admin`) protected by constant-time `X-Admin-Key` authentication:
    - `POST /api/v1/admin/documents/register`: Asynchronously registers documents for ingestion with strict URL/metadata validation and idempotency key deduplication.
    - `GET /api/v1/admin/jobs/{job_id}/status`: Inspects lifecycle state, failure details, retry schedule, and explicit searchability status (`searchable=False`, `publication_status="not_published"`).
    - `GET /api/v1/admin/jobs`: Lists recent jobs with bounded pagination (`limit` capped at 100) and optional status filtering.
    - `POST /api/v1/admin/jobs/{job_id}/retry`: Manually resets failed jobs to queued with zeroed attempt count; strictly rejects non-failed jobs (400).
  - Built unified operational CLI (`backend/app/ingestion/cli.py`) supporting `register`, `inspect`, `list`, `work`, and `readiness` commands without duplicate pipeline logic or secret leakage.
  - Added comprehensive test suite: 8 unit/mock/CLI tests in `backend/tests/test_admin_api.py` and live PostgreSQL integration test in `backend/tests/test_postgres_live_integration.py`.
  *Status: `IMPLEMENTED AND TESTED` (253 total passing tests: 244 offline unit/parity + 9 live PostgreSQL/pgvector integration tests)*
- [x] **Phase 8B.6 — Search Index Publication and Synchronization:**
  - Designed and built decoupled, atomic publication engine (`IndexPublisher`) supporting both file-backed and PostgreSQL repositories.
  - Generates immutable snapshot bundles (`chunks.json`, `bm25_index.json`, `dense_vectors.npy`, `manifest.json`) in staging folders (`snapshots/_staging_<id>/`).
  - Implemented strict multi-tier validation gates: dimensionality checks (384-dim normalized float32), non-finite vector detection (NaN/Inf), unique chunk IDs, and SHA-256 corpus fingerprinting.
  - Implemented atomic snapshot activation via `active_snapshot.json` atomic replacement, with seamless fallback mirroring to legacy `dense_index.npy`.
  - Enforced dynamic searchability contract: documents and jobs are marked searchable (`searchable=True`, `publication_status="published"`) strictly after inclusion in the verified active snapshot.
  - Integrated zero-downtime query reloading (`RetrievalService.reload_if_stale()`) without query interruptions or mixed lexical/dense artifacts.
  - Extended administrative REST API with `POST /api/v1/admin/indexes/publish` (with `--dry-run` validation) and `GET /api/v1/admin/indexes/status`.
  - Extended operational CLI (`backend/app/ingestion/cli.py`) with `publish-index` and `inspect-index` subcommands.
  - Added dedicated test suite (`backend/tests/test_index_publication.py`) covering snapshot generation, NaN rejection, failure recovery, REST API, searchability contract, service reload, and CLI operations.
  - Verified 32-query golden evaluation benchmark with 0% false abstentions and 100% metric preservation.
  *Status: `IMPLEMENTED AND TESTED` (261 total passing tests with live PostgreSQL/pgvector; 252 passed, 9 skipped without DATABASE_URL)*
- [ ] **Phase 8B.7 — End-to-End Ingestion Integration & Regression Verification:**
  - Verify acceptance tests across duplicate registration, changed checksums, malformed PDFs, SSRF attempts, worker crashes, and full test suite pass.
  *Status: `PLANNED`*

---

## Forward Product Roadmap (Phase 8 Series)

### Phase 8C — Generalized Multi-Domain Retrieval
- [x] Extend chunk and document schemas with regulator and domain taxonomic tags (`regulator: "SEBI"`, `domain: "CSCRF"`, `"LODR"`).
- [x] Implement pre-fusion metadata filtering (`RetrievalFilter`) across BM25 lexical search and NumPy dense vector retrieval to eliminate out-of-filter leakage before RRF scoring.
- [x] Extend query API (`POST /api/v1/query`) with optional, backward-compatible filter parameters (`domain`, `document_id`, `start_date`, `end_date`, `regulator`).
- [x] Enforce validation rules: restrict queries to `regulator="SEBI"` (rejecting secondary regulators like `"RBI"` with HTTP 422).
- [x] Propagate complete provenance and source metadata (`regulator`, `domain`, `effective_date`, `sha256`, `circular_number`, `title`, `page_start`, `page_end`).
- [x] Enforce version-aware searchability in `IndexPublisher`: verify document version SHA-256 against active snapshot to prevent pending un-published versions from appearing searchable.
- [x] Maintain pilot benchmark stability: executed 32-query golden benchmark (`evaluation/evaluate_retrieval.py`) confirming identical Doc MRR (0.8958 BM25, 0.9226 Dense, 0.9226 Hybrid RRF) and 0% false abstentions.
- [x] Add comprehensive multi-domain test suite (`backend/tests/test_multi_domain_retrieval.py`) covering 7 scenarios: unfiltered hybrid search, pre-fusion filter isolation, document_id filtering, date range filtering, API domain filtering & abstention, invalid regulator rejection (422), and version-aware searchability distinction.
*Status: `IMPLEMENTED AND TESTED` (268 total passing tests with live PostgreSQL/pgvector; 259 passed, 9 skipped without DATABASE_URL)*

### Phase 8D — Dynamic Regulatory Relationship Management
- [ ] Implement relational persistence for regulatory edges with complete evidence citations and review statuses.
- [ ] Create automated relationship candidate extractor (proposing `AMENDS`/`RESCINDS` edges from circular preambles).
- [ ] Implement Human-in-the-Loop (HITL) review workflow (`POST /api/v1/admin/edges/review`).
- [ ] Add audit logging for edge modifications and status re-evaluations.
*Status: `PLANNED`*

### Phase 8E — Grounded LLM Synthesis for Regulatory Intelligence
- [x] Introduce provider-neutral synthesis interface (`SynthesisProvider`) and schemas (`SynthesisInput`, `SynthesisOutput`, `ClaimCitation`, `CitationProvenance`, `UnresolvedSubquestion`).
- [x] Implement deterministic offline `MockSynthesisProvider` supporting claim-level citations and multi-part subquestion disclosures.
- [x] Build mechanical citation verifier (`sanitize_and_validate_synthesis_output`) checking chunk existence, claim support overlap, prompt injection neutralization, and preserving missing metadata without invention.
- [x] Implement `SynthesisService` orchestrator with bounded context and fail-safe exception handling (timeout, offline, malformed).
- [x] Wire synthesis service into `/api/v1/query` with backward-compatible response fields (`answer_text`, `synthesis_status`, `claim_citations`, `unresolved_subquestions`, `synthesis_failure_reason`).
- [x] Preserve deterministic relevance and `AnswerSafetyGate` pre-conditions: zero synthesis calls on controlled abstentions or empty evidence.
- [x] Create comprehensive 14-scenario test suite (`backend/tests/test_grounded_synthesis.py`) passing 100%.
- [x] Update API specification (`docs/api-spec.md`) with schemas and 4 required scenario examples.
*Status: `IMPLEMENTED AND TESTED`*

### Phase 8F — Hosted SEBI Deployment & Operational Hardening
- [ ] Containerize full application stack using Docker and Docker Compose (FastAPI, React, PostgreSQL, Redis).
- [ ] Implement Redis-backed rate limiting, response caching, and worker task broker.
- [ ] Expand golden evaluation benchmark (`golden.jsonl`) to 100+ queries across multiple SEBI regulatory domains.
- [ ] Verify deployment security: zero secret leakage, safe CORS, TLS configuration, and error sanitization.
*Status: `PLANNED`*

### Phase 8G — Secondary Regulator Pilot (Future Extensibility)
- [ ] Implement initial proof-of-concept connector for a secondary regulator (e.g., Reserve Bank of India - RBI).
- [ ] Evaluate multi-regulator query isolation and cross-regulatory citation.
*Status: `PLANNED`*
