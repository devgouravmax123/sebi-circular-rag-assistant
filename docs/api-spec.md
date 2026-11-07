# REST API Specification

## 1. General Principles & Protocols
- **Base URL:** `/api/v1` (with root liveness probes at `/health` and `/ready`)
- **Protocol:** HTTP/1.1 or HTTP/2 over TLS, JSON payloads (`Content-Type: application/json`) and multipart form data (`multipart/form-data`) for document uploads.
- **Authentication:**
  - **Public Routes:** `/health`, `/ready`, `/api/v1/collections` (public list), `/api/v1/query` (when querying public system demo collections).
  - **User Authenticated Routes:** Require `Authorization: Bearer <JWT>` containing verified `user_id`.
  - **Administrative Routes:** Require `X-Admin-Key: <ADMIN_API_KEY>` header.
- **Server-Side Secret Isolation:** All API credentials, database strings, and cloud keys reside strictly in server environment variables. They are never accepted via client payloads or exposed in responses.
- **Fail-Closed Authorization:** Unauthorized requests to private workspaces or collections return **HTTP 404 Not Found** (instead of HTTP 403 Forbidden) to eliminate workspace and collection enumeration vulnerabilities.
- **Deployment Status Annotations:** Every endpoint below is explicitly tagged:
  - `[DEPLOYED IN PRODUCTION]`: Live on Render Free Web Service (commit `71dc1ae`).
  - `[LOCALLY IMPLEMENTED / PENDING RELEASE]`: Fully implemented and tested in the local repository worktree, pending database migration and Render release.

---

## 2. Health & System Probes

### `GET /health` `[DEPLOYED IN PRODUCTION]`
Liveness probe indicating that the FastAPI process is responsive.
- **Auth:** None
- **Response (HTTP 200 OK):**
  ```json
  {
    "status": "ok"
  }
  ```

### `GET /ready` `[DEPLOYED IN PRODUCTION]`
Readiness probe verifying search index availability and database connectivity.
- **Auth:** None
- **Response (HTTP 200 OK):**
  ```json
  {
    "status": "ready",
    "storage_mode": "file_backed",
    "index_active": true
  }
  ```
- **Error Response (HTTP 503 Service Unavailable):**
  ```json
  {
    "detail": {
      "status": "not_ready",
      "reasons": ["Active search index snapshot pointer missing or not published"]
    }
  }
  ```

---

## 3. User Authentication & Identity Endpoints `[LOCALLY IMPLEMENTED / PENDING RELEASE]`

