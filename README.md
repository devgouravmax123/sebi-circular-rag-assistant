# SEBI Regulatory Intelligence Platform

A hosted, extensible regulatory intelligence platform for discovering, ingesting, searching, understanding, and tracking official regulatory documents with provenance-grounded question answering and supersession-aware temporal resolution.

---

## 1. Product Vision & Architecture Direction

This repository contains the **SEBI Regulatory Intelligence Platform**, designed to evolve from an initial pilot into a comprehensive, multi-domain, hosted regulatory intelligence platform.

### Canonical Product Definition
- **Platform Scope:** Discovery, ingestion, indexing, retrieval, relationship tracking, and evidence-constrained analysis of official regulatory documents.
- **Initial Pilot & Regression Baseline:** The Cybersecurity and Cyber Resilience Framework (CSCRF) 5-circular corpus (`sebi_cscrf_2024_113`, `sebi_cscrf_2025_045`, `sebi_cscrf_2025_060`, `sebi_cscrf_2025_096`, `sebi_cscrf_2025_119`). This baseline serves as a permanent regression test suite, **not** a corpus boundary.
- **Immediate Domain Expansion:** Extension to broader SEBI regulatory domains (LODR, ICDR, Mutual Funds, Intermediaries, Portfolio Managers) via dynamic document connectors and relational metadata registries.
- **Hosted Operational Validation First:** Full hosted deployment, operations, and evaluation will be established and validated on SEBI regulations before any secondary regulator is introduced.
- **Future Multi-Regulator Extensibility:** Architectural abstraction supporting pluggable official regulatory connectors (e.g., RBI, IRDAI, IFSCA, PFRDA) sharing unified schemas for temporal status resolution, provision-level relationship graphs, and evidence-grounded generation.

---

## 2. Implementation Status Summary

To ensure transparent engineering tracking, all core platform capabilities are explicitly classified according to their current repository state:

| Capability / Subsystem | Status | Details |
| :--- | :--- | :--- |
| **CSCRF 5-Circular Pilot Corpus & Ground Truth** | `IMPLEMENTED AND TESTED` | 5 raw PDFs parsed into 168 structured chunks across 5 document directories; 9 verified graph edges with dual-page citations in `cscrf_graph.json`. |
| **Deterministic Extraction & Chunking** | `IMPLEMENTED AND TESTED` | PyMuPDF-based text and boundary extraction with SHA-256 provenance tracking in `backend/app/ingestion/`. |
| **Hybrid Retrieval Engine (BM25 + NumPy Cosine + RRF)** | `IMPLEMENTED AND TESTED` | Reciprocal Rank Fusion of BM25 lexical rank and in-memory dense vector cosine similarity via `BAAI/bge-small-en-v1.5` embeddings (`backend/app/retrieval/`). |
| **Provision-Level Relevance Filtering & Ordering** | `IMPLEMENTED AND TESTED` | Domain guardrails in `relevance.py` ensuring precision for targeted compliance queries (e.g., ISO 27001 Qualified RE clarification prioritization). |
| **Temporal Status Engine (`status_as_of`)** | `IMPLEMENTED AND TESTED` | Multi-hop directed graph traversal (`Newer -> RELATIONSHIP -> Older`) resolving `in_force`, `amended`, `partially_superseded`, `superseded`, and `unknown` as of query date. |
| **Grounding & Fail-Closed Safety Gate** | `IMPLEMENTED AND TESTED` | `AnswerSafetyGate` validating mechanical chunk provenance and exact verbatim quotation substrings; abstains when evidence is missing or superseded. |
| **FastAPI REST API & Pydantic Contracts** | `IMPLEMENTED AND TESTED` | `POST /api/v1/query`, `GET /health`, `GET /ready` with full validation (149 passing backend unit & integration tests). |
| **Interactive React + TypeScript Frontend** | `IMPLEMENTED AND TESTED` | Responsive UI with query inputs, `as_of` date controls, expandable citation inspector, and confidence/status cards. |
| **Automated Retrieval Regression Suite** | `IMPLEMENTED AND TESTED` | 32 verified gold queries (28 answerable ranking queries, 4 unanswerable refusal queries) in `evaluation/evaluate_retrieval.py`. |
| **PostgreSQL + pgvector Persistence Foundation (Phase 8A)** | `IMPLEMENTED AND TESTED` | Complete persistence tier: SQLAlchemy 2.x models, Alembic migrations (`0001_initial_persistence_foundation`), file adapter, PostgreSQL/pgvector adapter, idempotent corpus importer CLI, dual-engine parity suite, and live PostgreSQL integration tests (203 total passing tests). |
| **Dynamic Ingestion Service & Admin API (Phase 8B)** | `PLANNED` | Proposed document registration, automated chunking, vector indexing, and pipeline queueing without code changes. |
| **Generalized Multi-Domain Retrieval (Phase 8C)** | `PLANNED` | Proposed hierarchical cross-domain routing, category filtering, and provision-scoped index partition across SEBI domains. |
| **Dynamic Regulatory Graph Manager (Phase 8D)** | `PLANNED` | Proposed relationship extraction proposals with human-in-the-loop review interface and audit ledger. |
| **Evidence-Constrained Hosted LLM Synthesis (Phase 8E)** | `PLANNED` | Proposed provider-agnostic synthesis (Gemini/Groq/Bedrock) backed by citation validation and strict fallback to verbatim extracted excerpts. |
| **Hosted SEBI Deployment & Operational Hardening (Phase 8F)** | `PLANNED` | Proposed containerized deployment (Docker/Compose), Redis task queue, object storage, and expanded multi-domain SEBI evaluation. |
| **Secondary Regulator Pilot (e.g., RBI / IRDAI)** | `PLANNED` | Proposed future extensibility milestone, strictly following operational validation of hosted SEBI platform. |
| **Automated Legal Interpretation / Adjudication** | `DEFERRED` | Out of scope. System presents verified evidence, never legal advice or speculative rulings. |

