# API Specification

## 1. General Principles & Protocols
- **Base URL:** `/api/v1`
- **Format:** JSON (`Content-Type: application/json`)
- **Authentication:** Unauthenticated in local development/pilot. Future admin routes require Bearer token authorization.
- **Secrets:** External model API keys, database credentials, and secrets reside strictly in backend environment variables and are never accepted via client requests or returned in responses.
- **Error Format:** All HTTP 4xx and 5xx responses use structured JSON error payloads and never expose server stack traces or internal filesystem paths.

---

## 2. Implemented Endpoints (`IMPLEMENTED AND TESTED`)

### `GET /health`
Verifies that the FastAPI process is running.
- **Request:** `GET /health`
- **Response (HTTP 200):**
  ```json
  {
    "status": "healthy"
  }
  ```

### `GET /ready`
Verifies that runtime dependencies (retrieval indexes, chunk corpora, graph definitions) are fully initialized.
- **Request:** `GET /ready`
- **Response (HTTP 200):**
  ```json
  {
    "status": "ready",
    "indexed_documents": 5,
    "total_chunks": 168,
    "graph_edges": 9
  }
  ```
- **Response (HTTP 503):** Returned if indexes are missing or failed to initialize.

---

### Storage Engine Selection & Administration (`IMPLEMENTED AND TESTED`)

The persistence tier dynamically selects the repository factory based on the server environment:
- **Default (File-Backed):** If `DATABASE_URL` is omitted or empty, `FileStorageRepositoryFactory` is instantiated, serving the pilot corpus from `data/processed/`.
- **PostgreSQL + pgvector:** If `DATABASE_URL` is provided (e.g. `postgresql+psycopg://user:pass@host:5432/dbname`), `PostgresStorageRepositoryFactory` is selected, executing queries against relational tables with native pgvector `<=>` cosine distance operations.

#### Administrative CLI Corpus Importer (`backend/app/persistence/cli_importer.py`)
Provides offline, transactional ingestion of the pilot corpus into PostgreSQL:
- **Syntax:** `python backend/app/persistence/cli_importer.py [OPTIONS]`
- **Flags:**
  - `--dry-run`: Validates document records, chunks, vectors, and edges without database writes.
  - `--all-or-nothing`: Executes the import within an atomic transaction, rolling back on any failure.
  - `--database-url <URL>`: Explicit target database connection string (overrides `DATABASE_URL` env).
  - `--json-output`: Emits structured JSON summary with record counts and validation errors.

---

### `POST /api/v1/query` (`IMPLEMENTED AND TESTED - Phase 8C Generalized Multi-Domain`)
Primary query endpoint for evidence-grounded regulatory search, temporal status evaluation, and cited question answering across published SEBI circular domains (e.g. CSCRF, LODR).

#### Request Schema (`QueryRequest`)
```json
{
  "query": "What are the disclosure timelines under Regulation 30?",
  "top_k": 5,
  "include_superseded": false,
  "regulator": "SEBI",
  "domain": "LODR",
  "document_id": "sebi_lodr_2025_021",
  "start_date": "2025-01-01",
  "end_date": "2025-12-31"
}
```

| Field | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `query` | string | Yes | Natural language query regarding SEBI regulatory provisions (min 2, max 500 characters). |
| `top_k` | integer | No | Maximum candidate passages to return (1 to 10, default 5). |
| `include_superseded` | boolean | No | Whether to include historical superseded provisions (default `false`). |
| `regulator` | string | No | Regulatory authority filter (must be `"SEBI"`; other regulators produce HTTP 422). |
| `domain` | string | No | Regulatory domain filter (e.g. `"CSCRF"`, `"LODR"`, `"ICDR"`). Applied pre-fusion. |
| `document_id` | string | No | Specific document identifier (e.g. `"sebi_cscrf_2025_119"`). Applied pre-fusion. |
| `start_date` | string | No | Earliest circular `issue_date` (`YYYY-MM-DD`). |
| `end_date` | string | No | Latest circular `issue_date` (`YYYY-MM-DD`). |