Implementation: [`backend/app/api/v1/auth.py`](file:///c:/Users/Raja/OneDrive/Desktop/sebi-rag-assistant/backend/app/api/v1/auth.py).

### `POST /api/v1/auth/register`
Creates a new user account, hashes the password using Argon2id, automatically provisions a default personal workspace, and issues an access token.
- **Auth:** None
- **Request Body (`UserSignupRequest`):**
  ```json
  {
    "email": "analyst@example.com",
    "password": "SecurePassword123!",
    "full_name": "Jane Doe"
  }
  ```
- **Validation Rules:**
  - `email`: Normalized lowercase; valid email regex.
  - `password`: String length $\ge 8$ and $\le 128$; cannot be solely whitespace.
- **Response (HTTP 201 Created — `TokenResponse`):**
  ```json
  {
    "access_token": "eyJhbGciOiJIUzI1NiIsIn...",
    "token_type": "bearer",
    "expires_in_seconds": 3600,
    "user": {
      "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "email": "analyst@example.com",
      "full_name": "Jane Doe",
      "is_active": true,
      "created_at": "2026-11-07T12:00:00Z"
    }
  }
  ```
- **Errors:**
  - `HTTP 409 Conflict`: An account with this email address already exists.
  - `HTTP 422 Unprocessable Entity`: Password $<8$ characters or invalid email format.

### `POST /api/v1/auth/login`
Authenticates user credentials and returns a signed JWT access token.
- **Auth:** None
- **Request Body (`UserLoginRequest`):**
  ```json
  {
    "email": "analyst@example.com",
    "password": "SecurePassword123!"
  }
  ```
- **Response (HTTP 200 OK — `TokenResponse`):** Same schema as `/register`.
- **Errors:**
  - `HTTP 401 Unauthorized`: Invalid email or password.

### `GET /api/v1/auth/me`
Retrieves the currently authenticated user's profile and active workspaces.
- **Auth:** `Authorization: Bearer <JWT>`
- **Response (HTTP 200 OK — `UserProfileResponse`):**
  ```json
  {
    "user": {
      "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "email": "analyst@example.com",
      "full_name": "Jane Doe",
      "is_active": true,
      "created_at": "2026-11-07T12:00:00Z"
    },
    "workspaces": [
      {
        "workspace_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
        "name": "Jane Doe's Workspace",
        "slug": "jane-doe-workspace",
        "role": "owner"
      }
    ]
  }
  ```
- **Errors:**
  - `HTTP 401 Unauthorized`: Missing, expired, or invalid token.

---

## 4. Workspace & Member Management Endpoints `[LOCALLY IMPLEMENTED / PENDING RELEASE]`

Implementation: [`backend/app/api/v1/workspaces.py`](file:///c:/Users/Raja/OneDrive/Desktop/sebi-rag-assistant/backend/app/api/v1/workspaces.py).

### `POST /api/v1/workspaces`
Provisions a new private workspace with the calling user as `owner`.
- **Auth:** `Authorization: Bearer <JWT>`
- **Request Body (`WorkspaceCreateRequest`):**
  ```json
  {
    "name": "Compliance & Risk Advisory",
    "slug": "compliance-risk-advisory"
  }
  ```
- **Response (HTTP 201 Created — `WorkspaceResponse`):**
  ```json
  {
    "id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
    "name": "Compliance & Risk Advisory",
    "slug": "compliance-risk-advisory",
    "owner_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "created_at": "2026-11-07T12:05:00Z"
  }
  ```
- **Errors:**
  - `HTTP 409 Conflict`: Slug already in use or reserved (`system`, `admin`, `api`).

### `GET /api/v1/workspaces`
Lists all workspaces where the user is an active member.
- **Auth:** `Authorization: Bearer <JWT>`
- **Response (HTTP 200 OK):** Array of `WorkspaceResponse` objects.

### `GET /api/v1/workspaces/{workspace_id}`
Returns details for a specific workspace.
- **Auth:** `Authorization: Bearer <JWT>` (Must be member of `workspace_id`)
- **Response (HTTP 200 OK — `WorkspaceDetailResponse`):** Includes workspace metadata and member roster.
- **Errors:**
  - `HTTP 404 Not Found`: Workspace does not exist or user is not an authorized member.

### `POST /api/v1/workspaces/{workspace_id}/members`
Invites an existing user to join a workspace with a specified role (`admin`, `member`, `viewer`).
- **Auth:** `Authorization: Bearer <JWT>` (Requires `owner` or `admin` in `workspace_id`)
- **Request Body (`WorkspaceMemberAddRequest`):**
  ```json
  {
    "email": "colleague@example.com",
    "role": "member"
  }
  ```
- **Response (HTTP 201 Created):** Member details.

---

## 5. Collection Management & Document Ingestion Endpoints `[LOCALLY IMPLEMENTED / PENDING RELEASE]`

Implementation: [`backend/app/api/v1/collections.py`](file:///c:/Users/Raja/OneDrive/Desktop/sebi-rag-assistant/backend/app/api/v1/collections.py).

### `GET /api/v1/collections` `[DEPLOYED IN PRODUCTION]`
Lists public demonstration collections available to all users.
- **Auth:** None
- **Response (HTTP 200 OK):**
  ```json
  [
    {
      "id": "sebi_cscrf",
      "name": "SEBI Cybersecurity and Cyber Resilience Framework",
      "description": "Official master circulars and technical revisions for CSCRF compliance.",
      "policy_name": "sebi_cscrf_strict",
      "enforce_regulatory_graph": true,
      "is_system": true,
      "is_public": true
    },
    {
      "id": "cisa_playbooks",
      "name": "CISA Cybersecurity Playbooks",
      "description": "Operational cybersecurity incident and vulnerability response playbooks.",
      "policy_name": "general_static",
      "enforce_regulatory_graph": false,
      "is_system": true,
      "is_public": true
    }
  ]
  ```

### `POST /api/v1/workspaces/{workspace_id}/collections` `[LOCALLY IMPLEMENTED]`
Creates a new private collection scoped to the authorized workspace.
- **Auth:** `Authorization: Bearer <JWT>` (Requires `owner`, `admin`, or `member` in `workspace_id`)
- **Request Body (`CollectionCreateRequest`):**
  ```json
  {
    "id": "fintech_risk_policies",
    "name": "Fintech Risk Policies 2026",
    "description": "Internal standard operating procedures and risk mitigation manuals.",
    "policy_name": "general_static",
    "enforce_regulatory_graph": false
  }
  ```
- **Validation Rules:**
  - `id`: Lowercase alphanumeric and underscores only; cannot conflict with system IDs.
  - `policy_name`: `"general_static"` (default) or `"sebi_cscrf_strict"`.
- **Response (HTTP 201 Created — `CollectionDetailResponse`):**
  ```json
  {
    "id": "fintech_risk_policies",
    "workspace_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
    "name": "Fintech Risk Policies 2026",
    "description": "Internal standard operating procedures and risk mitigation manuals.",
    "policy_name": "general_static",
    "enforce_regulatory_graph": false,
    "is_system": false,
    "is_public": false,
    "created_at": "2026-11-07T12:10:00Z"
  }
  ```
- **Errors:**
  - `HTTP 409 Conflict`: Collection ID already exists in this workspace or conflicts with system collection.

### `GET /api/v1/workspaces/{workspace_id}/collections` `[LOCALLY IMPLEMENTED]`
Lists all private collections belonging to the workspace.
- **Auth:** `Authorization: Bearer <JWT>` (Requires reader access to `workspace_id`)
- **Response (HTTP 200 OK):** Array of `CollectionDetailResponse` objects.

### `POST /api/v1/workspaces/{workspace_id}/collections/{collection_id}/upload` `[LOCALLY IMPLEMENTED]`
Uploads a binary PDF document via multipart form data, validates format and magic bytes, persists the file to Backblaze B2, registers document metadata, and enqueues a durable background ingestion job.
- **Auth:** `Authorization: Bearer <JWT>` (Requires writer access to `workspace_id`)
- **Content-Type:** `multipart/form-data`
- **Form Fields:**
  - `file`: Binary PDF file (Max 30 MiB; `%PDF-` magic bytes strictly verified).
  - `title` *(optional)*: Human-readable document title.
  - `circular_number` *(optional)*: Reference code or circular number.
- **Response (HTTP 201 Created — `CollectionIngestionJobResponse`):**
  ```json
  {
    "job_id": "e3f1b4c8-47bc-49e0-819a-32fa4c0d0124",
    "workspace_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
    "collection_id": "fintech_risk_policies",
    "document_id": "doc_a1b2c3d4e5f6",
    "source_url": "b2://uploads/9b1deb4d/fintech_risk_policies/doc_a1b2c3d4e5f6.pdf",
    "status": "queued",
    "stage": "pending",
    "idempotency_key": "upload_9a8b7c6d_e3f1b4c8",
    "is_duplicate_registration": false,
    "message": "Document uploaded successfully and queued for ingestion.",
    "created_at": "2026-11-07T12:15:00Z"
  }
  ```
- **Duplicate Document Response (HTTP 200 OK):**
  If an identical document (matching SHA-256) is already registered in this collection, the redundant upload is cleaned up and the existing job status is returned with `is_duplicate_registration: true`.
- **Errors:**
  - `HTTP 400 Bad Request`: Non-PDF file extension or corrupted magic bytes (not `%PDF-`).
  - `HTTP 403 Forbidden`: Attempting to upload to a system collection (`is_system = true`).
  - `HTTP 404 Not Found`: Workspace or collection does not exist or user lacks access.
  - `HTTP 413 Payload Too Large`: File exceeds 30 MiB limit.

### `GET /api/v1/workspaces/{workspace_id}/collections/{collection_id}/ingestion-jobs` `[LOCALLY IMPLEMENTED]`
Lists all ingestion jobs for the collection with execution status and stage progress.
- **Auth:** `Authorization: Bearer <JWT>`
- **Response (HTTP 200 OK):** Array of `CollectionIngestionJobResponse` objects.

### `GET /api/v1/workspaces/{workspace_id}/collections/{collection_id}/ingestion-jobs/{job_id}` `[LOCALLY IMPLEMENTED]`
Inspects status for a specific ingestion job.
- **Auth:** `Authorization: Bearer <JWT>`
- **Response (HTTP 200 OK — `CollectionIngestionJobResponse`):**
  ```json
  {
    "job_id": "e3f1b4c8-47bc-49e0-819a-32fa4c0d0124",
    "status": "completed",
    "stage": "finished",
    "attempt_count": 1,
    "max_attempts": 3,
    "error_message": null,
    "created_at": "2026-11-07T12:15:00Z",
    "completed_at": "2026-11-07T12:15:45Z"
  }
  ```

---

## 6. Query Execution Endpoints

### `POST /api/v1/query` `[DEPLOYED IN PRODUCTION / EXPANDED IN WORKTREE]`
Submits a natural language query against a specified collection, retrieves candidate evidence chunks, applies grounding policies, evaluates the safety gate, synthesizes an answer via Groq LLM, and mechanically verifies citations.

#### Request Schema (`QueryRequest` in `backend/app/schemas/query.py`)
```json
{
  "collection_id": "fintech_risk_policies",
  "query": "What are the data retention requirements for customer transaction logs?",
  "top_k": 5,
  "as_of_date": "2026-11-01",
  "include_superseded": false
}
```

| Field | Type | Required | Default | Description |
| :--- | :--- | :---: | :---: | :--- |
| `collection_id` | string | No* | `"sebi_cscrf"` | Target collection ID. *Mandatory for private collections. |
| `query` | string | **Yes** | — | Question text (min 2, max 500 characters). |
| `top_k` | integer | No | `5` | Candidate passages to retrieve (1 to 10). |
| `as_of_date` | string | No | `null` | Historical evaluation cutoff date (`YYYY-MM-DD`). |
| `include_superseded` | boolean | No | `false` | Whether to include superseded provisions (regulatory collections only). |

#### Response Schema — Answerable Query (`QueryResponse`)
```json
{
  "query": "What are the data retention requirements for customer transaction logs?",
  "disposition": "VERIFIED_HISTORICAL_EXCERPTS",
  "verified_excerpts": [
    {
      "excerpt_id": "excerpt_1",
      "source_chunk_id": "doc_a1b2c3d4_p12_0",
      "document_id": "doc_a1b2c3d4",
      "circular_number": "POL-2026-04",
      "title": "Data Governance Manual",
      "page_start": 12,
      "page_end": 12,
      "excerpt_text": "Section 4.3: Customer transaction logs must be retained in immutable cold storage for a minimum period of seven (7) years.",
      "regulatory_status": "IN_FORCE"
    }
  ],
  "answer_text": "Customer transaction logs must be retained in immutable cold storage for at least seven (7) years under Section 4.3 of the Data Governance Manual [doc_a1b2c3d4_p12_0].",
  "synthesis_status": "FULLY_ANSWERED",
  "claim_citations": [
    {
      "claim_text": "Customer transaction logs must be retained in immutable cold storage for at least seven (7) years.",
      "supporting_chunk_ids": ["doc_a1b2c3d4_p12_0"],
      "citations": [
        {
          "source_chunk_id": "doc_a1b2c3d4_p12_0",
          "document_id": "doc_a1b2c3d4",
          "page_start": 12,
          "page_end": 12
        }
      ]
    }
  ],
  "abstention_reason": null,
  "retrieval_candidate_count": 1
}
```

#### Response Schema — Controlled Abstention (`QueryResponse`)
Returned when evidence is insufficient, ambiguous, contradictory, or out of scope:
```json
{
  "query": "What are the penalty amounts under US HIPAA regulations?",
  "disposition": "CONTROLLED_ABSTENTION",
  "verified_excerpts": [],
  "answer_text": "Controlled Abstention: Insufficient evidence found in the selected collection to substantiate an answer for this query.",
  "synthesis_status": "ABSTAINED",
  "claim_citations": [],
  "abstention_reason": "insufficient_evidence",
  "retrieval_candidate_count": 0
}
```

- **Errors:**
  - `HTTP 401 Unauthorized`: Querying a private collection without valid JWT.
  - `HTTP 404 Not Found`: Collection does not exist or caller does not belong to owning workspace.
  - `HTTP 400 Bad Request`: Collection has no published snapshot.
  - `HTTP 429 Too Many Requests`: Rate limit exceeded.

---

## 7. Query History Endpoints `[LOCALLY IMPLEMENTED / PENDING RELEASE]`

Implementation: [`backend/app/api/v1/query_history.py`](file:///c:/Users/Raja/OneDrive/Desktop/sebi-rag-assistant/backend/app/api/v1/query_history.py).

### `GET /api/v1/workspaces/{workspace_id}/history`
Retrieves paginated query history for the authenticated user within the workspace.
- **Auth:** `Authorization: Bearer <JWT>`
- **Query Parameters:** `limit` (default 20, max 100), `offset` (default 0).
- **Response (HTTP 200 OK — `QueryHistoryListResponse`):**
  ```json
  {
    "items": [
      {
        "id": "qry_88310f1a-b32c-47ea-a021-998811223344",
        "query_text": "What are the data retention requirements for customer transaction logs?",
        "collection_id": "fintech_risk_policies",
        "disposition": "VERIFIED_HISTORICAL_EXCERPTS",
        "answer_text": "Customer transaction logs must be retained for at least 7 years...",
        "citation_count": 1,
        "latency_ms": 420.5,
        "created_at": "2026-11-07T12:20:00Z"
      }
    ],
    "total": 1
  }
  ```

### `GET /api/v1/workspaces/{workspace_id}/history/{history_id}`
Retrieves full details for a historical query, including verified excerpts and claim citations.
- **Auth:** `Authorization: Bearer <JWT>`
- **Response (HTTP 200 OK):** Detailed query record.
- **Errors:**
  - `HTTP 404 Not Found`: Entry not found or belongs to another user/workspace.

---

## 8. Administrative Endpoints `[DEPLOYED IN PRODUCTION / RESTRICTED]`

Implementation: [`backend/app/api/v1/admin.py`](file:///c:/Users/Raja/OneDrive/Desktop/sebi-rag-assistant/backend/app/api/v1/admin.py).
Requires header: `X-Admin-Key: <ADMIN_API_KEY>`.

### `POST /api/v1/admin/indexes/publish`
Builds, validates, and activates a search index snapshot from processed directories.
- **Query Parameter:** `dry_run` (boolean, default `false`).
- **Response (HTTP 200 OK):**
  ```json
  {
    "success": true,
    "snapshot_id": "snapshot_20261107_120000_123456",
    "total_documents": 5,
    "total_chunks": 168,
    "corpus_fingerprint": "9a8b7c6d5e4f...",
    "manifest_path": "data/indexes/snapshots/snapshot_20261107_120000_123456/manifest.json",
    "message": "Successfully published and activated index snapshot."
  }
  ```

### `GET /api/v1/admin/indexes/status`
Inspects active snapshot pointer and metadata manifest.
- **Response (HTTP 200 OK):** Status details including snapshot ID, created timestamp, chunk count, and document catalog.
