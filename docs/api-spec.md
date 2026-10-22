# REST API Specification

## 1. General Principles & Protocols
- **Base URL:** `/api/v1`
- **Protocol:** HTTP/1.1 or HTTP/2 over TLS, JSON payloads (`Content-Type: application/json`).
- **Authentication:** Unauthenticated for public query routes. Administrative routes require the `X-Admin-Key` header.
- **Server-Side Secret Isolation:** External model API keys, database credentials, and secrets reside strictly in backend environment variables and are never accepted via client requests or returned in responses.
- **Error Format:** All HTTP 4xx and 5xx responses use structured JSON error payloads (`{"detail": ...}`) and never expose server stack traces or internal filesystem paths.
- **Roadmap Context:** This specification documents the **current verified API contracts** alongside the **planned collection-aware contracts** scheduled for **Phase 2**.

---

## 2. Implemented Endpoints (`IMPLEMENTED AND VERIFIED`)

### `GET /health`
Liveness probe verifying that the FastAPI process is running.
- **Request:** `GET /health`
- **Response (HTTP 200 OK):**
  ```json
  {
    "status": "ok"
  }
  ```

---

### `GET /ready`
Readiness probe verifying that runtime dependencies are fully operational.
- **Request:** `GET /ready`
- **Verified Runtime Behavior (`backend/app/main.py:64-135`):**
  1. Verifies that `data/indexes/active_snapshot.json` exists and points to a valid snapshot directory.
  2. Verifies that `manifest.json`, `chunks.json`, `documents.json`, and dense vector assets (`dense_index.npy` or `embeddings.npy` unless in `lexical_only` mode) are readable on disk.
  3. Verifies that the snapshot embedding model matches the active embedding model (`BAAI/bge-small-en-v1.5`).
  4. Verifies database connectivity if `DATABASE_URL` is configured.
- **Response (HTTP 200 OK):**
  ```json
  {
    "status": "ready",
    "storage_mode": "file_backed",
    "index_active": true
  }
  ```
- **Response (HTTP 503 Service Unavailable):**
  ```json
  {
    "detail": {
      "status": "not_ready",
      "reasons": [
        "Active search index snapshot pointer missing or not published"
      ]
    }
  }
  ```
*(Note: Older documentation drafts claimed that `/ready` returned hardcoded values `indexed_documents: 5, total_chunks: 168, graph_edges: 9`. As verified in `backend/app/main.py`, the active endpoint dynamically reports health without hardcoded corpus counts).*

---

### `POST /api/v1/query` (Current Implemented Query Contract)
Primary endpoint for evidence-grounded regulatory search, temporal evaluation, and cited question answering.

#### Current Request Schema (`QueryRequest` in `backend/app/schemas/query.py`)
```json
{
  "query": "What are the ISO 27001 requirements for Qualified REs under CSCRF?",
  "top_k": 5,
  "include_superseded": false,
  "regulator": "SEBI",
  "domain": "CSCRF",
  "document_id": "sebi_cscrf_2025_119",
  "start_date": "2024-01-01",
  "end_date": "2025-12-31"
}
```

| Field | Type | Required | Default | Description & Verified Validation Rules |
| :--- | :--- | :---: | :---: | :--- |
| `query` | string | **Yes** | — | Natural language question (min 2, max 500 characters). Whitespace is stripped. |
| `top_k` | integer | No | `5` | Candidate passages to retrieve (1 to 10). |
| `include_superseded` | boolean | No | `false` | Whether to include historical superseded provisions. |
| `regulator` | string | No | `"SEBI"` | Currently validated to accept only `"SEBI"` (case-insensitive); other values return HTTP 422. |
| `domain` | string | No | `null` | Optional regulatory domain filter (e.g. `"CSCRF"`, `"LODR"`). Applied pre-fusion. |
| `document_id` | string | No | `null` | Optional document identifier filter (e.g. `"sebi_cscrf_2025_119"`). Applied pre-fusion. |
| `start_date` | string | No | `null` | Earliest circular `issue_date` (`YYYY-MM-DD`). |
| `end_date` | string | No | `null` | Latest circular `issue_date` (`YYYY-MM-DD`). |

