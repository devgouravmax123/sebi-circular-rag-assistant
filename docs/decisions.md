# Architecture Decision Records (ADRs)

This document records the key architectural choices, evaluation rationales, and accepted constraints for the SEBI Regulatory Intelligence Platform.

---

## ADR-001: React + TypeScript + Vite for Frontend
**Status:** Accepted
**Decision:** Build the client user interface using React, TypeScript, and Vite.
**Rationale:** Modern component model with strict typing and rapid local development reload. Provides responsive layouts, expandable citations, and transparent state management.
**Trade-off:** Requires a Node.js development runtime separate from the Python backend.

---

## ADR-002: FastAPI for Backend API Services
**Status:** Accepted
**Decision:** Implement the API layer using Python and FastAPI with Pydantic v2 schemas.
**Rationale:** Native integration with Python scientific and retrieval libraries (PyMuPDF, sentence-transformers, NetworkX) while providing typed request/response contracts and OpenAPI documentation.
**Trade-off:** Python async runtime requires careful management when executing CPU-intensive embedding or parsing routines.

---

## ADR-003: Hybrid Retrieval (BM25 + Dense Vectors + RRF)
**Status:** Accepted
**Decision:** Combine lexical search (BM25) with dense vector search (`BAAI/bge-small-en-v1.5`) using Reciprocal Rank Fusion ($k=60$).
**Rationale:** Regulatory queries require both exact alphanumeric precision (for circular numbers like `CIR/2025/119`, section names, and compliance terms) and semantic generalization for high-level compliance queries. RRF merges rank positions without normalizing disparate score distributions.
**Trade-off:** Requires maintaining and indexing dual retrieval channels.

---

## ADR-004: Local Embedding Model Selection (`BAAI/bge-small-en-v1.5`)
**Status:** Accepted
**Decision:** Standardize on `BAAI/bge-small-en-v1.5` (384-dimensional normalized vectors) evaluated via in-memory NumPy cosine similarity for local dense retrieval.
**Rationale:** Highly effective for English semantic retrieval, fast CPU inference, zero API cost, and proven benchmark performance.
**Trade-off:** Requires local sentence-transformers inference runtime.

---

## ADR-005: Standardized Graph Directionality & Provision Scoping
**Status:** Accepted
**Decision:** Standardize all graph edges strictly as `source_document_id (acting / newer) -> RELATIONSHIP -> target_document_id (affected / older)` with explicit provision scoping (`affected_provisions`).
**Rationale:** Eliminates directional ambiguity during graph traversal. Prevents partial amendments (e.g., an amendment to a single paragraph) from incorrectly invalidating base master circulars.
**Trade-off:** Requires granular, clause-level relationship auditing.

---

## ADR-006: File-Based Pilot Baseline Evolving to Relational DB
**Status:** Accepted (Updated)
**Decision:** The initial CSCRF 5-document pilot operates on versioned JSON/NumPy artifacts in `data/processed/` and `data/indexes/`. The forward architecture proposes transition to PostgreSQL + pgvector (Phase 8A) with dual-mode fallback for local testing.
**Rationale:** Allowed rapid vertical-slice validation and regression baselining without external database dependencies, while preparing a scalable foundation for multi-domain expansion.
**Trade-off:** File stores lack atomic concurrent write capabilities needed for multi-user production ingestion.

---

## ADR-007: Redis for Caching, Rate Limiting & Background Queues
**Status:** Proposed (Planned for Phase 8F)
**Decision:** Introduce Redis as an asynchronous task queue broker (Celery) and query cache in Phase 8F. Local pilot operates in-process.
**Rationale:** Useful for offloading long-running PDF downloads, OCR, and vector embeddings from the interactive HTTP request path in hosted production.
**Trade-off:** Introduces an infrastructure dependency during containerized deployment.

---

## ADR-008: Controlled Cost Management & Zero Mandatory Paid Tiers
**Status:** Required Constraint
**Decision:** The system must remain fully functional in a zero-cost local environment. No mandatory paid APIs or billable cloud resources may be introduced without explicit approval.
**Rationale:** Guarantees reproducibility, open-source accessibility, and deterministic test execution.

---

