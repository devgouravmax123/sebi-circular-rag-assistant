# Citenza — AI Document Intelligence & Research Platform

> **Your documents. Verified answers.**

Citenza is a production-grade, multi-user, self-service AI document intelligence and research platform. It empowers individuals and teams to create private workspaces, organize documents into custom collections, upload arbitrary PDF files from the browser, and ask natural-language questions with mathematically verified source citations, strict tenant data isolation, and deterministic fail-closed abstention.

---

## 1. Product Vision & Architecture

Citenza is built from the ground up as a general-purpose, multi-user document intelligence platform. Users can research corporate policies, technical specifications, research whitepapers, incident response playbooks, financial guidelines, or legal contracts without hallucinated claims or ungrounded assertions.

### Core Platform Guarantees
1. **Self-Service Multi-Tenancy & Guest Mode:** Sign in securely using Firebase Authentication, link workspaces, or test immediately via isolated Guest Sessions without creating permanent credentials.
2. **Trustworthy, Grounded Answers:** Every factual assertion synthesized by the LLM is mechanically verified against retrieved source text chunks, complete with 1-indexed physical and printed PDF page citations.
3. **Deterministic Fail-Closed Abstention:** If evidence is missing, conflicting, out-of-scope, or insufficient, Citenza explicitly abstains (`CONTROLLED_ABSTENTION`) rather than speculating.
4. **Verbatim Citation Validation & Candidate Binding:** Answers must cite only passages that actually exist in the server-side retrieved candidate set and match the source text verbatim.
5. **Strict Multi-Tenant Isolation:** Workspaces, collections, documents, vector embeddings, lexical indices, and query histories are strictly segregated by workspace UUID. Cross-tenant leakage is zero-tolerance ($0.0\%$).
6. **Zero-Cost Production Hosting Architecture ($0/month):** Engineered to operate within the generous free tiers of Render (FastAPI Web Service), Vercel (React SPA), Neon (Serverless PostgreSQL), Backblaze B2 (S3-compatible Object Storage), and Groq (LLM Synthesis).

---

## 2. System Architecture

```text
                                  +-------------------------------------------------------------+
                                  |                 INGESTION & STORAGE ENGINE                  |
                                  |                                                             |
   Browser Upload                 |   +---------------------+     +-------------------------+   |
   (Arbitrary PDF Files) -------> |   | Multipart Streaming | --> | PyMuPDF Text Extraction |   |
                                  |   +---------------------+     +-------------------------+   |
                                  |                                            |                |
                                  |                                            v                |
                                  |                               +-------------------------+   |
                                  |                               | Deterministic Chunking  |   |
                                  |                               +-------------------------+   |
                                  +--------------------------------------------|----------------+
                                                                               |
                                                                               v
                                  +-------------------------------------------------------------+
                                  |                    PERSISTENCE & STORAGE                    |
                                  |                                                             |
                                  |   Backblaze B2 Object Storage     Neon PostgreSQL           |
                                  |   - Raw PDF binaries              - Workspaces & Users      |
                                  |   - Published Index Snapshots     - Collections & Documents |
                                  |     (chunks, docs, dense vectors) - Query History & Jobs    |
                                  +-------------------------------------------------------------+
                                                                               ^
                                                                               |
                                  +--------------------------------------------|----------------+
                                  |                 QUERY ORCHESTRATION PIPELINE                |
                                  |                                                             |
   React UI / Client              |   +-----------------------------------------------------+   |
   POST /api/v1/query ----------> |   | FastAPI Query Controller & Workspace Authorizer     |   |
                                  |   +-----------------------------------------------------+   |
                                  |                              |                              |
                                  |                              v                              |
                                  |   +-----------------------------------------------------+   |
                                  |   | Hybrid Retrieval (BGE-Small Dense + BM25 Lexical)   |   |
                                  |   +-----------------------------------------------------+   |
                                  |                              |                              |
                                  |                              v                              |
                                  |   +-----------------------------------------------------+   |
                                  |   | Substantive Relevance Filter & Candidate Scoping    |   |
                                  |   +-----------------------------------------------------+   |
                                  |                              |                              |
                                  |                              v                              |
                                  |   +-----------------------------------------------------+   |
                                  |   | Answer Safety Gate (Verbatim Citation Validation)   |   |
                                  |   +-----------------------------------------------------+   |
                                  |                              |                              |
                                  |                              v                              |
                                  |   +-----------------------------------------------------+   |
                                  |   | Grounded LLM Synthesis (Groq Llama 3.3 70B / Mock)  |   |
                                  |   +-----------------------------------------------------+   |
                                  +-------------------------------------------------------------+
```

---

## 3. Technology Stack

- **Frontend:** React 18, TypeScript, Vite, Tailwind CSS, Lucide Icons, headless UI components.
- **Backend:** FastAPI, Python 3.12, Pydantic v2, SQLAlchemy 2.0, Alembic forward-only migrations.
- **Retrieval Engine:** Hybrid search combining BM25 Okapi lexical indexing, dense vector search (`BAAI/bge-small-en-v1.5`), and Reciprocal Rank Fusion (RRF).
- **LLM Synthesis:** Groq API (`llama-3.3-70b-versatile`) with strict context windows, provider fallback, and offline `MockSynthesisProvider` for hermetic testing.
- **Database:** Serverless Neon PostgreSQL with connection pooling.
- **Object Storage:** S3-compatible Backblaze B2 object storage for PDF binaries and published search index snapshot bundles.
- **Authentication:** Firebase Authentication (Spark free plan) supporting email/password registration with verification links, authenticated workspace provisioning, and session tokens.

---

## 4. Getting Started Locally

### Prerequisites
- Python 3.12+
- Node.js 20+
- Git

### Backend Setup
```bash
# Clone the repository
git clone https://github.com/your-username/sebi-rag-assistant.git
cd sebi-rag-assistant

# Create virtual environment and activate
python -m venv .venv
# Windows:
.\.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

# Install dependencies
pip install -r backend/requirements.txt

# Run backend API server locally
uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --reload
```

### Frontend Setup
```bash
cd frontend

# Install Node dependencies
npm install

# Start Vite development server
npm run dev
```

---

## 5. Automated Verification & Testing

The repository maintains an extensive test suite verifying mathematical grounding, deterministic abstention, workspace tenant isolation, and API stability.

```bash
# Run all backend unit and contract tests (863 passing tests):
python -m pytest backend/tests -m "not db and not integration"

# Run frontend test suite (61 passing tests):
npm test --prefix frontend

# Verify frontend production build:
npm run build --prefix frontend
```

---

## 6. Demonstration Collections

The platform includes pre-indexed demonstration collections for testing and verification:
- `general_synthetic`: Acme Global Technical & Infrastructure Handbook (infrastructure architecture, security and access control policies, product rate-limiting specs).
- `cisa_playbooks`: CISA Federal Government Cybersecurity Incident and Vulnerability Response Playbooks.
- `demo_finops`: Acme Cloud FinOps & Governance Guide.

Users can create new custom collections and upload arbitrary PDFs through the web UI at any time.

---

## 7. License

Apache-2.0. See `LICENSE` for details.