---

## 3. Core Architecture & Engineering Principles

```text
                                  +-------------------------------------------------------------+
                                  |                 INGESTION & REGISTRY ENGINE                |
                                  |                                                             |
   Official Sources               |   +---------------------+     +-------------------------+   |
   (SEBI, future regulators) ---> |   | Pluggable Connector | --> | Text & Table Extraction |   |
                                  |   +---------------------+     +-------------------------+   |
                                  |                                            |                |
                                  |                                            v                |
                                  |                               +-------------------------+   |
                                  |                               | Chunking & Provenance   |   |
                                  |                               +-------------------------+   |
                                  +--------------------------------------------|----------------+
                                                                               |
                                                                               v
                                  +-------------------------------------------------------------+
                                  |                   DUAL PERSISTENCE TIER                     |
                                  |                                                             |
                                  |   File-Backed Mode (Default)  PostgreSQL + pgvector         |
                                  |   - data/processed/ JSON      - Normalized Relational Tables|
                                  |   - dense_index.npy (NumPy)   - 384-dim pgvector (<=>)      |
                                  |   - cscrf_graph.json          - Immutable Version History   |
                                  +-------------------------------------------------------------+
                                                                               ^
                                                                               |
                                  +--------------------------------------------|----------------+
                                  |                 QUERY ORCHESTRATION PIPELINE                |
                                  |                                                             |
   React UI / Client              |   +-----------------------------------------------------+   |
   POST /api/v1/query ----------> |   | FastAPI Query Controller & Request Validator        |   |
                                  |   +-----------------------------------------------------+   |
                                  |                              |                              |
                                  |                              v                              |
                                  |   +-----------------------------------------------------+   |
                                  |   | Hybrid Retrieval (BGE-Small / BM25 Lexical + RRF)   |   |
                                  |   +-----------------------------------------------------+   |
                                  |                              |                              |
                                  |                              v                              |
                                  |   +-----------------------------------------------------+   |
                                  |   | Temporal Graph Resolver (as_of Status Evaluation)   |   |
                                  |   +-----------------------------------------------------+   |
                                  |                              |                              |
                                  |                              v                              |
                                  |   +-----------------------------------------------------+   |
                                  |   | Provision Relevance Filter & Safeguard Prioritizer  |   |
                                  |   +-----------------------------------------------------+   |
                                  |                              |                              |
                                  |                              v                              |
                                  |   +-----------------------------------------------------+   |
                                  |   | Grounding & Safety Gate (Citation / Claim Check)    |   |
                                  |   +-----------------------------------------------------+   |
                                  |                              |                              |
                                  |                              v                              |
                                  |   +-----------------------------------------------------+   |
                                  |   | Generation / Verbatim Substring Fallback            |   |
                                  |   +-----------------------------------------------------+   |
                                  +-------------------------------------------------------------+
```

