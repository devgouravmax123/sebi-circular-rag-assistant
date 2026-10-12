# Product Requirements Document (PRD)

## 1. Executive Summary & Product Vision

The **SEBI Regulatory Intelligence Platform** is a hosted, extensible intelligence platform designed to discover, ingest, search, understand, and track official regulatory documents.

While initially validated on a high-stakes Cybersecurity and Cyber Resilience Framework (CSCRF) baseline for the Securities and Exchange Board of India (SEBI), the platform's architectural core is explicitly designed for horizontal extensibility across broader SEBI regulatory domains and future official regulators (such as RBI, IRDAI, IFSCA, and PFRDA).

The distinguishing engineering feature is **supersession-aware temporal resolution** combined with **evidence-grounded retrieval and fail-closed answer verification**. Instead of treating regulatory corpora as static text collections, the platform models circular amendment chains, provision-scoped revocations, and time-dependent legal enforceability.

---

## 2. Core Problem & Market Opportunity

Compliance officers, legal analysts, and financial institutions face severe operational friction when navigating Indian financial regulations:

1. **Circular Proliferation & Amendment Chaining:** Master frameworks are continually amended, clarified, and extended by subsequent circulars. A standard semantic RAG pipeline frequently retrieves out-of-date circulars, presenting obsolete compliance deadlines or revoked mandates as current law.
2. **Provision-Level Granularity:** Circulars rarely supersede prior frameworks in whole; rather, specific paragraphs, tables, or clauses are modified while the remainder of the base circular remains strictly in force.
3. **Temporal Ambiguity:** Compliance obligations vary depending on the historical date in question, especially during phased transition periods (glide paths).
4. **LLM Hallucination & Citation Fraud:** Off-the-shelf generative AI frequently invents circular numbers, dates, or legal mandates. In regulatory compliance, an ungrounded answer is worse than no answer.

---

## 3. Product Goals & Scope

### 3.1. Primary Goals
- **Official Source Provenance:** Every response, excerpt, and citation must trace directly to an official regulator PDF, with dual page numbers (PDF page and printed page) and cryptographic document hashes (SHA-256).
- **Temporal Status Resolution:** Accurate evaluation of document and provision currency as of any requested date (`as_of`), distinguishing `in_force`, `amended`, `partially_superseded`, `superseded`, and `unknown`.
- **Provision-Scoped Graph Modeling:** Directed graph edges (`Newer -> RELATIONSHIP -> Older`) that restrict legal modifications to exact affected clauses (`affected_provisions`).
- **Fail-Closed Grounding:** Verifiable claim attribution with automated fallback to verbatim extracted excerpts or explicit abstention (`refuse: true`) when evidence is inconclusive or contradictory.
- **Extensible Multi-Domain Architecture:** Seamless ingestion of new circular domains without code changes, supported by relational metadata registries and pluggable source connectors.

### 3.2. Scope Evolution & Staging
1. **Pilot Baseline (Regression Anchor):** The 5-document SEBI CSCRF corpus (`sebi_cscrf_2024_113`, `2025_045`, `2025_060`, `2025_096`, `2025_119`). This baseline is permanently retained as an automated regression test suite.
2. **Immediate Scope (SEBI Broadening & Operational Validation):** Controlled ingestion of major SEBI regulatory domains (LODR, ICDR, Mutual Funds, Intermediaries, Portfolio Managers) and full hosted deployment validation on SEBI regulations.
3. **Future Extensibility (Multi-Regulator Expansion):** Pluggable ingestion connectors for secondary official Indian financial regulators (Reserve Bank of India - RBI, IRDAI) following operational validation of the hosted SEBI platform.

### 3.3. Non-Goals & Responsible Disclaimers
- **Not Legal Advice:** The platform provides information discovery and regulatory intelligence; it does not replace professional legal counsel or formal compliance audits.
- **No Unbounded Coverage Promises:** The platform makes no claim of indexing 100% of historical circulars; source coverage, cutoff dates, and sync status must always be transparently declared.
- **No Autonomous Legal Interpretation:** The system extracts, synthesizes, and attributes text based on explicit evidence; it does not adjudicate legal disputes or speculate on unstated regulatory intent.
- **No Hallucinated Fallback:** The system will never fabricate an answer to avoid showing an abstention.

---

## 4. User Personas & Core Journeys

| Persona | Primary Goal | Critical Need |
| :--- | :--- | :--- |
| **Compliance Officer** | Verify current applicable mandates for their specific entity category (e.g., Qualified RE vs. MII). | Must see current amended requirements and effective deadlines, not superseded initial drafts. |
| **Legal / Policy Analyst** | Trace regulatory evolution over time across circular amendment chains. | Needs historical `as_of` date querying and clear side-by-side citations of amending clauses. |
| **Internal Auditor** | Verify statutory basis of compliance controls against official circular text. | Requires exact page-level citations, direct PDF links, and cryptographic document hashes. |
| **Platform Administrator** | Ingest new circulars and verify relationship extractions without redeploying code. | Needs dynamic ingestion APIs, automated relationship proposal parsing, and audit logs. |

---

## 5. Functional Requirements (FR)

### Ingestion & Document Management
- **FR-1 Dynamic Document Registration:** Register official documents via API/CLI specifying metadata, source URL, and PDF file.
- **FR-2 Provenance & Integrity:** Automatically compute SHA-256 hash, record retrieval timestamp, extract text/tables, and log page-level extraction quality.
- **FR-3 Deterministic Chunking:** Split document text into bounded chunks with persistent chunk IDs, page ranges, and section headers.

