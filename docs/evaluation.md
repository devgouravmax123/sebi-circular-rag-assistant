# Evaluation Plan

## 1. Purpose
Measure whether retrieval and supersession handling improve answer quality and reduce stale answers. Evaluation must use real, traceable circulars and manually verified labels.

## 2. Dataset design and progression stages
Evaluation proceeds through four disciplined stages:
- **Stage A (Tiny Synthetic Fixtures):** Small, clearly synthetic fixtures for deterministic unit and integration tests.
- **Stage B (Micro-Slice):** 3–5 manually verified official SEBI circulars with an established amendment/rescission chain to prove the end-to-end vertical slice on real data.
- **Stage C (Corpus Expansion):** Expanding the curated corpus only after extraction, hybrid retrieval, citation verification, and status logic are validated.
- **Stage D (Golden Benchmark):** A verified `golden.jsonl` benchmark with isolated development and held-out test splits.

A `golden.jsonl` record may include:
```json
{
  "id": "q-001",
  "type": "direct_lookup",
  "split": "dev",
  "question": "Question grounded in a selected circular",
  "as_of": null,
  "answerable": true,
  "gold_docs": ["known-document-id"],
  "stale_docs": [],
  "key_points": ["required answer point"],
  "notes": "Reviewer notes and source references"
}
```
Use categories such as:
- direct lookup
- current-rule / temporal question
- amendment chain
- multi-document question
- unanswerable or ambiguous question

Do not invent circular IDs or claim relationship verification without examining official sources. Begin small and grow the dataset only as each example is independently verified.

## 3. Data quality rules and refusal rubric
- Every answerable question must have a traceable supporting source and expected key points.
- Every stale-document label must be justified by reviewed supersession evidence.
- Verify LLM-drafted questions and answers manually before including them.
- Split train/dev/test before threshold tuning.
- Do not use the held-out test set to select thresholds or prompts.

**Refusal evaluation rubric:**
A system must return `refuse: true` when:
1. No retrieved evidence meets the minimum relevance criteria.
2. Citations generated fail verification against retrieved context chunks.
3. Materially conflicting evidence cannot be reconciled.
4. The query is out-of-domain or unanswerable from the indexed corpus.
5. Graph uncertainty or missing effective dates prevent a reliable temporal determination.
*Note:* Upstream infrastructure failures (e.g. LLM API quota exhaustion, timeouts) must be measured as errors, not counted as correct domain refusals.

## 4. Retrieval metrics
- **Recall@k:** whether at least one relevant gold document/chunk appears among the top k results.
- **MRR:** reciprocal rank of the first relevant result, averaged over queries.
- Report the denominator, k, dataset split, and query count.

## 5. Answer and safety metrics
- **Answer correctness:** key points supported by sources and no material unsupported claims.
- **Citation accuracy:** citations resolve to the correct document/page and support the associated claim.
- **Refusal accuracy:** answerable cases are answered when evidence is sufficient; unanswerable cases are refused appropriately according to the rubric.
- **Stale-answer rate:** among relevant temporal questions, fraction of answers materially relying on a superseded rule as if current.
- **Latency:** report p50 and p95 with hardware/host, model, corpus size, and test conditions.
- **Provider usage:** if available, record request count and token/cost metadata without logging secrets.

Define each metric precisely before reporting. Manual evaluation should use a written rubric and preferably a second reviewer for a sample.

## 6. Ablation plan
Run the same questions through:
A. Dense/vector only  
B. BM25 only  
C. Hybrid retrieval with RRF  
D. Hybrid + reranker  
E. Hybrid + reranker + supersession logic  
F. Optional graph expansion

Record identical data splits and parameters. A stage may increase latency or reduce recall; report trade-offs honestly.

## 7. Failure analysis
For each failure, record question ID, expected behavior, actual behavior, category, root cause, and next action. Categories may include PDF extraction, retrieval miss, bad metadata, wrong graph edge, partial supersession, reranking error, hallucinated claim, invalid citation, incorrect refusal, prompt injection, provider error, or timeout.

## 8. Reporting template
- Dataset version and counts by category/split
- Corpus version and number of documents/pages/chunks
- Embedding and reranker model versions
- Retrieval parameters
- Results table for each system variant
- Confidence/limitations of conclusions
- Representative successes and failures
- Reproduction commands

Never fill the results table with illustrative values as if measured. Use `TBD` until experiments run.
