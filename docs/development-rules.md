# Development Rules for Humans & AI Coding Agents

## 1. Work Protocol & Engineering Standards
1. **Inspect Before Modifying:** Always inspect the existing codebase, schema definitions, and test suites before writing or altering code.
2. **Single-Task Focus:** Implement one scoped task or milestone at a time. Do not attempt multiple roadmap phases in a single run.
3. **Smallest Coherent Change:** Keep diffs minimal, clean, and tightly targeted to the stated objective.
4. **Mandatory Test Verification:** Run relevant tests (`pytest`) before and after every modification. Never claim a test passed without actually running it.
5. **Accurate Tracking:** Update `tasks.md` and documentation whenever features, decisions, or system behaviors change.
6. **Stop & Clarify:** If ambiguous requirements or breaking architectural changes arise, pause and ask for guidance before proceeding.

---

## 2. Explicit Approvals Required Before
- Changing core architectures or replacing foundational technologies.
- Introducing paid cloud services, billable APIs, or requiring credit card authorization.
- Sending user queries or regulatory document text to external third-party model providers without configuration.
- Downloading models exceeding 500MB or running large automated scraping spiders.
- Adding mandatory external services (such as hosted databases or Redis) that break local zero-cost standalone execution.
- Deleting existing tests, fixtures, or baseline corpus documents.
- Modifying public API contracts in a backward-incompatible manner.

---

## 3. Engineering & Domain Invariants

### 3.1. Official Source Provenance & Hash Integrity
- Every ingested document must retain its official source URL, physical PDF path, and SHA-256 hash.
- Every chunk must preserve both 1-indexed PDF page numbers and printed page strings.

### 3.2. Directed Graph & Temporal Invariants
- All regulatory relationship edges are strictly directed:
  $$\text{source\_document\_id (acting / newer)} \xrightarrow{\text{RELATIONSHIP}} \text{target\_document\_id (affected / older)}$$
- No self-loops are permitted (`source != target`).
- **Currency is never inferred from dates alone:** A newer publication date does not imply supersession without an explicit, verified relationship edge.
- **Provision-Level Scoping:** Partial amendments must specify `affected_provisions` to ensure unaffected clauses remain in force.

### 3.3. Grounding & Fail-Closed Abstention
- All claims must be mechanically attributed to retrieved context chunks.
- The system must abstain with a structured refusal (`refuse: true`) when evidence is missing, conflicting, out of domain, or superseded.
- For extractive responses, claims must match source chunk text with 100% character identity.
- In Phase 8E, hosted LLM synthesis must fail-closed to verified extractive excerpts whenever claim verification fails.

### 3.4. Database Invariants & Migration Safety
- **Safe Test Purging:** Destructive database operations (`drop_all`) must strictly require that the database name contains `test` (e.g. `sebi_rag_test`) or that `ALLOW_TEST_DATABASE_PURGE=1` is explicitly set in the execution environment.
- **Alembic Capacity Safety:** Alembic version table width must accommodate descriptive revision strings (e.g., `0001_initial_persistence_foundation`) using the `_ensure_version_table_capacity` helper in `backend/alembic/env.py`.
- **Vector Dimension Preservation:** Embedding dimensions must remain strictly aligned to 384 dimensions (`BAAI/bge-small-en-v1.5`). Dimension mismatches must be caught and rejected at the repository boundary before persistence.
- **Immutable Versioning:** Ingested documents with changed PDF checksums must generate new immutable version records (`document_versions`) rather than overwriting historical extractions in-place.
- **Verified Edge Protection:** Relationship re-imports must never downgrade or overwrite verified edges (`review_status = 'verified'`).

---

## 4. Secrets, Privacy & Security
- Keep `.env` strictly excluded from Git tracking via `.gitignore`.
- Keep `.env.example` free of real API keys or sensitive connection strings.
- Never expose LLM provider API keys, database credentials, or secret tokens in client bundles.
- Do not log raw prompt text or sensitive user information.
- Treat extracted PDF text as untrusted content; ensure retrieved context cannot override system instructions or alter tool execution.

---

## 5. Local Reproducibility & Zero Mandatory Spend
- The entire project must remain runnable and testable locally on standard hardware at zero cost.
- Test suites must execute deterministically without requiring live internet connections or paid external APIs.
- The initial 5-document CSCRF corpus serves as a permanent local regression baseline.

---

## 6. Completion Report Standard
At the conclusion of each engineering milestone, report:
1. Executive summary of actions performed.
2. List of modified and created files with rationale.
3. Exact commands run and test execution outputs.
4. Implementation status labels for all affected components.
5. Known limitations and next recommended steps.