### Regulatory Graph & Temporal Resolution
- **FR-4 Directed Relationship Modeling:** Enforce directed edges (`source_document_id -> RELATIONSHIP -> target_document_id`) supporting `AMENDS`, `RESCINDS`, and `REFERENCES`.
- **FR-5 Provision Scoping:** Enforce `affected_provisions` for partial modifications so unaffected provisions retain their base validity.
- **FR-6 Temporal Currency Engine:** Compute `status_as_of(document_id, date, provision)` traversing multi-hop amendment chains up to the query date.

### Search, Retrieval & Relevance
- **FR-7 Hybrid Retrieval:** Combine BM25 lexical ranking and dense vector similarity using Reciprocal Rank Fusion (RRF). Current pilot utilizes BAAI/bge-small-en-v1.5 and NumPy cosine similarity; production target utilizes pgvector.
- **FR-8 Provision-Level Relevance Safeguards:** Apply domain-specific relevance filters (such as prioritizing clarified provisions over base provisions for qualified entities).
- **FR-9 Cross-Domain Partitioning:** Support filtering by regulatory domain (e.g., CSCRF, LODR) and regulator (SEBI, RBI).

### Grounding, Synthesis & Safety
- **FR-10 Evidence-Constrained Synthesis:** Hosted LLM adapter generating answers strictly grounded in retrieved chunks with citations.
- **FR-11 Citation Verification:** Mechanically verify that every citation ID and claim matches retrieved chunk text.
- **FR-12 Fail-Closed Extractive Fallback:** If LLM synthesis cannot be validated or provider quotas fail, fall back to verbatim extracted excerpts or return a structured refusal.
- **FR-13 Explicit Refusal Rubric:** Return `refuse: true` when evidence is missing, contradictory, out of domain, or superseded.

---

## 6. Implementation Status Matrix

| Subsystem / Feature | Current Implementation Status | Validation Notes |
| :--- | :--- | :--- |
| CSCRF 5-Circular Baseline Corpus | `IMPLEMENTED AND TESTED` | 5 PDFs, 168 chunks, 9 verified graph edges, 149 passing tests. |
| Deterministic PDF Parser & Chunker | `IMPLEMENTED AND TESTED` | PyMuPDF text & page extraction with SHA-256 validation. |
| In-Memory Hybrid Search (BM25 + BGE-Small + RRF) | `IMPLEMENTED AND TESTED` | Reciprocal Rank Fusion of BM25 and NumPy cosine similarity on BAAI/bge-small-en-v1.5 embeddings. |
| NetworkX Supersession Graph Store | `IMPLEMENTED AND TESTED` | Directed traversal and `status_as_of` calculation. |
| Targeted Provision Relevance Filter | `IMPLEMENTED AND TESTED` | Guardrail in `relevance.py` prioritizing operative clarifications. |
| AnswerSafetyGate & Refusal Rubric | `IMPLEMENTED AND TESTED` | Mechanical chunk presence checking, verbatim substring validation, and domain refusal handling. |
| FastAPI REST API (`/api/v1/query`, `/health`, `/ready`) | `IMPLEMENTED AND TESTED` | Fully typed Pydantic contracts and error handlers (149 passing tests). |
| React + TypeScript User Interface | `IMPLEMENTED AND TESTED` | Interactive query, date picker, expandable citation cards. |
| Retrieval Regression Suite (32 Golden Queries) | `IMPLEMENTED AND TESTED` | Measures Recall@k, MRR, and provision accuracy (28 answerable, 4 unanswerable). |
| Relational Persistence (PostgreSQL + pgvector) | `PLANNED` | Phase 8A: Document, chunk, vector, and edge tables. |
| Dynamic Ingestion Admin API & Worker Queue | `PLANNED` | Phase 8B: Ingestion service, Celery worker, checksum deduplication. |
| Generalized Multi-Domain Retrieval Engine | `PLANNED` | Phase 8C: Cross-domain query routing and index partitioning across SEBI regulations. |
| Dynamic Regulatory Relationship Manager | `PLANNED` | Phase 8D: Relational edge store, candidate extraction, HITL review. |
| Evidence-Constrained Hosted LLM Synthesis | `PLANNED` | Phase 8E: Provider-agnostic adapter with fail-closed extractive fallback. |
| Hosted SEBI Deployment & Operational Hardening | `PLANNED` | Phase 8F: Docker Compose, Redis queue/cache, S3 storage, operational validation. |
| Secondary Regulator Pilot (e.g., RBI / IRDAI) | `PLANNED` | Phase 8G: Extension milestone following operational SEBI deployment. |
| Automated Legal Interpretation / Speculation | `DEFERRED` | Out of scope. The platform will never provide automated legal rulings. |

---

## 7. Non-Functional & Security Requirements

1. **Security & Secrets:** All provider API keys and database credentials reside strictly on the backend in environment variables. No client-side exposure.
2. **Data Privacy:** Query text must not be shared with external model providers unless explicitly configured, and no personally identifiable user data is stored.
3. **Deterministic Testing:** Local test execution must not depend on network access or paid cloud services.
4. **Graceful Degradation:** Upstream infrastructure errors (LLM timeouts, network failures) must return clean HTTP error codes and must never be disguised as successful regulatory refusals.