### Mandatory Engineering Safeguards
1. **Official-Source Provenance:** Every chunk links to official PDF source URLs, cryptographic checksums (SHA-256), and page numbers (both 1-indexed PDF page and printed page).
2. **Deterministic Ingestion:** Chunking and metadata enrichment are reproducible from raw files without manual intervention.
3. **No Temporal Inference from Dates Alone:** Publication dates never establish legal currency or supersession. Only explicit, source-verified regulatory relationship edges modify document or provision status.
4. **Provision-Level Scoping:** Supersession is scoped to specific sections/clauses (`affected_provisions`) to prevent partial amendments from falsely invalidating base frameworks.
5. **Fail-Closed Grounding & Verified Quotation:** Claims and excerpts must be verified against server-side retrieval sets and exact verbatim chunk substrings. The system does not claim semantic proof of arbitrary synthetic text or zero hallucinations; it abstains when evidence is missing or unverified.
6. **Zero Silent Cloud Spend:** No paid APIs, cloud resources, or billable infrastructure are enabled without explicit architectural sign-off.

---

## 4. Canonical Roadmap

The project is structured into sequential milestones:

- **Phases 0–7E (Foundational Pilot):** `COMPLETED & TESTED`
  - Environment, minimal slice, CSCRF data models, PDF extraction, hybrid retrieval (BM25 + BGE-Small dense index), NetworkX supersession graph, grounding safety gate, FastAPI `/api/v1/query` endpoint, React UI, and test-driven relevance improvements.
- **Phase 8A — Persistence Foundation (PostgreSQL + pgvector):** `COMPLETED & TESTED`
  - Dual-engine storage repository architecture:
    - Default zero-dependency file-backed repository adapter reading `data/processed/` and `data/indexes/`.
    - Production-grade PostgreSQL + pgvector adapter utilizing exact `<=>` cosine distance vector indexing (`BAAI/bge-small-en-v1.5`, 384 dimensions).
    - SQLAlchemy 2.x declarative models for 8 relational entities: `regulators`, `regulatory_domains`, `regulatory_documents`, `document_versions`, `provisions`, `chunks`, `chunk_embeddings`, `regulatory_edges`, and `ingestion_jobs`.
    - Alembic migration environment (`0001_initial_persistence_foundation`) hardened with automatic `alembic_version` column width capacity management.
    - Idempotent corpus importer CLI with `--dry-run`, `--all-or-nothing`, and verified relationship edge protection.
    - Full test suite verified: 203 passing tests (200 offline unit and parity tests, 3 live PostgreSQL/pgvector integration tests).
- **Phase 8B — Dynamic Document Ingestion:** `NEXT MILESTONE`
  - Proposed dynamic document registration service, idempotent background ingestion, checksum tracking, and PDF parser abstractions.
- **Phase 8C — Generalized Multi-Domain Retrieval:** `PLANNED`
  - Proposed corpus partitioning, domain filters, hierarchical BM25 + dense search, and cross-domain reciprocal rank fusion across SEBI regulations.
- **Phase 8D — Dynamic Regulatory Relationship Management:** `PLANNED`
  - Proposed database-backed graph store, relationship proposition parser, human-in-the-loop review interface, and edge evidence provenance ledger.
- **Phase 8E — Evidence-Constrained Hosted LLM Synthesis:** `PLANNED`
  - Proposed backend-only hosted LLM adapter (Gemini / Groq / Ollama / Bedrock) with claim extraction, token-level citation alignment, and fail-closed extractive fallback.
- **Phase 8F — Hosted SEBI Deployment & Operational Hardening:** `PLANNED`
  - Proposed containerized deployment (Docker Compose), Redis caching/queueing, S3-compatible document storage, expanded golden test suite (100+ multi-domain queries), and operational hardening.
- **Phase 8G — Carefully Scoped Regulatory Expansion (Future Pilot):** `PLANNED`
  - Proposed secondary regulator connector pilot (e.g., RBI / IRDAI) following successful operational validation of hosted SEBI platform.

---

## 5. Persistence Operational Runbook & Local Setup

The platform features a **Dual-Engine Persistence Architecture**:
- **File-Backed Mode (Default):** Activated when `DATABASE_URL` is unset or blank. Operates entirely without external database dependencies, reading processed JSON documents and NumPy index matrices in `data/processed/` and `data/indexes/`.
- **PostgreSQL + pgvector Mode:** Activated when `DATABASE_URL` is set to a valid PostgreSQL connection string (e.g., `postgresql+psycopg://user:pass@host:5432/dbname`). Utilizes native relational tables, immutable document versioning, and pgvector cosine distance operations (`<=>`).

