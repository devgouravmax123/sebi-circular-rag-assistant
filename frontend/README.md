# Frontend — SEBI Circular Assistant & Multi-Tenant Document Intelligence Platform

React 19 + TypeScript + Vite + Tailwind CSS interface for the SEBI Regulatory Intelligence Platform and Multi-Tenant SaaS.

## Prerequisites
- Node.js `v20+` / npm `10+`
- FastAPI backend running at `http://127.0.0.1:8000` (PostgreSQL test database with migrations 0001–0007 applied)

## Local Development Setup

1. **Install dependencies:**
   ```bash
   npm install
   ```

2. **Configure environment (Optional):**
   Copy `.env.example` to `.env` if a custom backend URL is desired:
   ```bash
   cp .env.example .env
   ```
   *Configured variables:*
   - `VITE_API_BASE_URL`: Base URL for the FastAPI backend (defaults to `http://127.0.0.1:8000` if omitted).

3. **Start the development server:**
   ```bash
   npm run dev
   ```
   The application will be accessible at `http://localhost:5173`.

4. **Run frontend tests:**
   ```bash
   npm test
   ```
   Executes all 37 unit and component tests (presentation, collection selector, and multi-tenant workspace/auth tests).

5. **Build production bundle:**
   ```bash
   npm run build
   ```
   Type-checks via `tsc -b` and builds production assets into `dist/`.

---

## Multi-Tenant Features & UI Workflows (Milestone 9)

### 1. Authentication & Session State
- **Anonymous Access**: Public demo collections (`sebi_cscrf`, `cisa_playbooks`) remain queryable without signing in.
- **Sign In / Sign Up**: Click "Sign In" or "Sign Up" in the header to open the auth modal. Passwords require at least 8 characters.
- **Session Restoration**: Authenticated sessions are restored via `GET /api/v1/auth/me`. If a token expires or becomes invalid, local session state is cleared cleanly.
- **Logout**: Discards client-side tokens and notifies `POST /api/v1/auth/logout`.

### 2. Workspaces & Roles
- **Workspace Selector**: Located in the top header. Shows the active workspace name, slug, and the caller's role (`owner`, `admin`, `member`, `viewer`).
- **Create Workspace**: Authorized users can create new workspaces by specifying name, slug, and description.
- **Manage Workspace**: Modal providing:
  - **Members Tab**: Lists members with email, full name, role, and joined date. Owners and Admins can invite new members by registered email or remove existing members (owners cannot be removed).
  - **Collections Tab**: Lists private collections in the selected workspace and allows owners/admins to create new private collections.

### 3. Collection Selection & Querying
- The collection dropdown displays both public/demo collections and authorized private collections belonging to the active workspace.
- Private collection queries automatically include the bearer token in the `Authorization` header.
- Answers, citations, and abstention reasons are rendered according to the collection's grounding policy.

### 4. Persistent Query History
- Click "📜 History" in the header to open the history drawer.
- Shows chronological paginated past queries with disposition labels and verified excerpt counts.
- Click any past query to inspect stored answer text, summary narrative, and citations.
- "Run Query Again" allows replaying any historical query directly into the search interface.
- Anonymous users see a friendly prompt to sign in.

---

## Known Limitations

1. **Stateless JWT Logout**: Backend tokens remain valid until expiration; client-side logout clears local tokens immediately.
2. **Private Document Ingestion**: Document upload/ingestion for newly created private collections is decoupled from the web UI and requires backend worker processing.
3. **In-Memory Rate Limiting**: The public query endpoint is rate-limited per backend instance.
