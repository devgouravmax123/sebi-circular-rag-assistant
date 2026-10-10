# Architecture Decision Records

This file records decisions and their rationale. Update it when a decision is confirmed or changed.

## ADR-001: React + TypeScript + Vite for frontend
**Status:** Proposed  
**Decision:** Use React, TypeScript, and Vite.  
**Rationale:** Common frontend tooling with a clear local development workflow.  
**Trade-off:** Adds a separate Node-based build environment.

## ADR-002: FastAPI for backend
**Status:** Proposed  
**Decision:** Use Python and FastAPI with Pydantic schemas.  
**Rationale:** Good fit for Python retrieval/document-processing libraries and typed API contracts.

## ADR-003: Hybrid retrieval
**Status:** Proposed  
**Decision:** Evaluate BM25 and FAISS dense retrieval, combined with RRF.  
**Rationale:** Lexical search helps exact circular IDs and phrases; dense search helps semantic matches. RRF combines ranks without directly mixing incomparable raw scores.  
**Trade-off:** More indexing and evaluation work than one retriever.

## ADR-004: Candidate embedding model selection
**Status:** Proposed  
**Decision:** Select `sentence-transformers/all-MiniLM-L6-v2` as the initial candidate embedding model, subject to local CPU/RAM resource verification and retrieval baseline testing. Answers are generated via backend-hosted LLM adapter.  
**Rationale:** Standard, lightweight (~80MB), and runs efficiently on CPU without incurring API cost.  
**Trade-off:** Model download and CPU inference consume local resources. Must verify suitability prior to finalizing.

## ADR-005: Standardized supersession graph direction and scoping
**Status:** Accepted  
**Decision:** Standardize all graph edges as `Newer/acting circular -> RELATIONSHIP -> Older/affected circular` with explicit distinction between whole-document and provision-level supersession (`affected_provisions`).  
**Rationale:** Eliminates ambiguity during graph traversal and date-based status evaluation. Preserves unaffected provisions during partial amendments.  
**Trade-off:** Edge extraction requires manual verification; absence of an edge does not prove a circular is in force.

## ADR-006: File-based artifacts before database
**Status:** Proposed  
**Decision:** Start with manifest/JSONL files and reproducible indexes.  
**Rationale:** Lower setup complexity and no required hosted database.  
**Trade-off:** Not intended for concurrent multi-user production writes.

## ADR-007: Redis is optional
**Status:** Proposed  
**Decision:** Start with in-process cache/rate limiting; consider Redis only after a measured need and verification of a genuinely free hosted option.  
**Rationale:** Avoid unnecessary infrastructure and free-tier dependency risk.

## ADR-008: Strict ₹0 required spend and quota management
**Status:** Required constraint  
**Decision:** Zero required spend. No mandatory paid service, automatic paid fallback, or billable tier.  
**Rationale:** The project must be buildable, runnable, and demoable at ₹0.  
**Operational note:** Free LLM quotas, hosting limits, data retention policies, and privacy terms must be verified before integration. API keys remain strictly on the backend.

## ADR-009: Citation validation, unified schema, and domain refusal in code
**Status:** Accepted  
**Decision:** Enforce a unified citation schema across API and architecture (`chunk_id`, `document_id`, `title`, nullable `issue_date`, `page_start`, `page_end`, `source_url`), perform strict code-level citation validation, and return structured refusals (`refuse: true`) based on an explicit refusal rubric. Distinguish domain refusals from upstream infrastructure failures (HTTP 4xx/5xx).  
**Rationale:** Prompt instructions alone cannot ensure citation truthfulness or graceful abstention.

## ADR-010: Phased dataset progression
**Status:** Accepted  
**Decision:** Advance datasets through 4 strict stages: Stage A (synthetic fixtures), Stage B (3–5 verified circular micro-slice), Stage C (expanded verified corpus), Stage D (golden benchmark with dev/test splits).  
**Rationale:** Prevents premature scaling and ensures high-quality verified ground truth.