### 5.1. Safe Local PostgreSQL + pgvector Test Setup
For local testing or integration verification, launch a disposable PostgreSQL 16 container with the `pgvector` extension:

```powershell
# 1. Run disposable local pgvector container
docker run -d --name sebi-pgvector-test -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=sebi_rag_test -p 5432:5432 pgvector/pgvector:pg16

# 2. Enable pgvector extension inside the test database
docker exec -i sebi-pgvector-test psql -U postgres -d sebi_rag_test -c "CREATE EXTENSION IF NOT EXISTS vector;"

# 3. Configure connection environment variable
$env:DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/sebi_rag_test"
```

> [!CAUTION]
> **Safety Guardrail:** Automated tests that perform schema resets (`drop_all`) strictly require that the database name contains `test` (e.g. `sebi_rag_test`) or that `ALLOW_TEST_DATABASE_PURGE=1` is explicitly set. They will abort with an error on any other database.

### 5.2. Running Database Migrations
Migrations are managed via Alembic:

```powershell
# Run migrations to head
.\.venv\Scripts\python.exe -m alembic upgrade head

# Inspect current revision status
.\.venv\Scripts\python.exe -m alembic current

# Rollback one revision (if testing down-migrations)
.\.venv\Scripts\python.exe -m alembic downgrade -1

# Re-upgrade to head
.\.venv\Scripts\python.exe -m alembic upgrade head
```

*Note on Revision Identifiers:* `backend/alembic/env.py` includes `_ensure_version_table_capacity` which automatically provisions the `alembic_version` table with `VARCHAR(64)`. This guarantees descriptive migration identifiers like `0001_initial_persistence_foundation` execute without PostgreSQL `StringDataRightTruncation` errors.

### 5.3. Corpus Importer CLI Usage
Transfer the verified 5-document CSCRF pilot corpus into PostgreSQL using the idempotent CLI importer:

```powershell
# Dry run: validate pilot corpus documents, chunks, vectors, and edges without writing
.\.venv\Scripts\python.exe backend/app/persistence/cli_importer.py --dry-run

# Full import into PostgreSQL with transaction safety
.\.venv\Scripts\python.exe backend/app/persistence/cli_importer.py --database-url "$env:DATABASE_URL" --all-or-nothing

# JSON output for automated scripting/pipelines
.\.venv\Scripts\python.exe backend/app/persistence/cli_importer.py --json-output
```

**Importer Semantics:**
- **Idempotency:** Re-running the importer against an already populated database performs zero duplicate writes.
- **Conflict Handling:** If a document's SHA-256 checksum has changed, a new immutable version record (e.g., `v2`) is created; existing historical versions and chunk associations are never overwritten in-place.
- **Edge Protection:** Existing verified graph edges (`review_status = 'verified'`) are never downgraded or overwritten by re-imports.

### 5.4. Running the Test Suites

```powershell
# 1. Run offline unit, parity, and grounding test suite (200 tests)
.\.venv\Scripts\python.exe -m pytest -q

# 2. Run dual-engine persistence parity tests
.\.venv\Scripts\python.exe -m pytest backend/tests/test_persistence_parity.py -v

# 3. Run live PostgreSQL + pgvector integration tests (requires live test container)
$env:DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/sebi_rag_test"
.\.venv\Scripts\python.exe -m pytest backend/tests/test_postgres_live_integration.py -v

# 4. Run entire full test suite with live database enabled (203 passing tests)
.\.venv\Scripts\python.exe -m pytest -v
```

---

## 6. Local Setup & Verification (Application)

### Prerequisites
- Python 3.11+
- Node.js 18+ and npm
- Windows PowerShell / Linux bash

### Backend Setup
```powershell
# Create and activate virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r backend/requirements.txt

# Start FastAPI development server (File-backed default mode)
$env:PYTHONPATH="backend;."
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

### Frontend Setup
```powershell
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 5173
```

---

## 6. Disclaimer

This platform is an independent regulatory technology project. It is **not** affiliated with, endorsed by, or operated by the Securities and Exchange Board of India (SEBI) or any other official regulatory body. Information and evidence returned by this system are for research, compliance assistance, and educational purposes and do not constitute formal legal advice.
