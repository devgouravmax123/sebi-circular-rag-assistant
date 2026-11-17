# Citenza — System Architecture

> **Your documents. Verified answers.**

## 1. Architectural Overview

Citenza is a production-grade, multi-user, self-service AI document intelligence platform designed to operate on a strict **$0/month hosting budget**.

```mermaid
flowchart TD
    subgraph "Client Layer"
        VercelSPA["Vercel Frontend SPA (React + TypeScript + Vite)"]
    end

    subgraph "Application Gateway (Render Free Tier)"
        RenderAPI["FastAPI Web Service (512 MB RAM / Uvicorn)"]
        AuthModule["Firebase Auth & Guest Sessions"]
        QueryOrch["Query Orchestrator & Safety Gate"]
        IngestRouter["Ingestion Router & Streamer"]
    end

    subgraph "Persistence & Cloud Services ($0/Month)"
        NeonDB["Neon Serverless PostgreSQL (Workspaces, Docs, History)"]
        B2Storage["Backblaze B2 (Raw PDFs & Search Snapshots)"]
        GroqLLM["Groq Llama 3.3 70B (Evidence-Grounded Synthesis)"]
        GAWorker["GitHub Actions / Scheduled Worker (Ingestion Runner)"]
    end

    VercelSPA -->|"HTTPS / Bearer JWT"| RenderAPI
    RenderAPI --> AuthModule
    RenderAPI --> QueryOrch
    RenderAPI --> IngestRouter
    RenderAPI -->|"CRUD / Pooled SQL"| NeonDB
    RenderAPI -->|"Stream Uploads / Read Snapshots"| B2Storage
    QueryOrch -->|"Context-Bounded Synthesis"| GroqLLM
    GAWorker -->|"Atomic Procedures"| NeonDB
    GAWorker -->|"Read Raw / Write Index Bundles"| B2Storage
```

---

## 2. Component Layers & Responsibilities

### 2.1. Client Layer (Vercel)
- **Framework:** React 18, TypeScript, Tailwind CSS, Vite.
- **Authentication:** Firebase Authentication Client SDK with verification links, plus instant Guest Mode session initiation.
- **Workspace Navigation:** Dynamic workspace selector, collection switcher, and persistent research history drawer.
- **Q&A Interface:** Natural-language query submission, answer card display, verified evidence drawer, and 1-indexed PDF page citation inspector.

### 2.2. Application Gateway (Render)
- **Framework:** FastAPI running on Python 3.12 with Uvicorn.
- **Authentication & Authorization:**
  - Firebase token validation via Firebase Admin SDK / JWKS.
  - Automatic workspace provisioning on first authenticated request.
  - Fail-closed workspace authorization: unauthorized access returns HTTP 404 to prevent enumeration.
- **Ingestion Streaming:** Receives multipart PDF streams, validates `%PDF-` magic bytes, computes SHA-256 digests, and streams to Backblaze B2.
- **Query Pipeline:**
  - Collection-scoped hybrid retrieval (BM25 lexical + dense vector search via `BAAI/bge-small-en-v1.5`).
  - Substantive relevance filtering ensuring candidate passages directly address query terms.
  - Mechanical `AnswerSafetyGate` validating claim citations against retrieved candidate chunks verbatim.
  - Grounded synthesis dispatch to Groq (`llama-3.3-70b-versatile`) with strict fallback to `MockSynthesisProvider` for offline testing.

### 2.3. Data & Storage Tier
- **Database (Neon PostgreSQL):**
  - Multi-tenant relational schema: `users`, `workspaces`, `workspace_memberships`, `collections`, `documents`, `chunks`, `ingestion_jobs`, `query_history`, `guest_sessions`.
  - Stored procedures: atomic job claiming (`app.claim_ingestion_job`) and guest expiration cleanup.
  - Direct connection (Port 5432) for schema migrations and pooled connection (Port 6543) for runtime API traffic.
- **Object Storage (Backblaze B2):**
  - S3-compatible object storage for immutable raw PDF binaries and published index snapshot bundles (`chunks.json`, `documents.json`, `dense_index.npy`, `manifest.json`).
- **Synthesis Provider (Groq):**
  - High-throughput inference for `llama-3.3-70b-versatile` executing under strict JSON contracts and evidence context limits.

---

## 3. End-to-End Query & Grounding Flow

```mermaid
sequenceDiagram
    autonumber
    actor User as User / Client
    participant API as FastAPI Router
    participant Auth as Workspace Auth
    participant Ret as Retrieval Service
    participant Gate as Answer Safety Gate
    participant Groq as Groq LLM Provider

    User->>API: POST /api/v1/query {query, collection_id}
    API->>Auth: Validate Token & Workspace Membership
    Auth-->>API: Authorized
    API->>Ret: Search Hybrid (BM25 + Dense RRF)
    Ret-->>API: Top Candidates
    API->>Ret: Filter Relevant Candidates (Substantive Match)
    alt No Relevant Candidates
        API-->>User: Controlled Abstention (Insufficient Evidence)
    else Relevant Candidates Exist
        API->>Groq: Synthesize Answer (Evidence Only)
        Groq-->>API: Synthesized Claims & Citations
        API->>Gate: Evaluate Draft (Verbatim Verification & Binding)
        alt Citations Valid
            Gate-->>API: VERIFIED_HISTORICAL_EXCERPTS
            API-->>User: Grounded Answer + Citations
        else Citation Verification Failed
            Gate-->>API: CONTROLLED_ABSTENTION / Fallback Excerpts
            API-->>User: Extractive Evidence Only / Abstention
        end
    end
```

---

## 4. Multi-Tenant Isolation Model

1. **Workspace Boundary:** Every collection, document, chunk, and query history record is strictly associated with a `workspace_id` UUID.
2. **Collection Isolation:** Ingestion indices and search queries operate strictly within a single `collection_id`. Cross-collection fusion is prohibited.
3. **Guest Session Isolation:** Guest users receive a dedicated temporary workspace with strict TTL expiration and automatic resource cleanup.
4. **Zero-Leakage Enforcement:** All queries and lookups require workspace authorization checks; unauthorized requests return HTTP 404.