## ADR-009: Fail-Closed Grounding & Explicit Refusal Rubric
**Status:** Accepted
**Decision:** Enforce mechanical claim-to-chunk provenance checks and verbatim quotation verification via `AnswerSafetyGate`, returning structured refusals (`refuse: true`) according to an explicit 5-point refusal rubric. Distinguish domain refusals from HTTP 4xx/5xx infrastructure failures.
**Rationale:** Generative LLMs hallucinate citations and claims. In regulatory intelligence, abstention is vastly preferable to inaccurate compliance advice.

---

## ADR-010: Phased Dataset Progression & Regression Anchoring
**Status:** Accepted
**Decision:** Advance evaluation through disciplined stages: Stage A (synthetic fixtures), Stage B (CSCRF pilot micro-slice), Stage C (multi-domain expansion), and Stage D (golden benchmark). The CSCRF pilot is retained as a permanent regression anchor.
**Rationale:** Prevents premature scaling and ensures backward compatibility as new regulatory domains are introduced.

---

## ADR-011: Relational Persistence with PostgreSQL & pgvector (Phase 8A)
**Status:** Accepted (Implemented in Phase 8A)
**Decision:** Adopted PostgreSQL with the `pgvector` extension as the canonical production persistence layer for documents, versions, provisions, chunks, vector embeddings, relationship edges, and ingestion jobs.
**Rationale:** Unifies relational metadata (circular numbers, dates, departments), graph relationships (foreign key edges), and dense vector similarity search into a single ACID-compliant database. Eliminates index desynchronization risks inherent in separate vector and relational stores.
**Implementation:** Implemented in `backend/app/persistence/` via SQLAlchemy 2.x declarative models and Alembic migration `0001_initial_persistence_foundation`. Includes automatic column width expansion (`_ensure_version_table_capacity`) preventing Alembic revision ID truncation on PostgreSQL.

---

## ADR-012: Evidence-Constrained Hosted LLM Synthesis Layer with Fail-Closed Fallback (Phase 8E)
**Status:** Proposed (Design Target — Does Not Authorize Paid API Spend)
**Decision:** Propose introducing a provider-agnostic LLM adapter (Gemini, Groq, Bedrock, Ollama) generating natural-language answers strictly bounded by retrieved context chunks. If citation alignment fails, or if upstream provider quotas are exhausted, the system automatically falls back to verified extractive excerpts.
**Rationale:** Combines the readability and synthesis capabilities of modern LLMs with absolute regulatory safety guarantees.
**Trade-off:** Dependent on external model API latency and rate limits; mitigated by graceful extractive fallback.

---

## ADR-013: Pluggable Source Connectors for Regulatory Extensibility (Phase 8B / 8G)
**Status:** Proposed (Design Target)
**Decision:** Propose implementing an abstract `BaseRegulatorConnector` interface with concrete implementations for SEBI, and future extension to secondary regulators (e.g. RBI, IRDAI) strictly following operational validation of the hosted SEBI platform.
**Rationale:** Enables horizontal expansion across regulatory bodies while preserving unified chunking, temporal resolution, and grounding pipelines.
**Trade-off:** Varying publication formats across regulators will require specialized layout parsing logic.

---

## ADR-014: Dual-Engine Storage Abstraction & Immutable Document Versioning
**Status:** Accepted (Implemented in Phase 8A)
**Decision:** Implement a dual-engine repository abstraction (`StorageRepositoryFactory`) providing complete interface parity between the zero-dependency local file system (`FileStorageRepositoryFactory`) and PostgreSQL + pgvector (`PostgresStorageRepositoryFactory`). Separate logical regulatory documents (`regulatory_documents`) from immutable physical versions (`document_versions`).
**Rationale:**
1. Zero-dependency local developer execution: Unit tests, pilot regression runs, and CI pipelines run instantly without requiring a live PostgreSQL service.
2. Production reliability: Hosted deployment seamlessly switches to PostgreSQL when `DATABASE_URL` is set.
3. Immutability & provenance: Regulatory circulars modified or re-published with changed content hashes generate new immutable physical version records rather than mutating historical records in-place, preserving legal auditability.
**Trade-off:** Requires dual-engine parity testing to ensure consistent behavior across both backends. Parity verified across 54 dedicated persistence tests.