#### Response Schema (`QueryResponse`)
```json
{
  "query": "What are the disclosure timelines under Regulation 30?",
  "disposition": "QUALIFIED_HISTORICAL_EXCERPTS",
  "verified_excerpts": [
    {
      "excerpt_id": "excerpt_1",
      "source_chunk_id": "sebi_lodr_2025_021_p1_001",
      "document_id": "sebi_lodr_2025_021",
      "circular_number": "SEBI/HO/CFD/CFD-PoD-1/P/CIR/2025/21",
      "title": "Amendments to SEBI (Listing Obligations and Disclosure Requirements) Regulations, 2015",
      "issue_date": "2025-02-15",
      "page_start": 1,
      "page_end": 1,
      "excerpt_text": "Regulation 30 of LODR Regulations requires listed entities to disclose material events within thirty minutes of closure of the board meeting or twenty-four hours of occurrence.",
      "regulatory_status": "UNRESOLVED",
      "status_reason": "Historical baseline provision from sebi_lodr_2025_021 without established supersession edges in pilot graph.",
      "applicable_edge_ids": [],
      "acting_document_ids": []
    }
  ],
  "historical_superseded_excerpts": [],
  "summary_narrative": "Qualified Historical Excerpts: 1 excerpt(s) verified as authentic text from SEBI LODR corpus. Provisions with UNRESOLVED status reflect historical circular content; active legal currency is not independently established beyond the pilot.",
  "technical_validation_passed": true,
  "retrieval_candidate_count": 1,
  "abstention_reason": null,
  "regulatory_warnings": [
    "Excerpt 'excerpt_1' from sebi_lodr_2025_021 has UNRESOLVED status; it represents historical baseline circular text whose active legal currency is not established beyond the pilot."
  ],
  "pilot_corpus_scope_limitation": "Educational & Pilot Assistant: Operating strictly over the 5-document SEBI CSCRF circular corpus (CIR/2024/113, CIR/2025/045, CIR/2025/060, CIR/2025/096, CIR/2025/119). Excerpts represent technical document grounding only and do not establish universal legal currency.",
  "answer_text": "Regulation 30 of the LODR Regulations mandates that listed entities disclose material events to stock exchanges within 30 minutes of board meeting closure or within 24 hours of occurrence.",
  "synthesis_status": "FULLY_ANSWERED",
  "claim_citations": [
    {
      "claim_text": "Regulation 30 of the LODR Regulations mandates that listed entities disclose material events to stock exchanges within 30 minutes of board meeting closure or within 24 hours of occurrence.",
      "supporting_chunk_ids": ["sebi_lodr_2025_021_p1_0"],
      "citations": [
        {
          "source_chunk_id": "sebi_lodr_2025_021_p1_0",
          "document_id": "sebi_lodr_2025_021",
          "circular_number": "SEBI/LAD-NRO/GN/2025/021",
          "title": "Listing Obligations and Disclosure Requirements",
          "issue_date": "2025-01-10",
          "sha256": null,
          "page_start": 1,
          "page_end": 1,
          "provision_id": "Reg 30",
          "regulatory_status": "UNRESOLVED"
        }
      ]
    }
  ],
  "unresolved_subquestions": [],
  "synthesis_failure_reason": null
}
```

#### Grounded LLM Synthesis Response Examples (Phase 8E)

##### 1. Supported Answer (`FULLY_ANSWERED`)
```json
{
  "query": "Must offline isolated backups be stored in air-gapped locations?",
  "disposition": "QUALIFIED_HISTORICAL_EXCERPTS",
  "synthesis_status": "FULLY_ANSWERED",
  "answer_text": "REs shall maintain offline isolated backups in air-gapped locations to prevent tampering by ransomware.",
  "claim_citations": [
    {
      "claim_text": "REs shall maintain offline isolated backups in air-gapped locations to prevent tampering by ransomware.",
      "supporting_chunk_ids": ["sebi_cscrf_2024_113_p128_106"],
      "citations": [
        {
          "source_chunk_id": "sebi_cscrf_2024_113_p128_106",
          "document_id": "sebi_cscrf_2024_113",
          "circular_number": "SEBI/HO/ITD/ITD-PoD-1/P/CIR/2024/113",
          "title": "Cybersecurity and Cyber Resilience Framework (CSCRF)",
          "issue_date": "2024-08-20",
          "page_start": 128,
          "page_end": 128,
          "provision_id": "PR.DS.S1",
          "regulatory_status": "UNRESOLVED"
        }
      ]
    }
  ],
  "unresolved_subquestions": [],
  "synthesis_failure_reason": null
}
```

##### 2. Partially Supported Multi-Part Question (`PARTIALLY_ANSWERED`)
```json
{
  "query": "Must offline backups be air-gapped and what is the penalty in rupees for non-compliance?",
  "disposition": "QUALIFIED_HISTORICAL_EXCERPTS",
  "synthesis_status": "PARTIALLY_ANSWERED",
  "answer_text": "REs shall maintain offline isolated backups in air-gapped locations to prevent tampering by ransomware.",
  "claim_citations": [
    {
      "claim_text": "REs shall maintain offline isolated backups in air-gapped locations to prevent tampering by ransomware.",
      "supporting_chunk_ids": ["sebi_cscrf_2024_113_p128_106"],
      "citations": [
        {
          "source_chunk_id": "sebi_cscrf_2024_113_p128_106",
          "document_id": "sebi_cscrf_2024_113",
          "page_start": 128,
          "page_end": 128,
          "regulatory_status": "UNRESOLVED"
        }
      ]
    }
  ],
  "unresolved_subquestions": [
    {
      "subquestion": "What is the penalty in rupees for non-compliance?",
      "reason": "Supplied regulatory passages do not contain specific provisions addressing this secondary detail."
    }
  ],
  "synthesis_failure_reason": null
}
```

