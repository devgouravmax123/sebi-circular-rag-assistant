# Architecture Decision Records (ADRs)

This document records the architectural decisions, evaluation rationales, and accepted constraints for the Document Q&A Platform as it evolves from a SEBI CSCRF-specific pilot into a generalized, collection-aware regulatory document Q&A platform.

---

## Shared Roadmap (Phases 0–4) & Historical Context Note

> [!NOTE]
> **Roadmap Standardization:**
> The project forward architecture standardizes on **Phases 0 through 4**:
> - **Phase 0:** Documentation, baseline reconciliation, and invariant definition.
> - **Phase 1:** Manifest-driven local ingestion (eliminating code-level corpus hardcoding without external database dependencies).
> - **Phase 2:** Collection-aware retrieval, index partitioning, and isolation.
> - **Phase 3:** Generalized relationships, pluggable temporal policies, and conditional metadata persistence.
> - **Phase 4:** Multi-collection evaluation, reliability hardening, and production deployment alignment.
>
> References to legacy "Phase 8A–8G" numbering in earlier decisions are historical artifacts. They reflect experimental or preliminary work (e.g. Phase 8A PostgreSQL models, Phase 8B ingestion CLI, Phase 8C domain filters) and are mapped to their respective Phases 0–4 below.

---

## Historical ADRs (ADR-001 through ADR-014)

### ADR-001: React + TypeScript + Vite for Frontend
**Status:** Accepted (Implemented and Verified)
**Decision:** Build the client user interface using React, TypeScript, and Vite.
**Rationale:** Modern component model with strict typing and rapid local development reload. Provides responsive layouts, expandable citations, and transparent state management.
**Trade-off:** Requires a Node.js development runtime separate from the Python backend.

---

### ADR-002: FastAPI for Backend API Services
**Status:** Accepted (Implemented and Verified)
**Decision:** Implement the API layer using Python and FastAPI with Pydantic v2 schemas.
**Rationale:** Native integration with Python scientific and retrieval libraries (PyMuPDF, sentence-transformers, NetworkX) while providing typed request/response contracts and OpenAPI documentation.
**Trade-off:** Python async runtime requires careful management when executing CPU-intensive embedding or parsing routines.

---

### ADR-003: Hybrid Retrieval (BM25 + Dense Vectors + RRF)
**Status:** Accepted (Implemented and Verified)
**Decision:** Combine lexical search (BM25) with dense vector search (`BAAI/bge-small-en-v1.5`) using Reciprocal Rank Fusion ($k=60$).
**Rationale:** Regulatory queries require both exact alphanumeric precision (for circular numbers, section codes, compliance terms) and semantic generalization for high-level compliance queries. RRF merges rank positions without normalizing disparate score distributions.
**Trade-off:** Requires maintaining and indexing dual retrieval channels.

---

### ADR-004: Local Embedding Model Selection (`BAAI/bge-small-en-v1.5`)
**Status:** Accepted (Implemented and Verified)
**Decision:** Standardize on `BAAI/bge-small-en-v1.5` (384-dimensional normalized vectors) evaluated via in-memory NumPy cosine similarity for local dense retrieval.
**Rationale:** Highly effective for English semantic retrieval, fast CPU inference, zero API cost, and proven benchmark performance.
**Trade-off:** Requires local sentence-transformers inference runtime.

---

### ADR-005: Standardized Graph Directionality & Provision Scoping
**Status:** Accepted (Implemented and Verified for SEBI CSCRF)
**Decision:** Standardize all graph edges strictly as `source_document_id (acting / newer) -> RELATIONSHIP -> target_document_id (affected / older)` with explicit provision scoping (`affected_provisions`).
**Rationale:** Eliminates directional ambiguity during graph traversal. Prevents partial amendments (e.g., an amendment to a single paragraph) from incorrectly invalidating base master circulars.
**Trade-off:** Requires granular, clause-level relationship auditing.

---

### ADR-006: File-Based Pilot Baseline Evolving to Relational DB
**Status:** Accepted (Historical; Reconciled with Roadmap)
**Historical Context:** Originally proposed moving immediately to PostgreSQL in legacy Phase 8A.
**Reconciled Status:** The file-based snapshot architecture (`data/processed/`, `data/indexes/snapshots/`) remains the active production runtime default. Relational persistence is decoupled from document expansion: Phase 1 enables data-driven ingestion via local manifests without requiring PostgreSQL. Relational database adoption is deferred to Phase 3 and made conditional on demonstrated operational need.

---

