# Product Requirements Document (PRD)

## 1. Executive Summary & Product Vision

The **Document Regulatory & Intelligence Platform** is an evidence-grounded, collection-aware question-answering system designed to discover, ingest, search, and understand official regulatory frameworks and structured documentation collections.

The platform began as a focused, high-stakes pilot for the **Securities and Exchange Board of India (SEBI) Cybersecurity and Cyber Resilience Framework (CSCRF)** across five circulars. It is evolving into a **general-purpose, collection-aware document question-answering platform** capable of managing diverse document corpora (e.g., broader financial regulations, corporate policies, technical standards, research specifications) without requiring developers to hardcode new documents into Python application code.

### Core Distinguishing Principles
1. **Evidence-Grounded Question Answering:** Every answer assertion is mechanically verified against authentic retrieved text excerpts with 1-indexed PDF and printed page provenance.
2. **Fail-Closed Safety & Explicit Abstention:** When evidence is missing, conflicting, out-of-domain, or superseded, the system returns a structured refusal rather than fabricating compliance advice.
3. **Supersession-Aware Temporal Resolution:** Where collections define regulatory relationships (such as SEBI circulars), amendment chains and clause-level modifications are resolved dynamically as of any requested date (`as_of`).
4. **Strict Collection Isolation:** Queries scoped to a specific collection are guaranteed never to retrieve, evaluate, cite, or leak candidate passages from another collection.
5. **Zero-Cost Local Reproducibility:** The entire platform remains fully operable, testable, and demonstrable in local development without mandatory paid APIs, hosted vector databases, or commercial subscriptions.

---

## 2. Problem Statement & Core Value

Legal analysts, compliance officers, risk managers, and auditors face operational friction when navigating dense regulatory corpora and technical documentation:

1. **Circular Proliferation & Amendment Chaining:** Master frameworks are frequently amended, extended, or partially superseded by subsequent circulars. Naive semantic search retrieves out-of-date circulars, presenting obsolete compliance deadlines or revoked mandates as active law.
2. **Provision-Level Granularity:** New circulars rarely repeal master frameworks in whole; instead, specific clauses, tables, or applicability thresholds are modified while the remainder of the base circular remains strictly in force.
3. **Temporal Ambiguity:** Compliance obligations vary depending on the historical date in question, especially during phased transition periods (glide paths).
4. **LLM Hallucination & Citation Inaccuracy:** Standard generative AI frequently invents circular numbers, dates, or legal mandates. In regulatory compliance, an ungrounded answer is far worse than no answer.
5. **Rigid Corpus Architecture:** Hardcoding document IDs, token expansions, and graph edges in application source code prevents compliance teams from adding new circulars or managing non-regulatory documentation collections.

---

## 3. Product Goals & Architectural Scope

### 3.1. Primary Goals
- **Official Source Provenance:** Every response, excerpt, and citation traces directly to an official source PDF, with dual page numbers (PDF page and printed page) and cryptographic document hashes (SHA-256).
- **Collection Awareness & Boundary Isolation:** Support multiple independently managed collections (e.g. `sebi_cscrf`, `sebi_lodr`, `internal_policies`), guaranteeing that retrieval and synthesis never leak cross-collection evidence.
- **Data-Driven Ingestion:** Ingest new documents and collections via declarative manifests without modifying application code.
- **Pluggable Relationship Policies:** Provide supersession-aware temporal resolution for regulatory corpora while supporting collections that have no amendment relationships via a passthrough policy.
- **Fail-Closed Grounding:** Verifiable claim attribution with automated fallback to verbatim extracted excerpts or explicit abstention (`refuse: true`) when evidence is inconclusive or contradictory.
- **Permanent Regression Anchoring:** The validated 5-document SEBI CSCRF corpus, its 9 verified edges, and 32 golden benchmark queries serve as a permanent regression anchor across all platform evolutions.

### 3.2. Scope Staging (Phases 0–4)
1. **Phase 0 — Documentation & Baseline:** Reconcile documentation with verified codebase state; define shared roadmap, architectural invariants, and approval boundaries.
2. **Phase 1 — Manifest-Driven Ingestion:** Introduce declarative JSON/YAML manifests (`data/manifests/*.json`) for local, zero-cost document ingestion without editing Python files.
3. **Phase 2 — Collection-Aware Retrieval & Isolation:** Introduce `collection_id` across API contracts and retrieval filters; enforce collection-scoped index partitioning and zero cross-collection evidence leakage.
4. **Phase 3 — Generalized Relationships & Conditional Metadata Persistence:** Support pluggable relationship policies (supersession vs. passthrough); evaluate relational database adoption for metadata/audit history conditional on demonstrated need.
5. **Phase 4 — Broader Evaluation, Reliability & Deployment:** Expand benchmarks across multi-collection workloads, test failure recovery, and align zero-cost production hosting.