##### 3. Controlled Abstention (`ABSTAINED`)
```json
{
  "query": "What are the baseline control enhancements in NIST SP 800-53 Revision 5?",
  "disposition": "CONTROLLED_ABSTENTION",
  "synthesis_status": "ABSTAINED",
  "answer_text": "Controlled Abstention: Insufficient evidence found in the SEBI regulatory corpus to substantiate an answer for this query.",
  "claim_citations": [],
  "unresolved_subquestions": [],
  "synthesis_failure_reason": "Insufficient evidence: candidate passages in the SEBI regulatory corpus do not substantively cover the query topic."
}
```

##### 4. Provider Failure (`PROVIDER_FAILED`)
```json
{
  "query": "What are the CISO reporting requirements?",
  "disposition": "QUALIFIED_HISTORICAL_EXCERPTS",
  "synthesis_status": "PROVIDER_FAILED",
  "answer_text": "Synthesis unavailable: The answer generation provider timed out.",
  "claim_citations": [],
  "unresolved_subquestions": [],
  "synthesis_failure_reason": "Synthesis provider execution timed out."
}
```

---

## 3. Refusal Rubric & Failure Semantics

The platform strictly differentiates between **Domain Refusals (HTTP 200 with `refuse: true`)** and **Infrastructure Errors (HTTP 4xx / 5xx)**:

### Domain Refusals (`refuse: true`, HTTP 200)
Returned when the platform functions correctly but cannot provide an evidence-grounded answer:
- `insufficient_evidence`: No retrieved chunks meet the minimum similarity threshold for the question.
- `citation_verification_failed`: Synthesis produced claims or citations that cannot be mapped to retrieved chunks.
- `conflicting_evidence`: Contradictory regulatory mandates cannot be reconciled from available circulars.
- `out_of_domain`: The question is unrelated to the indexed regulatory corpus.
- `temporal_uncertainty`: Missing effective dates or ambiguous edge scopes prevent establishing the status on the requested `as_of` date.

### Infrastructure Errors (HTTP 4xx / 5xx)
- **HTTP 422 Unprocessable Entity:** Malformed input (e.g. invalid date syntax, empty question, or query > 500 chars).
- **HTTP 429 Too Many Requests:** API rate limit exceeded.
- **HTTP 502 / 504 Gateway Failure:** Upstream hosted LLM provider timeout or invalid response.
- **HTTP 503 Service Unavailable:** Required indexes or database connections are uninitialized.

---

## 4. Planned Administrative & Ingestion Endpoints (Phase 8B / 8D)

### Administrative Ingestion Endpoints (`IMPLEMENTED AND TESTED - Phase 8B.5`)

All administrative endpoints reside under `/api/v1/admin` and require the `X-Admin-Key` request header. Authentication uses constant-time token comparison (`hmac.compare_digest`). If `ADMIN_API_KEY` is not set in production environments, requests fail closed with HTTP 500.

#### `POST /api/v1/admin/documents/register`
Asynchronously queues a regulatory document URL for ingestion. Validates URL protocol, scheme, IP ranges, document ID characters, and enforces idempotency deduplication.
- **Header:** `X-Admin-Key: <ADMIN_API_KEY>`
- **Request (`RegisterDocumentRequest`):**
  ```json
  {
    "source_url": "https://www.sebi.gov.in/legal/circulars/may-2025/cscrf-guidelines.pdf",
    "regulator": "SEBI",
    "domain": "CSCRF",
    "idempotency_key": "reg-sebi-2025-05-15",
    "document_id": "sebi_cscrf_2025_05_15",
    "title": "Master Circular on Cyber Security Framework 2025",
    "circular_number": "SEBI/HO/ITD/2025/119",
    "issue_date": "2025-05-15"
  }
  ```
- **Response (HTTP 202 Accepted):**
  ```json
  {
    "job_id": "e4b2d5a1-7c98-4a5f-9231-90a1bc345678",
    "idempotency_key": "reg-sebi-2025-05-15",
    "status": "queued",
    "stage": "discovered",
    "is_duplicate_registration": false,
    "searchable": false,
    "publication_status": "not_published",
    "publication_notice": "Document has been queued for ingestion but is not yet published to the active query index.",
    "created_at": "2026-10-11T12:00:00Z"
  }
  ```