### ADR-007: Redis for Caching, Rate Limiting & Background Queues
**Status:** Deferred / Requires User Approval
**Historical Context:** Proposed for legacy Phase 8F.
**Reconciled Status:** The application currently runs an efficient in-memory rate limiter (`backend/app/core/rate_limiter.py`). Redis is not required for single-instance hosting or local execution. Introducing Redis or Upstash is deferred until multi-worker horizontal scaling or distributed caching is explicitly authorized.

---

### ADR-008: Controlled Cost Management & Zero Mandatory Paid Tiers
**Status:** Required Constraint (Accepted)
**Decision:** The platform must remain fully developable, testable, and demonstrable in a zero-cost local environment. No mandatory paid APIs or billable cloud resources may be introduced without explicit approval.
**Rationale:** Guarantees reproducibility, open-source accessibility, and deterministic test execution.

---

### ADR-009: Fail-Closed Grounding & Explicit Refusal Rubric
**Status:** Accepted (Implemented and Verified)
**Decision:** Enforce mechanical claim-to-chunk provenance checks and verbatim quotation verification via `AnswerSafetyGate`, returning structured refusals (`refuse: true`) according to an explicit 5-point refusal rubric (`insufficient_evidence`, `citation_verification_failed`, `conflicting_evidence`, `out_of_domain`, `temporal_uncertainty`). Distinguish domain refusals from HTTP 4xx/5xx infrastructure failures.
**Rationale:** Generative LLMs hallucinate citations and claims. In regulatory intelligence, abstention is vastly preferable to inaccurate compliance advice.

---

### ADR-010: Phased Dataset Progression & Regression Anchoring
**Status:** Accepted (Implemented and Verified)
**Decision:** Advance evaluation through disciplined benchmark stages. The CSCRF pilot 5-circular corpus and its 32 golden queries (`evaluation/data/gold_evaluation_dataset.json`) are retained as a permanent regression anchor across all future platform expansions.
**Rationale:** Prevents premature scaling and ensures backward compatibility as new regulatory domains and general collections are introduced.

---

### ADR-011: Relational Persistence with PostgreSQL & pgvector
**Status:** Implemented (Persistence Tier), Verification Incomplete (Not Active Runtime Default)
**Historical Context:** Implemented in `backend/app/persistence/` via SQLAlchemy 2.x models and Alembic migration `0001_initial_persistence_foundation`.
**Reconciled Status:** While code and schema exist, PostgreSQL is not the default runtime path. The default path remains file-backed snapshot retrieval. Live database integration remains optional and conditional (Phase 3).

---

### ADR-012: Evidence-Constrained Hosted LLM Synthesis Layer with Fail-Closed Fallback
**Status:** Partially Implemented & Verified
**Decision:** Support external synthesis providers (Gemini, Groq, Ollama) generating natural-language answers strictly bounded by retrieved context chunks, with fail-closed fallback to extractive excerpts or structured refusals if citation validation fails.
**Current State:** `GroqSynthesisProvider` and `MockSynthesisProvider` are implemented in `backend/app/synthesis/`. The system fails closed to extractive excerpts or refusal if verification fails or quotas are exceeded.

---

### ADR-013: Pluggable Source Connectors for Regulatory Extensibility
**Status:** Proposed (Design Target)
**Decision:** Propose implementing an abstract `BaseRegulatorConnector` interface for web crawling and automated document discovery across regulators.
**Current State:** Not implemented in active runtime. Manual PDF placement and manifest-driven ingestion (Phase 1) take precedence over automated crawlers.

---

### ADR-014: Dual-Engine Storage Abstraction & Immutable Document Versioning
**Status:** Implemented (Persistence Tier), Verification Incomplete
**Decision:** Provide repository interfaces separating logical documents from immutable physical versions (`document_versions`), allowing file-based execution and optional PostgreSQL execution.
**Reconciled Status:** Validated in isolated persistence tests (`test_persistence_parity.py`), but runtime query pipeline currently executes against `data/indexes/snapshots/` index files.

---

## New Architecture Decision Records (ADR-015 through ADR-020)

### ADR-015: Manifest-Driven Local Ingestion (Phase 1)
**Status:** Accepted
**Context:** Adding new documents currently requires hardcoding entries in `backend/app/ingestion/corpus_registry.py` (`SEBI_CSCRF_CORPUS`).
**Decision:** Replace static Python dictionary registries with data-driven JSON/YAML manifests stored in `data/manifests/*.json`. The ingestion pipeline reads manifests, extracts text via PyMuPDF, chunks deterministically, validates SHA-256 hashes, and outputs processed artifacts locally without requiring an external database or network calls.
**Rationale:** Solves the first blocker to generalization cleanly within the zero-cost constraint.
**Consequences:** Enables adding arbitrary PDFs by supplying a manifest file; preserves existing SEBI document IDs and chunk provenances.

