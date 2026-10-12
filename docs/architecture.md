# System Architecture

## 1. High-Level Architecture Overview

The **SEBI Regulatory Intelligence Platform** is structured as a modular, hosted system separating offline document ingestion, hybrid persistence, dynamic relationship management, and online query orchestration.

```text
====================================================================================================
                                      OFFLINE / INGESTION TIER
====================================================================================================

 Official PDF Sources
 (SEBI, future regulators)
         |
         v
 +------------------------------------------------------------------------------------------------+
 | Dynamic Document Ingestion Engine (Phase 8B)                                                   |
 | - Pluggable Regulator Connectors (SEBI; future RBI)                                            |
 | - PDF Download & SHA-256 Checksum Deduplication                                                |
 | - Text Extraction (PyMuPDF) & Table Handling (pdfplumber)                                      |
 | - Deterministic Chunking (350 tokens, 40 overlap) with Page Boundaries                          |
 +------------------------------------------------------------------------------------------------+
         |                                                 |
         v                                                 v
 Document & Chunk Records                          Extracted Edge Candidates
         |                                                 |
         |                                                 v
         |                                 +----------------------------------------------+
         |                                 | Regulatory Relationship Manager (Phase 8D)   |
         |                                 | - Preamble & Citation Parser                 |
         |                                 | - Human-in-the-Loop (HITL) Review Interface  |
         |                                 | - Verbatim Evidence Ledger & Audit Log       |
         |                                 +----------------------------------------------+
         |                                                 |
         +------------------------+------------------------+
                                  |
                                  v
====================================================================================================
                                  DUAL PERSISTENCE TIER (Phase 8A Implemented)
====================================================================================================
 +------------------------------------------------------------------------------------------------+
 | Repository Abstraction Layer (StorageRepositoryFactory)                                        |
 | - FileStorageRepositoryFactory (Default: data/processed/ JSON, NumPy dense_index.npy)           |
 | - PostgresStorageRepositoryFactory (Activated when DATABASE_URL is set)                        |
 +------------------------------------------------------------------------------------------------+
 | PostgreSQL + pgvector (Phase 8A Implemented)                                                   |
 | - `regulators` & `regulatory_domains` (Taxonomic registration)                                 |
 | - `regulatory_documents` & `document_versions` (Logical vs immutable physical versions)       |
 | - `provisions` (Clause/section code, title, PDF/printed page mapping)                          |
 | - `chunks` & `chunk_embeddings` (Text slices, 384-dim pgvector with exact <=> cosine search)   |
 | - `regulatory_edges` (Directed relationships, review status, verified edge protection)        |
 | - `ingestion_jobs` (Lifecycle status: pending, running, completed, failed)                     |
 +------------------------------------------------------------------------------------------------+
 | Redis Cache & Worker Broker (Phase 8F Planned)                                                 |
 | - Celery Background Task Queue for Ingestion & Embedding Generation                            |
 | - Query Cache & Rate Limiting                                                                  |
 +------------------------------------------------------------------------------------------------+
                                  ^
                                  |
====================================================================================================
                                  ONLINE QUERY ORCHESTRATION TIER
====================================================================================================

 React + TypeScript Client
         |
         | POST /api/v1/query { question, as_of, regulator, domain }
         v
 +------------------------------------------------------------------------------------------------+
 | FastAPI Query Controller                                                                       |
 | - Request Validation (Pydantic QueryRequest) & Rate Limit Check                                |
 +------------------------------------------------------------------------------------------------+
         |
         v
 +------------------------------------------------------------------------------------------------+
 | Hybrid Retrieval Engine (BM25 Lexical + Dense Vector + RRF)                                    |
 | - Pilot: in-memory BAAI/bge-small-en-v1.5 embeddings & NumPy cosine similarity                 |
 | - Target (Phase 8A): pgvector cosine similarity search                                         |
 | - BM25 lexical keyword search (exact circular IDs, sections, terms)                           |
 | - Reciprocal Rank Fusion (k=60) combining ranked lists                                         |
 +------------------------------------------------------------------------------------------------+
         |
         v
 +------------------------------------------------------------------------------------------------+
 | Temporal Status Resolver (`status_as_of`)                                                      |
 | - Directed Graph Traversal (Newer -> RELATIONSHIP -> Older)                                    |
 | - Multi-hop supersession resolution preserving unaffected provisions                           |
 | - Status classification: in_force | amended | partially_superseded | superseded | unknown     |
 +------------------------------------------------------------------------------------------------+
         |
         v
 +------------------------------------------------------------------------------------------------+
 | Provision-Level Relevance Filter & Prioritizer                                                 |
 | - Domain guardrails (e.g. promoting operative clarifications for Qualified REs)                |
 | - Stale provision suppression based on resolved temporal status                                |
 +------------------------------------------------------------------------------------------------+
         |
         v
 +------------------------------------------------------------------------------------------------+
 | Grounding & Answer Safety Gate (`AnswerSafetyGate`)                                            |
 | - Mechanical citation ID verification against retrieved chunks                                 |
 | - Verbatim substring quotation checks (prevents extractive hallucination)                      |
 | - Evidence sufficiency check & domain refusal rubric evaluation                                |
 +------------------------------------------------------------------------------------------------+
         |
         v
 +------------------------------------------------------------------------------------------------+
 | Generation & Fallback Layer (Phase 8E)                                                         |
 | - Evidence-constrained Hosted LLM Adapter (Gemini / Groq / Ollama / Bedrock)                   |
 | - Fail-Closed Extractive Fallback (returns verified excerpts if synthesis fails)               |
 +------------------------------------------------------------------------------------------------+
         |
         v
 Return QueryResponse { answer, citations, status_notes, confidence, latency_ms }
```