#### `GET /api/v1/admin/jobs/{job_id}/status`
Retrieves detailed lifecycle execution state, current stage, error details, retry scheduling, and explicit search publication status.
- **Header:** `X-Admin-Key: <ADMIN_API_KEY>`
- **Response (HTTP 200 OK):**
  ```json
  {
    "job_id": "e4b2d5a1-7c98-4a5f-9231-90a1bc345678",
    "idempotency_key": "reg-sebi-2025-05-15",
    "source_url": "https://www.sebi.gov.in/legal/circulars/may-2025/cscrf-guidelines.pdf",
    "document_id": "sebi_cscrf_2025_05_15",
    "regulator": "SEBI",
    "domain": "CSCRF",
    "status": "completed",
    "stage": "persisted",
    "content_sha256": "4a7d...31",
    "attempt_count": 1,
    "max_attempts": 3,
    "searchable": false,
    "publication_status": "not_published",
    "publication_notice": "Document ingestion has completed and records are stored, but search index publication has not yet been executed."
  }
  ```

#### `GET /api/v1/admin/jobs`
Lists recent ingestion jobs with bounded pagination and optional status filtering.
- **Header:** `X-Admin-Key: <ADMIN_API_KEY>`
- **Query Params:** `status` (optional: `queued`, `running`, `completed`, `failed`), `limit` (default 50, max 100), `offset` (default 0).
- **Response (HTTP 200 OK):**
  ```json
  {
    "jobs": [...],
    "total": 12,
    "limit": 50,
    "offset": 0
  }
  ```

#### `POST /api/v1/admin/jobs/{job_id}/retry`
Manually requests retry for an explicitly authorized, failed ingestion job.
- **Header:** `X-Admin-Key: <ADMIN_API_KEY>`
- **Response (HTTP 200 OK):**
  ```json
  {
    "job_id": "e4b2d5a1-7c98-4a5f-9231-90a1bc345678",
    "status": "queued",
    "stage": "discovered",
    "message": "Job successfully reset to queued for retry."
  }
  ```
- **Error (HTTP 400 Bad Request):** If job is in `queued`, `running`, or `completed` state. Only `failed` jobs may be retried.

#### `POST /api/v1/admin/indexes/publish`
Builds, strictly validates, and atomically activates a reproducible search index snapshot from authoritative persisted corpus repositories.
- **Header:** `X-Admin-Key: <ADMIN_API_KEY>`
- **Query Params:** `dry_run` (optional boolean, default `false`)
- **Response (HTTP 200 OK):**
  ```json
  {
    "success": true,
    "snapshot_id": "snapshot_20261011_160000_123456",
    "total_documents": 5,
    "total_chunks": 168,
    "corpus_fingerprint": "a1b2c3d4...",
    "manifest_path": "data/indexes/snapshots/snapshot_20261011_160000_123456/manifest.json",
    "message": "Successfully published and activated index snapshot snapshot_20261011_160000_123456."
  }
  ```

#### `GET /api/v1/admin/indexes/status`
Inspects the active snapshot pointer, publication manifest, and lists pending un-indexed documents.
- **Header:** `X-Admin-Key: <ADMIN_API_KEY>`
- **Response (HTTP 200 OK):**
  ```json
  {
    "has_active_snapshot": true,
    "active_snapshot_id": "snapshot_20261011_160000_123456",
    "active_manifest": {
      "snapshot_id": "snapshot_20261011_160000_123456",
      "created_at": "2026-10-11T16:00:00Z",
      "embedding_model": "BAAI/bge-small-en-v1.5",
      "embedding_dimension": 384,
      "total_chunks": 168,
      "total_documents": 5,
      "document_ids": ["sebi_cscrf_2024_113", "sebi_cscrf_2025_045", "sebi_cscrf_2025_060", "sebi_cscrf_2025_096", "sebi_cscrf_2025_119"]
    },
    "pending_documents": [],
    "total_pending_documents": 0
  }
  ```

### `POST /api/v1/admin/edges/propose` (`PLANNED - Phase 8D`)
Proposes and stores an automated regulatory relationship edge subject to human review.
- **Request:**
  ```json
  {
    "source_document_id": "sebi_cscrf_2025_119",
    "target_document_id": "sebi_cscrf_2024_113",
    "relationship": "AMENDS",
    "scope": "partial",
    "affected_provisions": ["Clause 6.11"],
    "evidence_text": "Paragraph 6.11 of Circular 2024/113 is hereby substituted as follows...",
    "pdf_page": 4,
    "confidence": 0.95
  }
  ```
- **Response (HTTP 201 Created):**
  ```json
  {
    "edge_id": "edge_2025_119_to_2024_113_01",
    "review_status": "pending_review"
  }
  ```
