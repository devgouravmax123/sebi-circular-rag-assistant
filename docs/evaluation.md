# Citenza — Evaluation & Benchmark Methodology

> **Your documents. Verified answers.**

## 1. Overview & Evaluation Goals

Citenza's evaluation framework measures:
1. **Retrieval Precision & Recall:** Ensuring top retrieved passages contain the substantive answers to user queries across active collections (`general_synthetic`, `cisa_playbooks`, etc.).
2. **Citation Veracity & Verbatim Precision:** Ensuring that 100% of generated claims accurately reflect source text without fabrication.
3. **Controlled Abstention Reliability:** Confirming that out-of-scope, unanswerable, or adversarial queries trigger deterministic abstention without false acceptance.

---

## 2. Evaluation Datasets

The evaluation suite runs across active collections:
- **`cisa_playbooks` Evaluation Set:** 15 focused queries evaluating incident response procedures, containment guidelines, and forensic artifact preservation.
- **`general_synthetic` Evaluation Set:** 32 operational and infrastructure queries evaluating recovery point objectives (RPO), password requirements, lockout policies, rate limits, and snapshot schedules.

---

## 3. Running Automated Benchmarks

```bash
# Run Stage D benchmark runner
python -m pytest backend/tests/test_stage_d_benchmark_runner.py -v

# Run Stage D dataset adapter tests
python -m pytest backend/tests/test_stage_d_dataset_adapter.py -v

# Run citation validation benchmarks
python -m pytest backend/tests/test_collection_citation_validation.py -v
```