### 3.3. Non-Goals & Disclaimers
- **Not Legal Advice:** The platform provides information discovery and regulatory intelligence; it does not replace professional legal counsel or formal compliance audits.
- **No Autonomous Legal Interpretation:** The system extracts, synthesizes, and attributes text based on explicit evidence; it does not adjudicate legal disputes or speculate on unstated regulatory intent.
- **No Hallucinated Fallback:** The system will never fabricate an answer to avoid showing an abstention.
- **No Mandatory Paid Infrastructure:** The platform will not require paid vector databases, paid API subscriptions, or mandatory hosted cloud infrastructure to operate.

---

## 4. User Personas & Core Journeys

| Persona | Primary Goal | Critical Need | Supported Phase |
| :--- | :--- | :--- | :--- |
| **Compliance Officer** | Verify current applicable mandates for their specific entity category (e.g. Qualified RE vs. MII). | Must see current amended requirements and effective deadlines, not superseded initial drafts. | Implemented for SEBI CSCRF; Generalized in Phase 2. |
| **Legal / Policy Analyst** | Trace regulatory evolution over time across circular amendment chains. | Needs historical `as_of` date querying and clear side-by-side citations of amending clauses. | Implemented for SEBI CSCRF; Pluggable in Phase 3. |
| **Internal Auditor** | Verify statutory basis of compliance controls against official circular text. | Requires exact page-level citations, direct PDF links, and cryptographic document hashes. | Implemented and Verified. |
| **Platform Administrator** | Ingest new circulars or entire document collections without redeploying code. | Needs declarative manifest ingestion, automated validation, and repeatable indexing. | Phase 1 (Local Manifest) & Phase 3 (Admin API). |

---

## 5. Functional Requirements (FR)

### Collection & Document Ingestion
- **FR-1 Manifest-Driven Registration (Phase 1):** Ingest documents from declarative manifests (`data/manifests/{collection_id}.json`) specifying document ID, title, relative PDF path, optional dates, and source metadata without editing Python code.
- **FR-2 Provenance & Integrity (Implemented and Verified):** Automatically compute SHA-256 hash, record retrieval timestamp, extract text/tables via PyMuPDF, and log page-level extraction quality (`OK`, `SPARSE`, `EMPTY`).
- **FR-3 Deterministic Chunking (Implemented and Verified):** Split document text into bounded chunks with persistent chunk IDs (`{doc_id}_p{page_start}_{chunk_idx}`), dual-page provenance (PDF page and printed page), and word-count boundaries (350 words, 40 overlap).
- **FR-4 Malformed PDF & Path Traversal Protection (Phase 1):** Validate that PDF paths resolve within approved corpus roots and reject path traversal attempts (`../`), unreadable files, or malformed payloads with clean error reporting.

### Collection Scoping & Retrieval
- **FR-5 Collection-Scoped Querying (Phase 2):** Accept `collection_id` as a required parameter on query contracts. Reject unknown or ambiguous collections with HTTP 404 or 422.
- **FR-6 Mandatory Collection Isolation (Phase 2):** Enforce strict collection filtering during lexical search, dense vector retrieval, RRF candidate fusion, reranking, and context injection. Under no circumstances may chunks from collection $B$ appear in results for collection $A$.
- **FR-7 Hybrid Retrieval (Implemented and Verified):** Combine BM25 lexical ranking and dense vector similarity (`BAAI/bge-small-en-v1.5`) using Reciprocal Rank Fusion ($k=60$) over atomic index snapshots.

### Regulatory Relationships & Temporal Resolution
- **FR-8 Directed Relationship Modeling (Implemented for CSCRF):** Enforce directed edges (`source_document_id -> RELATIONSHIP -> target_document_id`) supporting `AMENDS`, `RESCINDS`, and `REFERENCES`.
- **FR-9 Provision Scoping (Implemented for CSCRF):** Enforce `affected_provisions` for partial modifications so unaffected provisions retain their base validity.
- **FR-10 Pluggable Policy Selection (Phase 3):** Support collections enforcing regulatory supersession via `RegulatorySupersessionPolicy`, while supporting non-regulatory collections (manuals, whitepapers) via `PassthroughPolicy` without forcing `UNRESOLVED` warnings or spurious abstentions.

### Grounding, Synthesis & Safety Gate
- **FR-11 Fail-Closed Grounding & Citation Validation (Implemented and Verified):** Mechanically verify that every cited chunk ID and asserted claim matches retrieved candidate passages.
- **FR-12 Structured Refusal Rubric (Implemented and Verified):** Return `refuse: true` / `disposition: CONTROLLED_ABSTENTION` when evidence is missing (`insufficient_evidence`), citations fail (`citation_verification_failed`), provisions conflict (`conflicting_evidence`), questions are out-of-domain (`out_of_domain`), or temporal dates are ambiguous (`temporal_uncertainty`).
- **FR-13 Extractive Fallback (Implemented and Verified):** If hosted LLM synthesis fails, times out, or generates ungrounded claims, fall back seamlessly to verified verbatim extracted excerpts or controlled abstention.

---

## 6. Implementation Status Matrix

