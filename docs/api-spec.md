# Citenza — REST API Specification

> **Your documents. Verified answers.**

## 1. General Principles & Protocols
- **Base URL:** `/api/v1` (with root probes at `/health` and `/ready`)
- **Protocol:** HTTP/1.1 or HTTP/2 over TLS, JSON payloads (`Content-Type: application/json`) and multipart form data (`multipart/form-data`) for document uploads.
- **Authentication:**
  - **Public Routes:** `/health`, `/ready`, `/api/v1/collections` (public system demo list), `/api/v1/query` (when querying public demo collections).
  - **Authenticated Routes:** Require `Authorization: Bearer <Firebase_ID_Token>` or `X-Guest-Session-Token: <UUID>`.
  - **Administrative Routes:** Require `X-Admin-Key: <ADMIN_API_KEY>` header.
- **Fail-Closed Authorization:** Unauthorized requests to private workspaces or collections return **HTTP 404 Not Found** (instead of HTTP 403 Forbidden) to eliminate workspace and collection enumeration vulnerabilities.

---

## 2. Health & System Probes

### `GET /health`
Liveness probe indicating that the FastAPI process is responsive.
- **Auth:** None
- **Response (HTTP 200 OK):**
  ```json
  {
    "status": "ok"
  }
  ```

### `GET /ready`
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

---

## 3. User Authentication & Workspaces

### `GET /api/v1/auth/me`
Retrieves authenticated user profile and list of accessible workspaces. Automatically provisions a personal workspace if the user is authenticated via Firebase for the first time.
- **Auth:** Bearer Token
- **Response (HTTP 200 OK):**
  ```json
  {
    "user_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "email": "user@example.com",
    "full_name": "Research Analyst",
    "workspaces": [
      {
        "workspace_id": "00000000-0000-0000-0000-000000000001",
        "name": "My Workspace",
        "slug": "my-workspace",
        "role": "owner"
      }
    ]
  }
  ```

### `POST /api/v1/workspaces`
Creates a new private workspace.
- **Auth:** Bearer Token
- **Request Body:**
  ```json
  {
    "name": "Security & Incident Response Team"
  }
  ```
- **Response (HTTP 201 Created):**
  ```json
  {
    "workspace_id": "7ca64708-410a-4299-8cfb-6f68c347f3b8",
    "name": "Security & Incident Response Team",
    "slug": "security-incident-response-team",
    "role": "owner"
  }
  ```

---

## 4. Guest Mode & Sessions

### `POST /api/v1/guest/initiate`
Initiates a temporary, isolated guest research session.
- **Auth:** None
- **Response (HTTP 200 OK):**
  ```json
  {
    "guest_session_id": "4b97ea3c-b174-4b53-b0ee-7c64b5952f19",
    "guest_token": "gst_9a8b7c6d5e4f3a2b1c0d",
    "workspace_id": "8f2a1b3c-4d5e-6f7a-8b9c-0d1e2f3a4b5c",
    "expires_at": "2026-11-17T17:00:00Z",
    "quota_remaining_uploads": 3
  }
  ```

### `GET /api/v1/guest/status`
Returns current guest session status, upload quota, and remaining time.
- **Auth:** `X-Guest-Session-Token`

---

## 5. Collections & Ingestion

### `GET /api/v1/collections`
Lists collections accessible to the user (all public demo collections plus private collections in the active workspace).
- **Auth:** Optional Bearer Token
- **Response (HTTP 200 OK):**
  ```json
  [
    {
      "id": "general_synthetic",
      "name": "Acme Technical & Infrastructure Handbook",
      "description": "General corporate technical handbook and infrastructure guidelines.",
      "policy_name": "general_static",
      "is_system": true,
      "document_count": 3
    },
    {
      "id": "cisa_playbooks",
      "name": "CISA Incident Response Playbooks",
      "description": "CISA Cybersecurity Incident and Vulnerability Response Playbooks.",
      "policy_name": "general_static",
      "is_system": true,
      "document_count": 1
    }
  ]
  ```

### `POST /api/v1/collections`
Creates a new custom collection in the active workspace.
- **Auth:** Bearer Token
- **Request Body:**
  ```json
  {
    "collection_id": "engineering_playbooks",
    "name": "Engineering Operations Playbooks",
    "description": "Internal SRE operational playbooks and architecture designs."
  }
  ```

### `POST /api/v1/collections/{collection_id}/upload`
Streams an arbitrary PDF document into the collection for automated ingestion.
- **Auth:** Bearer Token or Guest Token
- **Content-Type:** `multipart/form-data`
- **Form Fields:**
  - `file`: PDF binary (up to 30 MiB)
  - `title`: Document title
- **Response (HTTP 202 Accepted):**
  ```json
  {
    "job_id": "9c1b2a3f-4d5e-6a7b-8c9d-0e1f2a3b4c5d",
    "document_id": "doc_9c1b2a3f",
    "status": "queued",
    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
  }
  ```

---

## 6. Query & Grounded Synthesis

### `POST /api/v1/query`
Submits a natural-language research question scoped to a collection.
- **Auth:** Optional for public collections; required for private collections.
- **Request Body:**
  ```json
  {
    "query": "What is the mandatory password length under the Acme security policy?",
    "collection_id": "general_synthetic",
    "top_k": 3
  }
  ```
- **Response (HTTP 200 OK — Fully Answered):**
  ```json
  {
    "query": "What is the mandatory password length under the Acme security policy?",
    "collection_id": "general_synthetic",
    "disposition": "VERIFIED_HISTORICAL_EXCERPTS",
    "answer_text": "User passwords must be at least 16 characters in length containing uppercase, lowercase, numeric, and special symbol characters.",
    "synthesis_status": "FULLY_ANSWERED",
    "claim_citations": [
      {
        "claim_text": "User passwords must be at least 16 characters in length containing uppercase, lowercase, numeric, and special symbol characters.",
        "supporting_chunk_ids": ["acme_security_policy_p1_0"]
      }
    ],
    "verified_excerpts": [
      {
        "excerpt_id": "exc_01",
        "source_chunk_id": "acme_security_policy_p1_0",
        "document_id": "acme_security_policy",
        "page_start": 1,
        "page_end": 2,
        "excerpt_text": "User passwords must be at least 16 characters in length containing uppercase, lowercase, numeric, and special symbol characters."
      }
    ],
    "unresolved_subquestions": [],
    "technical_validation_passed": true
  }
  ```
- **Response (HTTP 200 OK — Controlled Abstention):**
  ```json
  {
    "query": "What are the margin trading rules for equity derivatives?",
    "collection_id": "general_synthetic",
    "disposition": "CONTROLLED_ABSTENTION",
    "answer_text": "Controlled Abstention: The retrieved passages do not contain substantive provisions addressing the query.",
    "synthesis_status": "ABSTAINED",
    "abstention_reason": "Insufficient evidence: candidate passages do not substantively cover the query topic.",
    "verified_excerpts": [],
    "claim_citations": [],
    "technical_validation_passed": true
  }
  ```

---

## 7. Query History

### `GET /api/v1/history`
Returns chronological query ledger for the active workspace.
- **Auth:** Bearer Token
- **Query Params:** `limit` (default 50), `collection_id` (optional filter).

### `GET /api/v1/history/{history_id}`
Returns complete detail of a past query session including verified excerpts and answer narrative.
- **Auth:** Bearer Token
