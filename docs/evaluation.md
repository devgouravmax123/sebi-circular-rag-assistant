# Evaluation & Benchmark Strategy

## 1. Objectives & Ground Truth Principles

The primary evaluation objective is measuring whether the **SEBI Regulatory Intelligence Platform** accurately retrieves relevant regulatory provisions, correctly resolves time-dependent supersession relationships, and prevents hallucinations or stale answers.

### Ground Truth Discipline
- **Zero Hallucinated Metrics:** Performance claims must derive from reproducible evaluation scripts executed against actual data.
- **Traceable Provenance:** Every query in the evaluation set maps to verified gold document IDs, chunk IDs, and specific expected provisions.
- **Permanent Regression Baseline:** The initial CSCRF 5-circular pilot corpus (`sebi_cscrf_2024_113`, `2025_045`, `2025_060`, `2025_096`, `2025_119`) and its 32 verified golden queries in [`evaluation/data/gold_evaluation_dataset.json`](file:///c:/Users/Raja/OneDrive/Desktop/sebi-rag-assistant/evaluation/data/gold_evaluation_dataset.json) serve as a permanent regression anchor across all future platform expansions.

---

## 2. Evaluation Datasets & Progression Stages

The platform progresses through four structured benchmark stages:

| Stage | Name | Corpus Scope | Query Volume | Objective | Implementation Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Stage A** | Synthetic Fixtures | Mock regulatory circulars | 12 unit tests | Validate data models, schema validation, and edge directionality. | `IMPLEMENTED AND TESTED` |
| **Stage B** | CSCRF Micro-Slice | 5 official SEBI CSCRF circulars | 32 verified gold queries (28 answerable ranking queries, 4 unanswerable refusal queries) | Prove hybrid retrieval (BM25 + BGE-Small dense vectors), provision relevance, temporal graph status, and refusal safety. | `IMPLEMENTED AND TESTED` |
| **Stage C** | Multi-Domain Expansion | CSCRF + LODR + ICDR | 100+ queries | Measure cross-domain retrieval precision, category routing, and provision filtering across SEBI regulations. | `PLANNED (Phase 8C)` |
| **Stage D** | Multi-Regulator Benchmark | SEBI + RBI official circulars | 250+ queries | Golden benchmark evaluating multi-regulator query isolation and cross-regulatory citation following operational validation. | `PLANNED (Phase 8G)` |

### Breakdown of Verified Stage B Dataset (32 Queries)
- **Answerable Ranking Queries (28 total):**
  - `clause_fact`: 5 queries
  - `technical_spec`: 5 queries
  - `deadline_exception`: 5 queries
  - `entity_applicability`: 5 queries
  - `cross_circular_amendment`: 4 queries
  - `paraphrase`: 4 queries
- **Unanswerable / Refusal Diagnostics (4 total):**
  - `unanswerable`: 4 queries testing controlled abstention under score thresholding ($S_{\max} < \tau$).

---

## 3. Core Evaluation Metrics

### 3.1. Retrieval Metrics
- **Recall@k:** Proportion of queries where at least one target gold chunk or document appears within the top $k$ retrieved results ($k=1, 3, 5$).
- **Mean Reciprocal Rank (MRR):** Average reciprocal rank of the first relevant gold passage:
  $$\text{MRR} = \frac{1}{|Q|} \sum_{i=1}^{|Q|} \frac{1}{\text{rank}_i}$$
- **Provision Precision:** Proportion of retrieved chunks directly matching the specific provision in question rather than broad, unamended parent frameworks.

### 3.2. Regulatory Currency & Supersession Metrics
- **Stale-Answer Rate:** The percentage of temporal compliance queries where a superseded or amended rule is presented as currently in force. Target: **0.0%**.
- **Historical Fidelity:** For queries with a specified historical `as_of` date, the percentage of responses correctly citing the rule effective on that historical date.

### 3.3. Grounding, Citation & Safety Metrics
- **Citation Precision:** Proportion of generated citations that accurately resolve to a valid document ID, PDF page number, and supporting text passage.
- **Refusal Precision & Recall:** Accuracy of domain refusals (`refuse: true`) against unanswerable, ambiguous, or out-of-domain test queries according to the refusal rubric.
- **Extraction Fidelity:** For extractive answers, percentage of text matching source chunk substrings with 100% character identity.

---

## 4. Refusal Evaluation Rubric

An evaluation query must produce `refuse: true` with a corresponding `refusal_reason` under any of the following conditions:

1. **Insufficient Evidence (`insufficient_evidence`):** The top retrieved candidates fall below the similarity threshold or do not discuss the queried topic.
2. **Citation Validation Failure (`citation_verification_failed`):** Generated claims lack direct supporting evidence in the retrieved context.
3. **Irreconcilable Conflict (`conflicting_evidence`):** Two retrieved provisions directly conflict without an established regulatory supersession edge.
4. **Out-of-Domain (`out_of_domain`):** The query asks about topics outside the indexed regulatory framework.
5. **Temporal Uncertainty (`temporal_uncertainty`):** Missing effective dates or ambiguous edge scopes prevent establishing the status on the requested `as_of` date.

*Important Note:* Network timeouts, provider quota exhaustion, or database errors must be classified as infrastructure errors (HTTP 5xx), never counted as successful domain refusals.

---

## 5. Automated Evaluation Runner

Evaluation and test suites are executed deterministically:

```powershell
# 1. Run retrieval evaluation against the CSCRF golden baseline (32 queries)
.\.venv\Scripts\python.exe evaluation/evaluate_retrieval.py

# 2. Run offline unit, parity, and grounding test suite (200 passing tests)
.\.venv\Scripts\python.exe -m pytest -q

# 3. Run dual-engine persistence parity suite
.\.venv\Scripts\python.exe -m pytest backend/tests/test_persistence_parity.py -v

# 4. Run live PostgreSQL + pgvector integration tests (requires disposable test DB)
$env:DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/sebi_rag_test"
.\.venv\Scripts\python.exe -m pytest backend/tests/test_postgres_live_integration.py -v

# 5. Full test suite with live database enabled (203 passing tests)
.\.venv\Scripts\python.exe -m pytest -v
```

*Note on Persistence Parity:* Parity testing (`test_persistence_parity.py`) verifies that query results, document attributes, chunk orders, and graph edge traversals execute with identical semantics across both the file-backed adapter and the PostgreSQL adapter.
