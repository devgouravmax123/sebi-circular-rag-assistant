# System Architecture

## 1. High-Level Architecture Overview

The platform uses a modular, layered architecture separating offline document ingestion, atomic search index publication, hybrid retrieval, and evidence-grounded answer synthesis.

```text
====================================================================================================
                                      OFFLINE / INGESTION TIER
====================================================================================================

 Official Source PDFs (data/raw/*.pdf)
          |
          v
 +------------------------------------------------------------------------------------------------+
 | Ingestion Pipeline (extractor.py & chunker.py)                                                 |
 | - PyMuPDF Text Extraction & Extraction Quality Auditing (OK, SPARSE, EMPTY)                    |
 | - SHA-256 Checksum Verification & Deduplication                                                |
 | - Deterministic Sliding-Window Chunking (350 words, 40 overlap)                                 |
 | - Dual Page Provenance: 1-indexed PDF page & printed document page number                      |
 | - Stable Chunk ID Generation: {document_id}_p{page_start}_{chunk_idx}                          |
 +------------------------------------------------------------------------------------------------+
          |
          v
 Processed Artifacts (data/processed/{doc_id}/[document|pages|chunks|manifest].json)
          |
          v
 +------------------------------------------------------------------------------------------------+
 | Index Publication Pipeline (IndexPipeline & SnapshotPublicationService)                       |
 | - BM25 Inverted Token Matrix Builder (bm25_index.pkl)                                          |
 | - BAAI/bge-small-en-v1.5 Dense Vector Embeddings (dense_index.npy / dense_index.faiss)          |
 | - Chunk Metadata Catalog (chunk_catalog.json)                                                  |
 | - Atomic Snapshot Swap to data/indexes/snapshots/{snapshot_id}/                                |
 +------------------------------------------------------------------------------------------------+
          |
          v
====================================================================================================
                               ONLINE QUERY ORCHESTRATION TIER
====================================================================================================

 React + TypeScript Client
          |
          | POST /api/v1/query { query, top_k, include_superseded }
          v
 +------------------------------------------------------------------------------------------------+
 | FastAPI Query Controller (api/v1/query.py)                                                     |
 | - Pydantic Request Validation & Rate Limiter Check (InMemoryRateLimiter)                       |
 +------------------------------------------------------------------------------------------------+
          |
          v
 +------------------------------------------------------------------------------------------------+
 | Hybrid Retrieval Service (retrieval/service.py)                                                |
 | - Atomic Hot-Reload Check against active_snapshot.json (reload_if_stale)                       |
 | - Lexical Channel: BM25Okapi scoring                                                           |
 | - Dense Channel: BAAI/bge-small-en-v1.5 normalized cosine similarity                           |
 | - Reciprocal Rank Fusion (k=60) combining candidate pools into top_k candidates               |
 +------------------------------------------------------------------------------------------------+
          |
          v
 +------------------------------------------------------------------------------------------------+
 | Grounding & Relevance Engine (grounding/relevance.py)                                          |
 | - Specificity-Aware Reranker: prioritizes specific query token coverage                        |
 | - Candidate Relevance Gate: filters out topical drift and generic boilerplate                  |
 +------------------------------------------------------------------------------------------------+
          |
          v
 +------------------------------------------------------------------------------------------------+
 | Temporal & Relationship Resolver (grounding/graph_resolver.py)                                 |
 | - Traverses directed relationship edges: Newer -> AMENDS/RESCINDS -> Older                     |
 | - Resolves legal status as of requested as_of date                                             |
 | - Provision-Scoped Validity: unaffected clauses in target circulars remain valid               |
 +------------------------------------------------------------------------------------------------+
          |
          v
 +------------------------------------------------------------------------------------------------+
 | Answer Safety Gate (grounding/safety_gate.py)                                                  |
 | - Evaluates Refusal Rubric: insufficient evidence, conflict, out-of-domain, temporal ambiguity  |
 | - Verbatim Substring Matching & Extractive Fallback                                            |
 +------------------------------------------------------------------------------------------------+
          |
          v
 +------------------------------------------------------------------------------------------------+
 | Synthesis Layer (synthesis/service.py)                                                         |
 | - External LLM Provider Adapter (Groq / Gemini / Mock)                                         |
 | - Citation Validator (validator.py): mechanically verifies cited chunks in retrieved context   |
 | - Fail-Closed Fallback: reverts to verified excerpts if synthesis verification fails          |
 +------------------------------------------------------------------------------------------------+
          |
          v
 Return QueryResponse (verified answer, claim citations, dual-page excerpts, disposition)
```