---

### ADR-016: Collection Abstraction & Mandatory Retrieval Isolation (Phase 2)
**Status:** Accepted
**Context:** Currently, `RetrievalService` loads all chunks into a flat list and indexes them together. There is no `collection_id` concept, causing potential cross-collection evidence leakage.
**Decision:** Introduce `collection_id` as a first-class entity across ingestion, indexing, and query contracts (`QueryRequest.collection_id`). Retrieval, RRF fusion, reranking, graph traversal, and synthesis MUST be strictly isolated within the boundaries of the requested collection. Searching across all collections simultaneously without explicit collection selection is forbidden (fail-closed).
**Rationale:** Prevents evidence from unrelated corpora (e.g. internal manuals vs. SEBI circulars) from contaminating answers or citations.
**Consequences:** Requires collection-scoped indexes (or strict pre-filtering) and collection-aware API contracts.

---

### ADR-017: Pluggable Relationship & Temporal Policies (Phase 3)
**Status:** Accepted
**Context:** Current safety gate logic assumes every document may participate in an `AMENDS` or `RESCINDS` graph, and `relevance.py` contains hardcoded SEBI circular sequence numbers. Non-regulatory documents (technical manuals, research papers) lack amendment chains and publication dates.
**Decision:** Decouple regulatory supersession from core grounding. Define collection-level relationship policies:
1. `RegulatorySupersessionPolicy`: Applies directed graph traversal (`AMENDS`, `RESCINDS`, `REFERENCES`) and temporal `as_of` date filtering (used for SEBI CSCRF).
2. `PassthroughPolicy`: Skips supersession and temporal filtering for collections without amendment chains.
Core claim grounding and citation verification remain mandatory across all policies.
**Rationale:** Allows general document Q&A without breaking or weakening the rigorous safeguards built for SEBI regulations.

---

### ADR-018: Local Index Snapshots as Default Persistence Tier
**Status:** Accepted
**Context:** Storing dense embeddings and chunks in hosted vector databases or managed PostgreSQL instances introduces network latency, cold starts, and potential monthly hosting costs.
**Decision:** Standardize on atomic local file snapshots (`data/indexes/snapshots/{snapshot_id}/`) containing BM25 inverted indexes, NumPy dense vectors, and chunk catalogs as the primary persistence mechanism for the platform.
**Rationale:** Provides sub-10ms retrieval latency, consumes minimal memory (<100MB for pilot), requires zero external infrastructure, and guarantees zero hosting costs.

---

### ADR-019: Shared Roadmap Standardization (Phases 0–4)
**Status:** Accepted
**Context:** Documentation previously mixed Phase 8A–8G references with new roadmap tasks, causing confusion regarding project priorities and implementation status.
**Decision:** Standardize all project documentation, issue tracking, and roadmap discussions on Phases 0 through 4. Legacy Phase 8A–8G labels are retained solely as historical annotations.
**Rationale:** Establishes unambiguous communication across engineering documentation.

---

### ADR-020: Conditional Metadata Database Adoption (Phase 3)
**Status:** Accepted
**Context:** Proposals have been made to introduce Neon PostgreSQL or external relational databases for document metadata and job queues.
**Decision:** External database adoption is conditional upon demonstrated operational need (e.g., concurrent multi-user write requirements, durable cross-session audit ledgers). Database adoption must not be mandated for local development, CI testing, or read-only query serving. Vector storage remains in local snapshot files.
**Rationale:** Preserves zero-cost deployment and rapid local developer iteration while providing an optional relational path for production deployments.

---

## Architectural Decisions Requiring User Approval

The following items are explicitly **NOT** approved by these ADRs and require formal user review and approval before any implementation:

1. **Target Cloud Production Hosting:** Selection between OCI Always Free ARM64 VM (single containerized host) vs. Render (Web Service) + Neon + Upstash.
2. **Mandatory External Database:** Mandating Neon PostgreSQL (or any external database) as a required runtime dependency for ingestion or query serving.
3. **External Vector Store Migration:** Migrating dense vector retrieval from local NumPy/FAISS files to pgvector or cloud vector databases.
4. **Distributed Cache / Message Queue:** Introducing Upstash Redis, Redis, or Celery as mandatory services.
5. **Paid API Spend:** Provisioning or requiring paid LLM API subscriptions.
6. **Altering Safety Gate Refusal Rubric:** Modifying or relaxing any of the five refusal conditions for the SEBI CSCRF collection.
7. **Modifying Permanent Evaluation Baseline:** Changing the 32 golden queries or expected outputs in `evaluation/data/gold_evaluation_dataset.json`.
