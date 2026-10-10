# Implementation Tasks

## Working agreement
- [ ] Read all project documentation before coding.
- [ ] Keep required spend at ₹0; do not enable paid billing or select mandatory paid services.
- [ ] Do not expose API keys in frontend code, Git history, logs, screenshots, or commits.
- [ ] Do not build the entire app in one agent run.
- [ ] Complete one milestone at a time; report changed files, commands run, test results, and blockers.
- [ ] Do not invent test results, performance numbers, citations, or data provenance.
- [ ] Ask before major dependency, architecture, hosting, or data-source changes.

## Phase 0 — Environment and repository
- [x] Inspect the current folder and report existing files before modifying anything.
- [x] Check Python, Node.js, npm, Git versions.
- [x] Confirm the project directory and create the planned folder structure.
- [x] Create `.gitignore` and `.env.example`.
- [x] Create Python virtual environment; document activation commands for Windows PowerShell.
- [x] Record exact versions actually installed.
**Acceptance:** clean Git repository, no secrets, environment report recorded, no app code generated prematurely.

## Phase 1 — Minimal vertical slice
- [x] Create minimal FastAPI app with `/health`.
- [x] Add a test for `/health`.
- [x] Create React + TypeScript + Vite frontend.
- [x] Add a minimal question form and answer panel with loading/error states.
- [x] Connect frontend to backend using environment-based API URL.
- [x] Document local startup commands.
**Acceptance:** UI can call `/health`; backend tests pass; no LLM key required.

## Phase 2 — Data model and tiny fixture corpus (Stage A)
- [x] Define schemas for circular metadata, chunks, graph edges (directed `Newer -> RELATIONSHIP -> Older`), citations, and answer responses.
- [x] Add provision-level scoping (`affected_provisions`) to edge schemas.
- [x] Add a few clearly labelled synthetic fixture records for unit testing only.
- [x] Preserve source URL, document ID, date, page number, and extraction/OCR metadata.
- [x] Add data validation tests.
**Acceptance:** fixtures are explicitly marked as synthetic test data and never presented as official corpus results.

## Phase 3 — Ingestion prototype and micro-slice (Stage B)
- [x] Choose 3–5 official SEBI circulars with verified amendment/rescission relationships.
- [x] Store source URLs, retrieval date, checksums, and metadata in a manifest.
- [x] Parse PDF text and page boundaries.
- [ ] Extract tables separately where useful.
- [x] Mark OCR-derived text and report extraction quality.
- [x] Add a manual review workflow for low-quality or ambiguous documents.
**Acceptance:** every ingested document has provenance; extraction output can be traced to source page.

## Phase 4 — Chunking and retrieval
- [ ] Implement deterministic chunking with page, provision, and document metadata.
- [ ] Perform local resource check before initializing candidate embedding model (`sentence-transformers/all-MiniLM-L6-v2`).
- [ ] Build BM25 index.
- [ ] Build normalized dense-vector index with FAISS.
- [ ] Implement RRF fusion.
- [ ] Add retrieval tests using Stage B small labelled set.
- [ ] Record model and index versions so indexes can be reproduced.
**Acceptance:** retrieval returns valid source IDs and page references; basic Recall@k/MRR script runs.

## Phase 5 — Supersession graph
- [ ] Define allowed edge types: `AMENDS`, `RESCINDS`, `REFERENCES` with direction `Newer/acting -> Older/affected`.
- [ ] Store edge evidence sentence, page, source document, confidence, and whole vs. partial provision scope.
- [ ] Manually label a validation set of edges for Stage B circulars.
- [ ] Implement status evaluation `status_as_of(document_id, date, provision=None)` preserving unaffected provisions for historical/current queries.
- [ ] Add tests for amendments, rescissions, partial supersession, cycles, missing dates, and unknown relationships.
- [ ] Prefer evidence-backed unknown status over unsupported certainty.
**Acceptance:** each status decision is explainable from stored evidence; graph output is covered by tests.

## Phase 6 — Reranking and answer generation
- [ ] Add a reranker only after baseline retrieval is measured.
- [ ] Handle reranker scores correctly; do not apply arbitrary multipliers to raw logits.
- [ ] Define provider-neutral LLM adapter and structured output schema.
- [ ] Add Gemini adapter if a suitable free quota is available.
- [ ] Add optional Groq adapter only if its current terms and quotas are suitable.
- [ ] Keep API keys on the backend in environment variables.
- [ ] Validate citation IDs and source/page metadata in code.
- [ ] Abstain when evidence or verified citations are insufficient.
- [ ] Add prompt-injection tests for hostile text inside documents.
**Acceptance:** answers cite only retrieved, known source chunks; unanswerable cases can be refused.

## Phase 7 — API and UI
- [ ] Implement `/ask`, `/health`, and `/feedback`.
- [ ] Validate question length (initial maximum 500 characters) and `as_of` date.
- [ ] Return answer, refusal state, citations, status notes, and latency metadata.
- [ ] Add source cards with circular number, date, page, and official URL.
- [ ] Add as-of date control, loading/error/refusal states, and feedback controls.
- [ ] Add safe logging and basic rate limiting.
**Acceptance:** UI handles success, refusal, validation error, timeout, and provider quota failure.

## Phase 8 — Evaluation and ablations
- [ ] Create a manually verified golden dataset.
- [ ] Split examples into development and held-out test sets before tuning thresholds.
- [ ] Measure Recall@5, MRR, answer correctness, citation accuracy, refusal accuracy, and stale-answer rate.
- [ ] Compare vector-only, BM25-only, hybrid RRF, reranking, and supersession-aware variants.
- [ ] Record sample counts, failures, limitations, and reproducible commands.
- [ ] Do not tune thresholds against the held-out test set.
**Acceptance:** metrics are computed from actual runs and no numbers are claimed without evidence.

## Phase 9 — Deployment and hardening
- [ ] Add Docker support only after local functionality is stable.
- [ ] Add GitHub Actions for automated tests.
- [ ] Verify free hosting and provider terms immediately before deployment.
- [ ] Ensure local setup remains supported if a free host sleeps or becomes unavailable.
- [ ] Keep secrets in deployment environment settings.
- [ ] Add data-use/privacy notes for the selected LLM provider.
- [ ] Test rate limits, CORS, errors, logging, and document prompt injection.
**Acceptance:** no mandatory paid dependency; deployment instructions are reproducible and honest about free-tier limits.

## Phase 10 — Portfolio polish
- [ ] Complete README architecture and setup sections.
- [ ] Add a clear architecture diagram.
- [ ] Capture a short demo showing citations and an outdated-rule edge case.
- [ ] Write at least 10 evidence-based failure analyses.
- [ ] Report limitations and actual measured metrics.
- [ ] Write resume bullets only after metrics are measured.
**Acceptance:** another developer can run the project and understand the evidence behind the claims.