---

## 2. Component Breakdown & Verification Status

| Subsystem / Component | Architectural Role | Current Status | Verification Notes |
| :--- | :--- | :--- | :--- |
| **API Entry Point** | FastAPI server (`main.py`, `query.py`, `admin.py`) | `IMPLEMENTED AND VERIFIED` | Provides `/health`, `/ready`, `/api/v1/query`, and `/api/v1/admin/indexes/publish`. Tested across 445 backend tests. |
| **Hybrid Retriever** | Dual-channel retrieval with Reciprocal Rank Fusion | `IMPLEMENTED AND VERIFIED` | `retrieval/service.py` & `rrf.py`. BM25Okapi + `BAAI/bge-small-en-v1.5` embeddings with RRF ($k=60$). |
| **Atomic Index Publisher** | Snapshot builder and atomic symlink/pointer swapper | `IMPLEMENTED AND VERIFIED` | `retrieval/index_pipeline.py`. Builds staging snapshot and updates `active_snapshot.json` atomically. |
| **Relevance & Specificity Reranker** | Query token coverage reranking and candidate filtering | `IMPLEMENTED AND VERIFIED` | `grounding/relevance.py`. Specificity reranker prioritizes specific query tokens; preserves RRF tie-breaker. |
| **Temporal Graph Resolver** | Directed graph traversal and supersession engine | `IMPLEMENTED AND VERIFIED (CSCRF)` | `grounding/graph_resolver.py`. Evaluates `AMENDS`, `RESCINDS`, and `REFERENCES` edges for SEBI CSCRF. |
| **Answer Safety Gate** | Mechanical provenance checks and 5-point refusal rubric | `IMPLEMENTED AND VERIFIED` | `grounding/safety_gate.py` & `validator.py`. Enforces fail-closed citation verification and extractive fallback. |
| **Grounded LLM Adapter** | Evidence-constrained generation with fallback | `PARTIALLY IMPLEMENTED` | `synthesis/groq_provider.py` and `gemini_provider.py` implemented. Falls back closed to extractive excerpts. |
| **In-Memory Rate Limiter** | Client IP fixed-window sliding rate limiter | `IMPLEMENTED AND VERIFIED` | `core/rate_limiter.py`. Enforces burst and per-minute query limits without external database dependencies. |
| **Relational Persistence (Postgres)** | Normalized SQL models and Alembic migrations | `IMPLEMENTED, VERIFICATION INCOMPLETE` | `persistence/models.py`. Validated in unit parity tests, but NOT active runtime default (which is file snapshot). |
| **Manifest-Driven Ingestion** | Local data-driven document ingestion | `PLANNED (Phase 1)` | Replaces static Python dictionary `corpus_registry.py` with declarative JSON manifests. |
| **Collection-Aware Isolation** | Collection scoping across retrieval and synthesis | `PLANNED (Phase 2)` | Explicit `collection_id` scoping to guarantee zero cross-collection evidence leakage. |
| **Pluggable Relationship Policies** | Modular policies (supersession vs. passthrough) | `PLANNED (Phase 3)` | Allows general document collections to operate without mandatory regulatory supersession graphs. |
| **Distributed Broker / Cache (Redis)** | Asynchronous task queue and distributed cache | `DEFERRED / REQUIRES APPROVAL` | Deferred. Current in-memory limiter and synchronous pipeline meet zero-cost requirements. |

---

## 3. Data Flow & Request Lifecycle (`POST /api/v1/query`)

1. **Request Intake & Schema Validation:**
   - Client sends `QueryRequest` (query, top_k, optional filters).
   - FastAPI validates string length (2–500 chars), date syntax, and regulator code.
2. **Rate Limit Verification:**
   - `InMemoryRateLimiter` checks IP request count against configured window (`RATE_LIMIT_QUERY_PER_MINUTE`). Exceeded limits return HTTP 429.
