"""
Deterministic lexical retrieval evaluation benchmark.

Measures Recall@k and Mean Reciprocal Rank (MRR) over verified ground-truth queries.
"""
import sys
from pathlib import Path
from typing import List, Dict, Any, Set
from dataclasses import dataclass

# Ensure backend root is on sys.path for direct script execution and IDE resolution
backend_path = Path(__file__).resolve().parent.parent / "backend"
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))

from app.retrieval.service import RetrievalService


@dataclass
class EvalQuery:
    query_id: str
    query_text: str
    relevant_doc_ids: Set[str]
    description: str


# 5 Curated Ground-Truth Queries Grounded Strictly in SEBI CSCRF Corpus Provisions
BENCHMARK_QUERIES: List[EvalQuery] = [
    EvalQuery(
        query_id="q1_iso27001_relaxation",
        query_text="ISO 27001 certification mandatory or recommended for Qualified REs",
        relevant_doc_ids={"sebi_cscrf_2025_119", "sebi_cscrf_2024_113"},
        description="CIR/2025/119 relaxed ISO 27001 from mandatory to recommended for Qualified REs.",
    ),
    EvalQuery(
        query_id="q2_compliance_deadline_august",
        query_text="extension compliance timelines August 31 2025 except MIIs KRAs QRTAs",
        relevant_doc_ids={"sebi_cscrf_2025_096"},
        description="CIR/2025/096 extended timeline by 2 months till August 31, 2025.",
    ),
    EvalQuery(
        query_id="q3_cloud_hsm_mandate",
        query_text="Framework adoption cloud services hardware security module HSM mandatory MIIs",
        relevant_doc_ids={"sebi_cscrf_2025_060", "sebi_cscrf_2024_113"},
        description="CIR/2025/060 and Master Annexure-J specify HSM for MIIs and alternatives for smaller REs.",
    ),
    EvalQuery(
        query_id="q4_portfolio_managers_aum",
        query_text="Portfolio Managers categorization Asset Under Management AUM 10000 crores",
        relevant_doc_ids={"sebi_cscrf_2025_119", "sebi_cscrf_2025_060"},
        description="CIR/2025/119 Table 1 revised Portfolio Managers AUM thresholds.",
    ),
    EvalQuery(
        query_id="q5_rto_two_hours_iosco",
        query_text="Recovery Time Objective RTO two hours IOSCO resumption critical operations 15 minutes RPO",
        relevant_doc_ids={"sebi_cscrf_2025_119", "sebi_cscrf_2024_113"},
        description="CIR/2025/119 Clause 6.10 and Master RC.RP.S2 specify RTO 2 hours and RPO 15 minutes.",
    ),
]


def evaluate_bm25_retrieval(top_k: int = 5) -> Dict[str, Any]:
    """
    Run baseline evaluation on the processed SEBI CSCRF corpus.
    Calculates Recall@k and MRR (Mean Reciprocal Rank).
    """
    service = RetrievalService(processed_dir="data/processed")
    service.load_and_index_corpus()

    reciprocal_ranks: List[float] = []
    recalls: List[float] = []
    query_results: List[Dict[str, Any]] = []

    for q in BENCHMARK_QUERIES:
        resp = service.search(query=q.query_text, top_k=top_k)
        retrieved_docs = [r.document_id for r in resp.results]

        # Check first relevant hit rank for MRR
        rr = 0.0
        for rank, doc_id in enumerate(retrieved_docs, start=1):
            if doc_id in q.relevant_doc_ids:
                rr = 1.0 / rank
                break
        reciprocal_ranks.append(rr)

        # Check recall: fraction of relevant doc IDs retrieved in top_k
        hits = len(set(retrieved_docs).intersection(q.relevant_doc_ids))
        recall = hits / len(q.relevant_doc_ids) if q.relevant_doc_ids else 0.0
        recalls.append(recall)

        query_results.append({
            "query_id": q.query_id,
            "query_text": q.query_text,
            "expected_docs": list(q.relevant_doc_ids),
            "retrieved_docs": retrieved_docs,
            "reciprocal_rank": rr,
            "recall": recall,
        })

    mrr = sum(reciprocal_ranks) / len(reciprocal_ranks) if reciprocal_ranks else 0.0
    mean_recall = sum(recalls) / len(recalls) if recalls else 0.0

    return {
        "num_queries": len(BENCHMARK_QUERIES),
        "top_k": top_k,
        "mrr": round(mrr, 4),
        "mean_recall": round(mean_recall, 4),
        "query_metrics": query_results,
    }


if __name__ == "__main__":
    metrics = evaluate_bm25_retrieval(top_k=5)
    print(f"BM25 Baseline Evaluation Results (top_k={metrics['top_k']}):")
    print(f"- MRR: {metrics['mrr']}")
    print(f"- Mean Recall@{metrics['top_k']}: {metrics['mean_recall']}")
    for q in metrics["query_metrics"]:
        print(f"  [{q['query_id']}] RR={q['reciprocal_rank']}, Recall={q['recall']} -> Retrieved: {q['retrieved_docs']}")