#### Current Response Schema (`QueryResponse` in `backend/app/schemas/query.py`)
```json
{
  "query": "What are the ISO 27001 requirements for Qualified REs under CSCRF?",
  "disposition": "QUALIFIED_HISTORICAL_EXCERPTS",
  "verified_excerpts": [
    {
      "excerpt_id": "excerpt_1",
      "source_chunk_id": "sebi_cscrf_2025_119_p8_0",
      "document_id": "sebi_cscrf_2025_119",
      "circular_number": "SEBI/HO/ITD/2025/119",
      "title": "Clarifications and Technical Revisions to CSCRF",
      "issue_date": "2025-08-28",
      "page_start": 8,
      "page_end": 8,
      "excerpt_text": "Clause 6.11 Clarification: Qualified REs are encouraged and recommended (not mandatory) to obtain ISO 27001 certification.",
      "regulatory_status": "UNRESOLVED",
      "status_reason": "Operative provision from sebi_cscrf_2025_119 clarifying earlier requirements.",
      "applicable_edge_ids": ["edge_cscrf_2025_119_amends_2024_113_technical_clarifications"],
      "acting_document_ids": ["sebi_cscrf_2025_119"]
    }
  ],
  "historical_superseded_excerpts": [],
  "summary_narrative": "Qualified Historical Excerpts: 1 excerpt(s) verified as authentic text from SEBI regulatory corpus.",
  "technical_validation_passed": true,
  "retrieval_candidate_count": 1,
  "abstention_reason": null,
  "regulatory_warnings": [],
  "pilot_corpus_scope_limitation": "Educational & Pilot Assistant: Operating strictly over the 5-document SEBI CSCRF circular corpus (CIR/2024/113, CIR/2025/045, CIR/2025/060, CIR/2025/096, CIR/2025/119). Excerpts represent technical document grounding only and do not establish universal legal currency.",
  "answer_text": "Qualified REs are encouraged and recommended (not mandatory) to obtain ISO 27001 certification under Circular 2025/119 [sebi_cscrf_2025_119_p8_0].",
  "synthesis_status": "FULLY_ANSWERED",
  "claim_citations": [
    {
      "claim_text": "Qualified REs are encouraged and recommended (not mandatory) to obtain ISO 27001 certification.",
      "supporting_chunk_ids": ["sebi_cscrf_2025_119_p8_0"],
      "citations": [
        {
          "source_chunk_id": "sebi_cscrf_2025_119_p8_0",
          "document_id": "sebi_cscrf_2025_119",
          "page_start": 8,
          "page_end": 8
        }
      ]
    }
  ],
  "unresolved_subquestions": [],
  "synthesis_failure_reason": null
}
```

---

## 3. Planned Collection-Aware API Contracts (Phase 2 Design Specification)

In **Phase 2**, the query endpoint will evolve to support explicit collection selection and strict cross-collection isolation.

### Planned Request Schema (`POST /api/v1/query`)
```json
{
  "collection_id": "sebi_cscrf",
  "query": "What are the ISO 27001 requirements for Qualified REs?",
  "top_k": 5,
  "as_of_date": "2025-09-01",
  "include_superseded": false
}
```

| Field | Type | Required | Description |
| :--- | :--- | :---: | :--- |
| `collection_id` | string | **Yes** | Target collection identifier (e.g. `"sebi_cscrf"`, `"sebi_lodr"`, `"nist_sp800_53"`). Unknown collection returns HTTP 404. |
| `query` | string | **Yes** | Natural language question (2 to 500 characters). |
| `top_k` | integer | No | Maximum candidate passages (default 5, max 10). |
| `as_of_date` | string | No | Optional temporal evaluation cutoff (`YYYY-MM-DD`). |
| `include_superseded` | boolean | No | Whether to return historical superseded passages (default `false`). |

### Planned Collection Scoping & Isolation Contract
1. **Mandatory Scoping:** A query MUST specify a valid `collection_id`. If `collection_id` is missing, invalid, or empty, the API immediately fails closed with HTTP 422 or HTTP 404. The system will **never** perform an unconstrained cross-collection search.
2. **Dynamic Scope Disclaimers:** The response replaces the static `pilot_corpus_scope_limitation` string with a dynamic, collection-specific scope notice (`collection_scope_description`) derived from the active collection manifest.
3. **Strict Citation Isolation:** Chunks cited in `claim_citations` and `verified_excerpts` are verified to originate strictly from documents belonging to the requested `collection_id`.

---

## 4. Refusal Rubric & Failure Semantics (`IMPLEMENTED AND VERIFIED`)

The API strictly differentiates between **Domain Refusals (HTTP 200 with controlled disposition)** and **Infrastructure Errors (HTTP 4xx/5xx)**:

### Domain Refusals (HTTP 200 with `disposition: "CONTROLLED_ABSTENTION"`)
Returned when the platform operates normally but determines that an evidence-grounded answer cannot be safely supplied:
1. `insufficient_evidence`: Top retrieved candidates fall below the similarity threshold or do not cover the requested topic.
2. `citation_verification_failed`: Synthesized claims or citations cannot be mechanically attributed to retrieved context chunks.
3. `conflicting_evidence`: Retrieved provisions directly contradict each other without an established supersession edge.
4. `out_of_domain`: The question pertains to topics outside the indexed collection.
5. `temporal_uncertainty`: Missing effective dates or ambiguous edge scopes prevent determining legal status on the requested `as_of` date.