The features below have been audited against the repository codebase as of **October 21, 2026** (supported by 445 passing tests, 9 skipped, 0 failed in `backend/tests/` via pytest):

| Subsystem / Feature | Implementation Status | Code Evidence & Technical Notes |
| :--- | :--- | :--- |
| **CSCRF 5-Circular Baseline Corpus** | `IMPLEMENTED AND VERIFIED` | 5 PDFs in `data/raw/`, 168 chunks in `data/processed/`, 9 verified edges in `docs/cscrf-relationship-audit.md`. Permanent regression anchor. |
| **Deterministic PDF Extractor & Chunker** | `IMPLEMENTED AND VERIFIED` | `backend/app/ingestion/extractor.py` (PyMuPDF) and `chunker.py` (sliding window 350 words, 40 overlap). Dual-page provenance and SHA-256 verified. |
| **In-Memory Hybrid Search (BM25 + BGE-Small + RRF)** | `IMPLEMENTED AND VERIFIED` | `backend/app/retrieval/service.py` and `rrf.py`. BM25Okapi + NumPy cosine similarity on 384-dim normalized vectors from `BAAI/bge-small-en-v1.5`. |
| **Snapshot Index Publication Engine** | `IMPLEMENTED AND VERIFIED` | `backend/app/retrieval/index_pipeline.py`. Atomic swap of `active_snapshot.json` with hot-reload in `RetrievalService.reload_if_stale()`. |
| **Temporal Supersession Graph Resolver** | `IMPLEMENTED AND VERIFIED` | `backend/app/grounding/graph_resolver.py` and `backend/app/schemas/cscrf_graph_data.py`. Directed multi-hop traversal with provision-scoped amendments. |
| **Specificity-Aware Relevance Filter** | `IMPLEMENTED AND VERIFIED` | `backend/app/grounding/relevance.py`. Specific query token coverage scoring and candidate filtering for compliance queries. |
| **Answer Safety Gate & Refusal Rubric** | `IMPLEMENTED AND VERIFIED` | `backend/app/grounding/safety_gate.py` and `validator.py`. Mechanical claim verification, verbatim substring matching, and 5-point refusal rubric. |
| **FastAPI REST API (`/query`, `/health`, `/ready`)** | `IMPLEMENTED AND VERIFIED` | `backend/app/main.py` and `backend/app/api/v1/query.py`. Fully typed Pydantic contracts with rate limiting and structured error handling. |
| **In-Memory Rate Limiter** | `IMPLEMENTED AND VERIFIED` | `backend/app/core/rate_limiter.py`. Fixed-window sliding token limiter for zero-cost single-instance execution. |
| **Grounded LLM Synthesis & Fallback** | `PARTIALLY IMPLEMENTED` | `backend/app/synthesis/groq_provider.py` and `gemini_provider.py` implemented with fail-closed extractive fallback. Verified in unit tests. |
| **Dual-Engine Relational Persistence (PostgreSQL)** | `IMPLEMENTED, VERIFICATION INCOMPLETE` | `backend/app/persistence/` contains SQLAlchemy models and Alembic migrations. Verified in parity tests, but NOT active runtime default. |
| **Manifest-Driven Ingestion Engine** | `PLANNED (Phase 1)` | Declarative JSON manifests replacing hardcoded `corpus_registry.py` without requiring database services. |
| **Collection-Aware Retrieval & Isolation** | `PLANNED (Phase 2)` | Explicit `collection_id` scoping in query contracts and strict index partitioning to prevent cross-collection leakage. |
| **Pluggable Relationship Policy Engine** | `PLANNED (Phase 3)` | Dynamic policy configuration separating regulatory supersession from passthrough collections. |
| **Conditional Metadata Persistence (Neon DB)** | `PROPOSED / REQUIRES APPROVAL` | Relational persistence for dynamic multi-user ingestion, edge curation, and query audit ledgers (Phase 3). |
| **Distributed Cache & Queue (Redis / Upstash)** | `DEFERRED / REQUIRES APPROVAL` | Celery background queues and distributed caching. Deferred while single-instance in-memory limiter suffices. |

---

## 7. Non-Functional & Security Requirements

1. **Server-Side Secret Isolation:** All API credentials, admin keys, and optional provider keys reside strictly in backend environment variables (`.env`). No credentials are ever transmitted to the frontend or exposed in API errors.
2. **Data Privacy & Minimal Logging:** Query texts are processed in memory and never persisted with personally identifiable information (PII). Model prompts are bounded by retrieved context.
3. **Local Determinism & Zero-Cost Execution:** The entire platform must remain fully runnable, testable, and demonstrable on standard local workstations without requiring paid subscriptions, cloud accounts, or live internet connections.
4. **Input Sanitization & Untrusted Text:** Extracted PDF content and user queries are treated as untrusted input. Prompt injection attempts inside retrieved evidence are isolated and prevented from overriding safety policies.
5. **Graceful Infrastructure Degradation:** Upstream failures (e.g. LLM timeouts, network errors) return clean HTTP 5xx codes and are never disguised as successful regulatory refusals (`refuse: true`).