---

## 2. Component Breakdown & Implementation Status

| Component | Architecture Role | Implementation Status | Implementation Notes |
| :--- | :--- | :--- | :--- |
| **API Entry Point** | FastAPI server handling query and administrative routes | `IMPLEMENTED AND TESTED` | `backend/app/main.py`, `backend/app/api/endpoints.py`. Implements `POST /api/v1/query`, `GET /health`, `GET /ready` (149 passing tests). |
| **Hybrid Retriever** | Dual-channel retrieval with Reciprocal Rank Fusion | `IMPLEMENTED AND TESTED` | `backend/app/retrieval/service.py`, `rrf.py`. In-memory BM25 + BAAI/bge-small-en-v1.5 NumPy cosine similarity with RRF ($k=60$) and pre-fusion metadata filtering across active snapshots. |
| **Temporal Graph** | Directed NetworkX graph evaluating circular currency | `IMPLEMENTED AND TESTED` | `backend/app/grounding/resolver.py`, `backend/app/schemas/graph.py`. Strict `Newer -> RELATIONSHIP -> Older` directionality (9 verified edges). |
| **Relevance Filter** | Provision-specific prioritization and demotion guardrails | `IMPLEMENTED AND TESTED` | `backend/app/grounding/relevance.py`. Scoped clarification prioritization for compliance queries. |
| **Grounding Gate** | Verbatim provenance validation and refusal generator | `IMPLEMENTED AND TESTED` | `backend/app/grounding/safety_gate.py`. Fail-closed validation against retrieved chunk substrings. |
| **CSCRF Baseline** | 5-circular pilot corpus with verified audit ledger | `IMPLEMENTED AND TESTED` | 168 chunks in `data/processed/`, 9 verified edges in `data/processed/cscrf_graph.json`. |
| **React Frontend** | Client UI for query exploration and citation inspection | `IMPLEMENTED AND TESTED` | `frontend/src/`. Interactive question form, as-of date picker, citation cards, and refusal panels. |
| **PostgreSQL Tier** | Production relational and vector persistence | `IMPLEMENTED AND TESTED` | Phase 8A: Dual-engine architecture supporting default file mode and production PostgreSQL + pgvector adapter with exact `<=>` cosine search, 8 normalized tables, Alembic migrations, and idempotent corpus importer (203 total passing tests). |
| **Dynamic Ingestion** | Snapshot publication and asynchronous worker | `IMPLEMENTED AND TESTED` | Phase 8B: Admin endpoints and worker pipeline for registering new circulars and publishing reproducible search index snapshots (`IndexPublisher`). |
| **Multi-Domain Index**| Cross-domain retrieval with pre-fusion metadata filters | `IMPLEMENTED AND TESTED` | Phase 8C: Generalized retrieval across published SEBI domains (e.g. CSCRF, LODR) with pre-fusion filtering, version-aware searchability, and provenance propagation. |
| **Dynamic Graph Mgr** | Graph persistence, candidate extraction, HITL audit | `PLANNED` | Phase 8D: Proposed administrative UI and database-backed edge ledger for relationship curation. |
| **Hosted LLM Adapter**| Provider-agnostic generation with fail-closed fallback| `PLANNED` | Phase 8E: Proposed hosted LLM generation strictly bounded by retrieved context, with extractive fallback. |
| **Deployment Stack** | Docker Compose, Redis task queue, and cloud hosting | `PLANNED` | Phase 8F: Proposed containerization, rate limiting, and object storage integration for hosted SEBI platform. |
| **Multi-Regulator Pilot** | Pluggable connector pilot for secondary regulator (RBI) | `PLANNED` | Phase 8G: Proposed future expansion strictly following hosted SEBI deployment validation. |

---

## 3. Data Flow & Request Lifecycle (`POST /api/v1/query`)

1. **Request Intake & Schema Validation:**
   - The client posts a query with optional parameters:
     ```json
     {
       "question": "What are the ISO 27001 requirements for Qualified REs under CSCRF?",
       "as_of": "2026-01-01",
       "regulator": "SEBI",
       "domain": "CSCRF"
     }
     ```
   - FastAPI validates string length, date syntax, and domain tokens via `QueryRequest`.