```json
{
  "query": "What are the compliance penalties under US HIPAA regulations?",
  "disposition": "CONTROLLED_ABSTENTION",
  "verified_excerpts": [],
  "historical_superseded_excerpts": [],
  "summary_narrative": "Controlled Abstention: Insufficient evidence found in the SEBI regulatory corpus to substantiate an answer.",
  "technical_validation_passed": true,
  "retrieval_candidate_count": 0,
  "abstention_reason": "insufficient_evidence",
  "regulatory_warnings": [],
  "answer_text": "Controlled Abstention: Insufficient evidence found in the SEBI regulatory corpus to substantiate an answer for this query.",
  "synthesis_status": "ABSTAINED",
  "claim_citations": [],
  "unresolved_subquestions": [],
  "synthesis_failure_reason": "Insufficient evidence: candidate passages do not substantively cover the query topic."
}
```

### Infrastructure Errors (HTTP 4xx / 5xx)
- **HTTP 400 Bad Request:** Malformed payload or invalid operation (e.g. attempting to retry a non-failed job).
- **HTTP 404 Not Found:** Requested job, snapshot, or collection does not exist.
- **HTTP 422 Unprocessable Entity:** Schema validation failure (e.g. query length $<2$ or $>500$ chars, invalid date syntax).
- **HTTP 429 Too Many Requests:** IP rate limit exceeded (`RATE_LIMIT_QUERY_PER_MINUTE`).
- **HTTP 502 / 504 Gateway Error:** External synthesis provider timeout or network error. Returns safe JSON; never exposes API keys or stack traces.
- **HTTP 503 Service Unavailable:** Active snapshot missing, manifest corrupted, or database unreachable.

---

## 5. Administrative & Index Management Endpoints

Administrative routes reside under `/api/v1/admin` and require the `X-Admin-Key` header matching `ADMIN_API_KEY`. If `ADMIN_API_KEY` is not configured in production, admin routes fail closed with HTTP 500.

### Snapshot & Index Publishing Endpoints (`IMPLEMENTED AND VERIFIED`)

#### `POST /api/v1/admin/indexes/publish`
Builds, validates, and atomically activates a reproducible search index snapshot from processed corpus directories.
- **Header:** `X-Admin-Key: <ADMIN_API_KEY>`
- **Query Parameter:** `dry_run` (boolean, default `false`)
- **Response (HTTP 200 OK):**
  ```json
  {
    "success": true,
    "snapshot_id": "snapshot_20261021_120000_123456",
    "total_documents": 5,
    "total_chunks": 168,
    "corpus_fingerprint": "9a8b7c6d5e4f3a2b...",
    "manifest_path": "data/indexes/snapshots/snapshot_20261021_120000_123456/manifest.json",
    "message": "Successfully published and activated index snapshot snapshot_20261021_120000_123456."
  }
  ```

#### `GET /api/v1/admin/indexes/status`
Inspects the active snapshot pointer and metadata manifest.
- **Header:** `X-Admin-Key: <ADMIN_API_KEY>`
- **Response (HTTP 200 OK):**
  ```json
  {
    "has_active_snapshot": true,
    "active_snapshot_id": "snapshot_20261021_120000_123456",
    "active_manifest": {
      "snapshot_id": "snapshot_20261021_120000_123456",
      "created_at": "2026-10-21T12:00:00Z",
      "embedding_model": "BAAI/bge-small-en-v1.5",
      "embedding_dimension": 384,
      "total_chunks": 168,
      "total_documents": 5,
      "document_ids": [
        "sebi_cscrf_2024_113",
        "sebi_cscrf_2025_045",
        "sebi_cscrf_2025_060",
        "sebi_cscrf_2025_096",
        "sebi_cscrf_2025_119"
      ]
    },
    "pending_documents": [],
    "total_pending_documents": 0
  }
  ```

### Document Ingestion Job Endpoints (`IMPLEMENTED, VERIFICATION INCOMPLETE`)

These endpoints exist in `backend/app/api/v1/admin.py` and interface with `IngestionOrchestrator` when a database is configured:
- `POST /api/v1/admin/documents/register`: Asynchronously registers a document URL for background downloading, text extraction, and chunking.
- `GET /api/v1/admin/jobs/{job_id}/status`: Queries lifecycle execution status (`queued`, `running`, `completed`, `failed`).
- `GET /api/v1/admin/jobs`: Lists recent ingestion jobs with pagination.
- `POST /api/v1/admin/jobs/{job_id}/retry`: Resets a failed ingestion job for retry.

*(Note: In Phase 1, offline manifest-driven ingestion takes precedence to support zero-cost local execution without requiring database background workers).*