3. **Index Hot-Reload Check:**
   - `RetrievalService.reload_if_stale()` compares the memory snapshot pointer with `data/indexes/active_snapshot.json`. If a new snapshot was published, it reloads in memory without interrupting active requests.
4. **Hybrid Candidate Retrieval:**
   - BM25 scores the corpus lexically; `BAAI/bge-small-en-v1.5` embeds the query and computes cosine similarity against normalized dense vectors.
   - Top candidates are fused using Reciprocal Rank Fusion ($k=60$):
     $$\text{RRF Score}(d) = \sum_{m \in \{\text{BM25}, \text{Dense}\}} \frac{1}{60 + \text{rank}_m(d)}$$
5. **Relevance Filtering & Specificity-Aware Reranking:**
   - Filters out passages lacking query keyword overlap or topical relevance.
   - Candidates are reranked by specific query token coverage descending, with RRF score as tie-breaker.
6. **Temporal Status Resolution:**
   - For collections with active relationship graphs, `ConservativeGraphResolver` evaluates edges active on or before `as_of_date`.
   - Modifying edges tag passages as `in_force`, `amended`, `partially_superseded`, or `superseded`. Unaffected provisions retain base validity.
7. **Grounding & Refusal Evaluation:**
   - `AnswerSafetyGate` evaluates the Refusal Rubric: if candidates fall below threshold, conflict without resolution, or fall out-of-domain, it returns `disposition: CONTROLLED_ABSTENTION`.
8. **Answer Synthesis & Verification:**
   - If evidence is sufficient, `SynthesisService` prompts the configured provider (Groq/Gemini/Mock) with evidence chunks.
   - `CitationValidator` mechanically verifies that all claim assertions and `[chunk_id]` tags map to retrieved context.
   - If citation validation fails or the provider times out, the system fails closed to verified extractive excerpts or structured refusal.
9. **Response Serialization:**
   - The API returns `QueryResponse` with the synthesized answer, structured citations, dual-page excerpts, and narrative disposition.

---

## 4. Key Architectural Invariants & Safeguards

1. **Immutable Source Provenance:** Every chunk retains its source PDF path, SHA-256 hash, 1-indexed PDF page, and printed page string.
2. **Strict Directed Graph Invariant:** For regulatory collections, edges enforce $\text{source (newer)} \xrightarrow{\text{RELATIONSHIP}} \text{target (older)}$. Self-loops (`source == target`) are blocked.
3. **No Chronological Legal Inference:** Newer publication dates never imply supersession without an explicit, verified relationship edge.
4. **Provision-Level Scoping:** Amendments define `affected_provisions`. Amending Clause 6 never invalidates Clauses 1–5 of the base circular.
5. **Mandatory Collection Isolation (Phase 2 Invariant):** Queries scoped to `collection_id` $A$ must NEVER retrieve, evaluate, cite, or leak chunks from collection $B$. Cross-collection leakage is treated as a critical architectural failure.
6. **Zero-Cost Local Default:** The entire pipeline executes locally from files and memory snapshots at zero infrastructure cost.

---

## 5. Search Index Snapshot & Publication Lifecycle (`IMPLEMENTED AND VERIFIED`)

To prevent incomplete or unvalidated index builds from corrupting query serving, index publication operates as an atomic snapshot system (`backend/app/retrieval/index_pipeline.py`):

1. **Candidate Staging:** Index artifacts (`chunks.json`, `bm25_index.pkl`, `dense_index.npy`, `manifest.json`) are written into an isolated staging folder: `data/indexes/snapshots/_staging_<id>/`.
2. **Validation Gates:**
   - Dimensionality check: dense vectors must be 384-dimensional normalized float32.
   - Numeric validity: zero NaN, null, or infinite values permitted.
   - Bijective mapping: exact 1-to-1 correspondence between `chunks.json` entries and matrix rows.
   - SHA-256 fingerprinting: catalog checksum matches manifest.
3. **Atomic Activation:**
   - The validated staging directory is renamed to `data/indexes/snapshots/<snapshot_id>/`.
   - `active_snapshot.json.tmp` is written and atomically renamed to `data/indexes/active_snapshot.json`.
4. **Hot Reloading:**
   - `RetrievalService.reload_if_stale()` detects snapshot pointer changes on incoming queries and re-binds indexes in memory with zero application downtime.