2. **Hybrid Candidate Retrieval:**
   - The query is tokenized for lexical search (BM25) and embedded via `BAAI/bge-small-en-v1.5` for dense search (using NumPy cosine similarity in current pilot; pgvector in target architecture).
   - Top-$k$ candidates from both channels are fused using Reciprocal Rank Fusion ($k=60$):
     $$\text{RRF Score}(d) = \sum_{m \in \{\text{BM25}, \text{Dense}\}} \frac{1}{60 + \text{rank}_m(d)}$$
3. **Temporal Status Resolution:**
   - The graph resolver examines each candidate's `document_id` and provisions against the verified edge graph.
   - Traversal identifies modifying edges issued prior to or on the `as_of` date.
   - Status notes are attached: `in_force`, `amended`, `partially_superseded`, or `superseded`.
4. **Provision Relevance Filtering & Ordering:**
   - Domain-specific guardrails in `relevance.py` evaluate whether an operative clarification (e.g., Circular 2025/119 for Qualified REs) supersedes or clarifies a base clause (Circular 2024/113).
   - Relevant amending passages are prioritized while preserving historical base context.
5. **Grounding Verification & Refusal Evaluation:**
   - The candidate set is evaluated against the Refusal Rubric:
     - If no relevant candidates meet similarity thresholds $\rightarrow$ `refuse: true` (`insufficient_evidence`).
     - If conflicting unresolvable provisions exist $\rightarrow$ `refuse: true` (`conflicting_evidence`).
     - If outside the indexed corpus $\rightarrow$ `refuse: true` (`out_of_domain`).
6. **Synthesis / Fallback Generation:**
   - In the current pilot (`IMPLEMENTED`), the `AnswerSafetyGate` generates an extractive grounded answer from the top-ranked verified passage.
   - In Phase 8E (`PLANNED`), a hosted LLM synthesizes an evidence-bounded answer. The synthesis is mechanically checked for verbatim chunk support; if verification fails, the system seamlessly falls back to extractive excerpts.
7. **Response Packaging:**
   - The API constructs a `QueryResponse` including the answer, verified citations, status notes, confidence score, and latency metrics.

---

## 4. Key Architectural Safeguards

1. **Provenance Invariance:** Every chunk retains immutable provenance: `source_url`, `pdf_path`, `sha256`, 1-indexed PDF page, and printed page number.
2. **Directed Graph Invariant:** Graph edges are strictly directed `Newer -> RELATIONSHIP -> Older`. Self-loops (`source == target`) are blocked at the schema level.
3. **Provision-Level Scoping:** Supersession edges define `affected_provisions`. An amendment to Clause 6 never invalidates Clauses 1–5 of the parent document.
4. **No Date-Based Legal Currency:** Circular currency is never inferred solely from publication chronology. Only verified relationship edges modify document legal standing.
5. **Server-Side Secret Isolation:** All API credentials, database strings, and provider keys reside strictly on the server in `.env` variables and are never shipped to the frontend.

---

## 5. Search Index Publication & Synchronization Lifecycle (Phase 8B.6 Implemented)

To guarantee that newly ingested regulatory documents are not reported as searchable until fully validated, search index publication operates as a decoupled, atomic snapshotting system:

1. **Candidate Staging:**
   - The candidate index artifacts (`chunks.json`, `bm25_index.json`, `dense_vectors.npy`, and `manifest.json`) are compiled inside an isolated staging directory: `snapshots/_staging_<id>/`.
2. **Strict Validation Gates:**
   - Dimensionality check (384-dim normalized float32 vectors).
   - Finite numeric validation (zero NaN / ±Infinity values allowed).
   - Chunk ID uniqueness and bijective mapping between chunks and vector matrix rows.
   - SHA-256 corpus fingerprinting and artifact checksum verification.
3. **Atomic Activation:**
   - Upon passing all gates, the staging directory is finalized to `snapshots/<snapshot_id>/`.
   - The active pointer `active_snapshot.json` is atomically updated via a temporary file replacement pattern (`active_snapshot.json.tmp` -> replace `active_snapshot.json`).
   - Legacy fallback `dense_index.npy` is updated to guarantee backwards compatibility with older evaluation runners.
4. **Zero-Downtime Query Engine Hot-Reload:**
   - `RetrievalService.reload_if_stale()` verifies whether the active pointer timestamp has advanced.
   - Online query requests are never interrupted: existing requests conclude against the prior snapshot in memory, while subsequent requests bind to the newly activated snapshot.
5. **Searchability Contract:**
   - Admin endpoints (`/api/v1/admin/jobs/{job_id}/status`) report `searchable: false` and `publication_status: "not_published"` or `"pending_publication"` until the document ID is verified inside the active snapshot's manifest.
