# Development Rules for Humans & AI Coding Agents

## 1. Work Protocol & Engineering Standards
1. **Inspect Before Modifying:** Always inspect existing code, schema definitions, test suites, and Git status before making changes. Never assume an implementation exists based on documentation alone.
2. **Single-Task Focus:** Implement one scoped task or milestone at a time. Do not jump across roadmap phases in a single engineering session.
3. **Smallest Coherent Change:** Keep diffs minimal, clean, and tightly targeted to the specific objective. Never reformat entire files or run repository-wide formatters.
4. **Mandatory Test Verification:** Run relevant test suites before and after modifications. Always report exact test execution commands, exit codes, and test counts. Never fabricate or extrapolate test results.
5. **Accurate Tracking & Documentation:** Keep documentation aligned with actual code. Clearly differentiate `IMPLEMENTED AND VERIFIED`, `IMPLEMENTED, VERIFICATION INCOMPLETE`, `PARTIALLY IMPLEMENTED`, `PLANNED`, `PROPOSED / REQUIRES APPROVAL`, and `DEFERRED` capabilities.
6. **Stop & Clarify:** If ambiguous requirements, contradictory documentation, or architectural crossroads arise, pause and request user guidance before proceeding.
7. **Preserve User Modifications:** Never overwrite, revert, or discard unrelated pre-existing working-tree changes.

---

## 2. Explicit Approvals Required Before
The following actions are strictly prohibited without prior explicit user authorization:
- Changing core system architectures or replacing foundational technologies.
- Introducing paid cloud services, billable APIs, or requiring credit card authorization.
- Making external database services (e.g. Neon, PostgreSQL) or caching services (e.g. Redis, Upstash) mandatory for running the application locally or executing tests.
- Migrating vector embeddings or index storage from local snapshots to external databases.
- Downloading models exceeding 500MB or running broad web-scraping spiders.
- Deleting, renaming, or modifying existing tests, test fixtures, or baseline corpus documents.
- Modifying the permanent SEBI CSCRF evaluation baseline (`evaluation/data/gold_evaluation_dataset.json`).
- Weakening the `AnswerSafetyGate` refusal rubric, grounding checks, or citation validation standards.
- Making backward-incompatible changes to public API request/response schemas.
- Staging, committing, pushing, or triggering production deployments.

---

## 3. Platform & Domain Invariants

### 3.1. Official Source Provenance & Hash Integrity
- Every ingested document must retain its source identifier, file path, and SHA-256 cryptographic hash.
- Every chunk must preserve both 1-indexed PDF page numbers and printed page strings.
- Missing metadata must remain missing: never invent publication dates, effective dates, section codes, or URLs.

### 3.2. Collection Abstraction & Mandatory Isolation
- Collections represent independent document groups governed by their own metadata and policies.
- **Strict Collection Isolation:** Queries scoped to a specific `collection_id` must NEVER retrieve, evaluate, rerank, or cite candidate passages from another collection. Cross-collection evidence leakage is a fatal architectural defect.
- If a query provides an invalid, unknown, or ambiguous `collection_id`, the system must fail closed (HTTP 404 or 422), never fall back to an unconstrained multi-collection search.

### 3.3. Directed Graph & Temporal Invariants
- For collections enforcing regulatory relationships (e.g. SEBI CSCRF), all relationship edges are strictly directed:
  $$\text{source\_document\_id (acting / newer)} \xrightarrow{\text{RELATIONSHIP}} \text{target\_document\_id (affected / older)}$$
- No self-loops are permitted (`source != target`).
- **Currency is never inferred from dates alone:** A newer publication date does not imply supersession without an explicit, verified relationship edge.
- **Provision-Level Scoping:** Partial amendments must specify `affected_provisions` so unaffected clauses retain base validity.
- Collections without supersession graphs (e.g. general technical manuals) use a `PassthroughPolicy` where all active documents are searchable without requiring amendment edges.

### 3.4. Grounding & Fail-Closed Abstention
- Every answer claim that requires factual support must be mechanically verified against retrieved context chunks.
- The system must abstain with a structured refusal (`refuse: true` or `disposition: CONTROLLED_ABSTENTION`) when evidence is missing, conflicting, out of domain, or superseded.
- For extractive responses, claims must match source chunk text with 100% character identity.
- External LLM synthesis (Gemini, Groq) must fail-closed to verified extractive excerpts or structured refusals if citation validation fails.

### 3.5. Persistence & Ingestion Safety
- **Zero-Cost Local Default:** Ingestion and retrieval must operate fully using local file paths and snapshot directories (`data/manifests/`, `data/processed/`, `data/indexes/snapshots/`) without requiring an active PostgreSQL or Redis instance.
- **Deterministic Chunking:** Chunks must be deterministically generated with persistent, reproducible chunk IDs (`{doc_id}_p{page_start}_{chunk_idx}`).
- **Immutable Versioning:** Ingested documents with changed PDF checksums must produce new immutable version records rather than mutating historical extractions in-place.
- **Safe Test Purging:** Destructive database operations (`drop_all`) must strictly require that the database name contains `test` (e.g. `sebi_rag_test`) or that `ALLOW_TEST_DATABASE_PURGE=1` is explicitly set in the execution environment.

---

## 4. Secrets, Privacy & Security
- Keep `.env` strictly excluded from Git tracking via `.gitignore`.
- Keep `.env.example` free of real API keys or sensitive connection strings.
- Never expose LLM provider API keys, database credentials, or admin keys in client bundles or API error responses.
- Do not log raw prompt text, sensitive user queries, or database credentials.
- Treat extracted PDF text as untrusted content; ensure retrieved context cannot override system instructions or alter tool execution.

---

## 5. Local Reproducibility & Shared Roadmap (Phases 0–4)
- All engineering tasks must align with the standardized shared roadmap:
  - **Phase 0:** Documentation, baseline reconciliation, and invariant definition.
  - **Phase 1:** Manifest-driven local ingestion (JSON manifests, SHA-256 deduplication, zero external DB dependencies).
  - **Phase 2:** Collection-aware retrieval, index partitioning, and mandatory isolation.
  - **Phase 3:** Generalized relationships, pluggable temporal policies, and conditional metadata persistence.
  - **Phase 4:** Multi-collection evaluation, reliability hardening, and deployment alignment.
- The 5-document SEBI CSCRF corpus serves as a permanent local regression baseline and must remain passing at 100%.

---

## 6. Milestone Completion Report Standard
At the conclusion of each engineering milestone, report:
1. Executive summary of actions performed.
2. List of modified and created files with rationale.
3. Exact commands run, exit codes, and test execution outputs.
4. Implementation status labels for all affected components.
5. Known limitations, unresolved questions, and next recommended steps.
