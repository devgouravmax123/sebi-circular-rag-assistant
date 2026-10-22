# Evaluation & Benchmark Strategy

## 1. Objectives & Ground Truth Principles

The primary evaluation objective is measuring whether the platform accurately retrieves relevant regulatory provisions, correctly resolves time-dependent supersession relationships, and prevents hallucinations or stale answers.

### Ground Truth Discipline
- **Zero Hallucinated Metrics:** Performance claims must derive from reproducible evaluation scripts executed against actual data.
- **Traceable Provenance:** Every query in the evaluation set maps to verified gold document IDs, chunk IDs, and specific expected provisions.
- **Permanent Regression Baseline:** The initial CSCRF 5-circular pilot corpus (`sebi_cscrf_2024_113`, `2025_045`, `2025_060`, `2025_096`, `2025_119`) and its 32 verified golden queries in [`evaluation/data/gold_evaluation_dataset.json`](file:///c:/Users/Raja/OneDrive/Desktop/sebi-rag-assistant/evaluation/data/gold_evaluation_dataset.json) serve as a permanent regression anchor across all future platform expansions.

---

## 2. Evaluation Datasets & Progression Stages (Roadmap Aligned)

The benchmark strategy follows the standardized shared roadmap (Phases 0–4):

| Stage | Corpus Scope | Query Volume | Primary Objective | Verification Status |
| :--- | :--- | :--- | :--- | :--- |
| **Stage A: Unit & Parity Fixtures** | Mock synthetic circulars & isolated fixtures | 454 unit tests across 28 test modules | Validate schema parsing, chunk boundary logic, RRF fusion, safety gate rules, rate limiting, and dual-engine persistence parity. | `IMPLEMENTED AND VERIFIED` (445 passed, 9 skipped via pytest as of Oct 21, 2026). |
| **Stage B: CSCRF Gold Benchmark** | 5 official SEBI CSCRF circulars (168 chunks) | 32 verified golden queries (28 answerable, 4 unanswerable) | Evaluate hybrid retrieval (BM25 + BGE-Small dense vectors), specificity-aware reranking, temporal supersession resolution, and controlled abstention. | `IMPLEMENTED AND VERIFIED` (Permanent regression baseline). |
| **Stage C: Multi-Collection Isolation** | CSCRF + new independent collection (e.g. LODR / ICDR) | 50+ isolation queries | Measure cross-collection isolation, verifying zero evidence leakage between disjoint collections. | `PLANNED (Phase 2 & 4)` |
| **Stage D: Multi-Domain Regulatory Benchmark** | Broader SEBI circulars & secondary regulators | 200+ queries | Measure large-scale retrieval recall, pluggable policy accuracy, and long-tail temporal graph traversals. | `PLANNED (Phase 4)` |

### Breakdown of Verified Stage B Dataset (32 Queries)
- **Answerable Ranking Queries (28 total):**
  - `clause_fact`: 5 queries (direct factual verification against specific clauses).
  - `technical_spec`: 5 queries (technical controls like HSM, mobile app security, air-gapped backups).
  - `deadline_exception`: 5 queries (compliance timelines and entity-specific exceptions).
  - `entity_applicability`: 5 queries (Qualified RE, MII, Stock Broker categorizations).
  - `cross_circular_amendment`: 4 queries (evaluating multi-hop circular supersession).
  - `paraphrase`: 4 queries (testing semantic retrieval under lexical variation).
- **Unanswerable / Refusal Diagnostics (4 total):**
  - `unanswerable`: 4 queries testing controlled abstention under score thresholding and out-of-domain detection (e.g. NIST SP 800-53, HIPAA).

---

## 3. Core Evaluation Metrics

### 3.1. Retrieval & Ranking Metrics
- **Recall@k:** Proportion of queries where at least one target gold chunk or document appears within the top $k$ retrieved results ($k=1, 3, 5$).
- **Mean Reciprocal Rank (MRR):** Average reciprocal rank of the first relevant gold passage:
  $$\text{MRR} = \frac{1}{|Q|} \sum_{i=1}^{|Q|} \frac{1}{\text{rank}_i}$$
- **Provision Precision:** Proportion of retrieved chunks directly matching the specific provision in question rather than broad, unamended parent frameworks.

### 3.2. Regulatory Currency & Supersession Metrics
- **Stale-Answer Rate:** The percentage of temporal compliance queries where a superseded or amended rule is presented as currently in force. Target: **0.0%**.
- **Historical Fidelity:** For queries with a specified historical `as_of` date, the percentage of responses correctly citing the rule effective on that historical date.

### 3.3. Grounding, Citation & Safety Metrics
- **Citation Precision:** Proportion of generated citations that accurately resolve to a valid document ID, PDF page number, and supporting text passage.
- **Refusal Precision & Recall:** Accuracy of domain refusals (`refuse: true` / `disposition: CONTROLLED_ABSTENTION`) against unanswerable, ambiguous, or out-of-domain test queries according to the refusal rubric.
- **Extraction Fidelity:** For extractive answers, percentage of text matching source chunk substrings with 100% character identity.

### 3.4. Multi-Collection Isolation Metrics (Phase 2 & 4)
- **Negative Leakage Rate:** Percentage of candidate passages or citations in response to a query on collection $A$ that originate from collection $B$. Target: **0.0%**.
- **Cross-Collection Contamination:** Detection of foreign entity names or circular IDs in synthesized answers. Target: **0.0%**.

---

## 4. Refusal Evaluation Rubric (`IMPLEMENTED AND VERIFIED`)

An evaluation query must produce `refuse: true` with a corresponding `refusal_reason` under any of the following conditions:

1. **Insufficient Evidence (`insufficient_evidence`):** The top retrieved candidates fall below the similarity threshold or do not discuss the queried topic.
2. **Citation Validation Failure (`citation_verification_failed`):** Generated claims lack direct supporting evidence in the retrieved context.
3. **Irreconcilable Conflict (`conflicting_evidence`):** Two retrieved provisions directly conflict without an established regulatory supersession edge.
4. **Out-of-Domain (`out_of_domain`):** The query asks about topics outside the indexed collection.
5. **Temporal Uncertainty (`temporal_uncertainty`):** Missing effective dates or ambiguous edge scopes prevent establishing the status on the requested `as_of` date.

*Important Note:* Network timeouts, provider quota exhaustion, or database errors must be classified as infrastructure errors (HTTP 5xx), never counted as successful domain refusals.

---

## 5. Automated Evaluation Runner

Evaluation and test suites are executed deterministically in the local Python environment:

```powershell
# 1. Run retrieval evaluation against the CSCRF golden baseline (32 queries)
.\.venv\Scripts\python.exe evaluation/evaluate_retrieval.py

# 2. Run application-level safety gate and abstention evaluation
.\.venv\Scripts\python.exe evaluation/evaluate_application_abstention.py

# 3. Run full backend test suite (454 tests collected, 445 passing, 9 skipped as of Oct 21, 2026)
.\.venv\Scripts\python.exe -m pytest backend/tests/ -q

# 4. Run persistence parity suite (verifying file vs. PostgreSQL behavior)
.\.venv\Scripts\python.exe -m pytest backend/tests/test_persistence_parity.py -v
```

### Limitations of Mock Synthesis Evaluation
Evaluation runs using `MockSynthesisProvider` validate request routing, candidate ranking, safety gate filtering, and citation verification mechanics. They do **not** validate that real generative LLMs (e.g. Groq, Gemini) will always produce natural, stylistically polished text. Real LLM evaluation requires live provider credentials and is segregated to prevent accidental token spend or non-deterministic CI failures.
