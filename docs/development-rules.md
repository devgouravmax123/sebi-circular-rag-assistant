# Citenza — Development Rules & Engineering Standards

> **Your documents. Verified answers.**

## 1. Safety & Architecture Invariants

1. **Strictly Local Development Execution:** Never run database migrations or deploy against production cloud databases without explicit user confirmation and preflight checks.
2. **Never Expose Secrets:** Environment secrets, Firebase private keys, database passwords, and API keys must never be committed to Git or printed in console logs.
3. **Forward-Only Database Evolution:** Revisions `0001` through `0011` are permanently applied to production. All new schema changes must be forward-only Alembic revisions (e.g. `0012`).
4. **Verbatim Evidence Grounding:** All claims asserted by synthesis providers must be verified verbatim against authoritative source chunk text.
5. **Retrieval-Candidate Binding:** Answers may only cite chunks present in the server-side retrieved candidate set for that specific query.
6. **Deterministic Abstention:** If evidence is insufficient, conflicting, or out of scope, the system must fail-closed to `CONTROLLED_ABSTENTION`.
7. **Workspace & Collection Isolation:** All tenant data must be isolated by `workspace_id`. Cross-collection and cross-workspace leakage is strictly prohibited.

---

## 2. Code Quality & Testing Standards

- **Backend Tests:** Run pytest with virtual environment:
  ```bash
  python -m pytest backend/tests -m "not db and not integration"
  ```
- **Frontend Tests:** Run node test runner via Vite:
  ```bash
  npm test --prefix frontend
  ```
- **Build Verification:** Ensure frontend builds with zero TypeScript errors:
  ```bash
  npm run build --prefix frontend
  ```
