"""
Unified Retrieval Evaluator for SEBI CSCRF Regulatory Q&A/Intelligence Assistant.

Evaluates lexical (BM25), dense, and hybrid (RRF) retrieval systems against the gold evaluation dataset.
Implements:
1. Primary chunk-ranked document metrics (K=10 raw chunks budget, first-occurrence deduplication, cutoffs k in {1, 3, 5}).
2. Diagnostic aggregated document metrics (max chunk score per document, doc_id tie-breaking, cutoffs k in {1, 3, 5}).
3. Exact chunk metrics (cutoffs k in {1, 3, 5}).
4. Unanswerable query abstention diagnostics (S_max < threshold).
5. Head-to-head pairwise comparison (Wins / Ties / Losses) and latency profiling.
"""
import argparse
import json
import sys
import time
from pathlib import Path
from typing import List, Dict, Any, Optional, Set, Tuple
from dataclasses import dataclass, field

# Ensure backend directory is in sys.path
backend_path = Path(__file__).resolve().parent.parent / "backend"
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))

from app.schemas.evaluation import (
    GoldEvaluationDataset,
    GoldQueryRecord,
    validate_gold_dataset_against_corpus,
)
from app.schemas.retrieval import RetrievalResponse, RetrievalResult
from app.retrieval.service import RetrievalService
from app.retrieval.dense_embedder import DenseEmbedder
from app.retrieval.rrf import reciprocal_rank_fusion


@dataclass
class QueryDocumentMetricResult:
    query_id: str
    gold_doc_ids: Set[str]
    retrieved_raw_chunk_ids: List[str]
    retrieved_unique_docs: List[str]
    reciprocal_rank: float
    recall_at_k: Dict[int, float]


@dataclass
class QueryChunkMetricResult:
    query_id: str
    gold_chunk_ids: Set[str]
    retrieved_chunk_ids: List[str]
    reciprocal_rank: float
    recall_at_k: Dict[int, float]


@dataclass
class MetricSummary:
    mrr: float
    mean_recall_at_k: Dict[int, float]
    num_eligible_queries: int
    total_queries: int


@dataclass
class AbstentionSummary:
    threshold: float
    num_unanswerable_eligible: int
    abstentions_on_unanswerable: int
    abstention_rate: float
    num_answerable_eligible: int
    false_abstentions_on_answerable: int
    false_abstention_rate: float
    unanswerable_scores: List[float]
    answerable_scores: List[float]


def compute_chunk_ranked_document_metrics(
    query_record: GoldQueryRecord,
    retrieved_results: List[RetrievalResult],
    cutoffs: List[int] = [1, 3, 5],
) -> QueryDocumentMetricResult:
    """
    Primary chunk-ranked document metric:
    - Chunks are ranked 1..K in retrieved order.
    - Chunks mapped to doc IDs and deduplicated by FIRST OCCURRENCE.
    - Evaluated at unique document cutoffs k in {1, 3, 5}.
    """
    gold_docs = set(query_record.document_labels.gold_document_ids)
    raw_chunk_ids = [r.chunk_id for r in retrieved_results]

    # First occurrence deduplication
    unique_docs: List[str] = []
    seen: Set[str] = set()
    for r in retrieved_results:
        if r.document_id not in seen:
            seen.add(r.document_id)
            unique_docs.append(r.document_id)

    # Calculate Reciprocal Rank on unique documents
    rr = 0.0
    for rank_idx, doc_id in enumerate(unique_docs, start=1):
        if doc_id in gold_docs:
            rr = 1.0 / rank_idx
            break

    # Calculate Recall@k on unique documents
    recall_at_k: Dict[int, float] = {}
    total_gold = len(gold_docs)
    for k in cutoffs:
        if total_gold == 0:
            recall_at_k[k] = 0.0
        else:
            top_k_docs = set(unique_docs[:k])
            hits = len(top_k_docs.intersection(gold_docs))
            recall_at_k[k] = hits / total_gold

    return QueryDocumentMetricResult(
        query_id=query_record.query_id,
        gold_doc_ids=gold_docs,
        retrieved_raw_chunk_ids=raw_chunk_ids,
        retrieved_unique_docs=unique_docs,
        reciprocal_rank=rr,
        recall_at_k=recall_at_k,
    )


def compute_aggregated_document_metrics(
    query_record: GoldQueryRecord,
    retrieved_results: List[RetrievalResult],
    cutoffs: List[int] = [1, 3, 5],
) -> QueryDocumentMetricResult:
    """
    Diagnostic aggregated document metric:
    - Aggregate score by max chunk score per document.
    - Sort descending by max score, breaking ties by document_id ascending.
    - Evaluate MRR and Recall@k on this sorted unique document list.
    """
    gold_docs = set(query_record.document_labels.gold_document_ids)
    raw_chunk_ids = [r.chunk_id for r in retrieved_results]

    doc_max_scores: Dict[str, float] = {}
    for r in retrieved_results:
        doc_id = r.document_id
        if doc_id not in doc_max_scores or r.score > doc_max_scores[doc_id]:
            doc_max_scores[doc_id] = r.score

    # Sort descending by score, ascending by doc_id
    sorted_docs = sorted(
        doc_max_scores.keys(),
        key=lambda d: (-doc_max_scores[d], d),
    )

    rr = 0.0
    for rank_idx, doc_id in enumerate(sorted_docs, start=1):
        if doc_id in gold_docs:
            rr = 1.0 / rank_idx
            break

    recall_at_k: Dict[int, float] = {}
    total_gold = len(gold_docs)
    for k in cutoffs:
        if total_gold == 0:
            recall_at_k[k] = 0.0
        else:
            top_k_docs = set(sorted_docs[:k])
            hits = len(top_k_docs.intersection(gold_docs))
            recall_at_k[k] = hits / total_gold

    return QueryDocumentMetricResult(
        query_id=query_record.query_id,
        gold_doc_ids=gold_docs,
        retrieved_raw_chunk_ids=raw_chunk_ids,
        retrieved_unique_docs=sorted_docs,
        reciprocal_rank=rr,
        recall_at_k=recall_at_k,
    )


def compute_chunk_metrics(
    query_record: GoldQueryRecord,
    retrieved_results: List[RetrievalResult],
    cutoffs: List[int] = [1, 3, 5],
) -> QueryChunkMetricResult:
    """
    Exact chunk-level metric:
    - Ranks raw chunk items directly.
    - Evaluated against gold_chunk_ids at raw ranks k in {1, 3, 5}.
    """
    gold_chunks = set(query_record.chunk_labels.gold_chunk_ids)
    retrieved_chunk_ids = [r.chunk_id for r in retrieved_results]

    rr = 0.0
    for rank_idx, cid in enumerate(retrieved_chunk_ids, start=1):
        if cid in gold_chunks:
            rr = 1.0 / rank_idx
            break

    recall_at_k: Dict[int, float] = {}
    total_gold = len(gold_chunks)
    for k in cutoffs:
        if total_gold == 0:
            recall_at_k[k] = 0.0
        else:
            top_k_chunks = set(retrieved_chunk_ids[:k])
            hits = len(top_k_chunks.intersection(gold_chunks))
            recall_at_k[k] = hits / total_gold

    return QueryChunkMetricResult(
        query_id=query_record.query_id,
        gold_chunk_ids=gold_chunks,
        retrieved_chunk_ids=retrieved_chunk_ids,
        reciprocal_rank=rr,
        recall_at_k=recall_at_k,
    )


def aggregate_metric_results(
    results: List[Any],
    eligible_count: int,
    total_dataset_queries: int,
    cutoffs: List[int] = [1, 3, 5],
) -> MetricSummary:
    if not results:
        return MetricSummary(
            mrr=0.0,
            mean_recall_at_k={k: 0.0 for k in cutoffs},
            num_eligible_queries=eligible_count,
            total_queries=total_dataset_queries,
        )

    mrr = sum(r.reciprocal_rank for r in results) / len(results)
    mean_recalls = {
        k: sum(r.recall_at_k[k] for r in results) / len(results)
        for k in cutoffs
    }

    return MetricSummary(
        mrr=mrr,
        mean_recall_at_k=mean_recalls,
        num_eligible_queries=eligible_count,
        total_queries=total_dataset_queries,
    )


def compute_abstention_diagnostics(
    dataset: GoldEvaluationDataset,
    all_query_results: Dict[str, List[RetrievalResult]],
    threshold: float,
) -> AbstentionSummary:
    """
    Calculates abstention rate on unanswerable queries and false-abstention rate on answerable queries.
    Baseline rule: Abstain if max(score) < threshold.
    """
    unanswerable_scores: List[float] = []
    answerable_scores: List[float] = []

    abstentions_on_unanswerable = 0
    num_unanswerable_eligible = 0

    false_abstentions_on_answerable = 0
    num_answerable_eligible = 0

    for q in dataset.queries:
        results = all_query_results.get(q.query_id, [])
        max_score = max((r.score for r in results), default=0.0)

        if q.is_unanswerable_eval_eligible:
            num_unanswerable_eligible += 1
            unanswerable_scores.append(max_score)
            if max_score < threshold:
                abstentions_on_unanswerable += 1

        elif q.is_document_eval_eligible:
            num_answerable_eligible += 1
            answerable_scores.append(max_score)
            if max_score < threshold:
                false_abstentions_on_answerable += 1

    abstention_rate = (
        abstentions_on_unanswerable / num_unanswerable_eligible
        if num_unanswerable_eligible > 0
        else 0.0
    )
    false_abstention_rate = (
        false_abstentions_on_answerable / num_answerable_eligible
        if num_answerable_eligible > 0
        else 0.0
    )

    return AbstentionSummary(
        threshold=threshold,
        num_unanswerable_eligible=num_unanswerable_eligible,
        abstentions_on_unanswerable=abstentions_on_unanswerable,
        abstention_rate=abstention_rate,
        num_answerable_eligible=num_answerable_eligible,
        false_abstentions_on_answerable=false_abstentions_on_answerable,
        false_abstention_rate=false_abstention_rate,
        unanswerable_scores=unanswerable_scores,
        answerable_scores=answerable_scores,
    )


class UnifiedEvaluator:
    """
    Harness evaluating BM25, Dense, and Hybrid (RRF) retrievers.
    """
    def __init__(
        self,
        dataset_path: str | Path = "evaluation/data/gold_evaluation_dataset.json",
        processed_dir: str | Path = "data/processed",
        indexes_dir: str | Path = "data/indexes",
        raw_budget_K: int = 10,
    ):
        self.dataset_path = Path(dataset_path)
        self.processed_dir = Path(processed_dir)
        self.indexes_dir = Path(indexes_dir)
        self.raw_budget_K = raw_budget_K
        self.dataset: Optional[GoldEvaluationDataset] = None

    def load_and_validate_dataset(self) -> GoldEvaluationDataset:
        if not self.dataset_path.exists():
            raise FileNotFoundError(f"Gold evaluation dataset not found: {self.dataset_path}")

        with open(self.dataset_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        dataset = GoldEvaluationDataset(**data)
        errors = validate_gold_dataset_against_corpus(dataset, processed_dir=self.processed_dir)
        if errors:
            raise ValueError(f"Dataset integrity validation failed ({len(errors)} errors):\n" + "\n".join(errors))

        self.dataset = dataset
        return dataset

    def evaluate_retriever(
        self,
        retriever_name: str,
        search_fn: Any,
        abstention_threshold: float,
    ) -> Dict[str, Any]:
        if not self.dataset:
            self.load_and_validate_dataset()

        assert self.dataset is not None

        all_query_results: Dict[str, List[RetrievalResult]] = {}
        primary_doc_results: List[QueryDocumentMetricResult] = []
        diag_doc_results: List[QueryDocumentMetricResult] = []
        chunk_results: List[QueryChunkMetricResult] = []

        doc_eligible_count = 0
        chunk_eligible_count = 0
        unanswerable_count = 0
        pending_review_count = 0

        latencies_ms: List[float] = []

        for q in self.dataset.queries:
            t0 = time.perf_counter()
            resp: RetrievalResponse = search_fn(q.query_text, top_k=self.raw_budget_K)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            latencies_ms.append(elapsed_ms)

            all_query_results[q.query_id] = resp.results

            if q.is_document_eval_eligible:
                doc_eligible_count += 1
                primary_doc = compute_chunk_ranked_document_metrics(q, resp.results)
                diag_doc = compute_aggregated_document_metrics(q, resp.results)
                primary_doc_results.append(primary_doc)
                diag_doc_results.append(diag_doc)

            if q.is_chunk_eval_eligible:
                chunk_eligible_count += 1
                ch_res = compute_chunk_metrics(q, resp.results)
                chunk_results.append(ch_res)

            if q.is_unanswerable_eval_eligible:
                unanswerable_count += 1

            if q.annotation_status.document_level.value == "pending_review":
                pending_review_count += 1

        primary_doc_summary = aggregate_metric_results(
            primary_doc_results,
            eligible_count=doc_eligible_count,
            total_dataset_queries=len(self.dataset.queries),
        )
        diag_doc_summary = aggregate_metric_results(
            diag_doc_results,
            eligible_count=doc_eligible_count,
            total_dataset_queries=len(self.dataset.queries),
        )
        chunk_summary = aggregate_metric_results(
            chunk_results,
            eligible_count=chunk_eligible_count,
            total_dataset_queries=len(self.dataset.queries),
        )
        abstention_summary = compute_abstention_diagnostics(
            self.dataset,
            all_query_results,
            threshold=abstention_threshold,
        )

        avg_latency = sum(latencies_ms) / len(latencies_ms) if latencies_ms else 0.0

        return {
            "retriever_name": retriever_name,
            "raw_budget_K": self.raw_budget_K,
            "avg_latency_ms": avg_latency,
            "counts": {
                "total_queries": len(self.dataset.queries),
                "doc_eligible": doc_eligible_count,
                "chunk_eligible": chunk_eligible_count,
                "unanswerable_eligible": unanswerable_count,
                "pending_review": pending_review_count,
            },
            "primary_document_metrics": primary_doc_summary,
            "diagnostic_aggregated_document_metrics": diag_doc_summary,
            "chunk_metrics": chunk_summary,
            "abstention_diagnostics": abstention_summary,
            "query_results": all_query_results,
            "per_query_chunk_results": {r.query_id: r for r in chunk_results},
            "per_query_doc_results": {r.query_id: r for r in primary_doc_results},
        }


def print_evaluation_report(results: List[Dict[str, Any]]):
    print("\n" + "=" * 95)
    print("                 SEBI CSCRF UNIFIED RETRIEVAL EVALUATION REPORT")
    print("=" * 95)

    counts = results[0]["counts"]
    print(f"\nDataset Overview:")
    print(f"  * Total Queries:                {counts['total_queries']}")
    print(f"  * Document-level Scored:        {counts['doc_eligible']}")
    print(f"  * Chunk-level Scored:           {counts['chunk_eligible']}")
    print(f"  * Unanswerable Diagnostic:      {counts['unanswerable_eligible']}")
    print(f"  * Pending Review (Excluded):    {counts['pending_review']}")
    print(f"  * Raw Chunk Budget (K):         {results[0]['raw_budget_K']}")

    # Table 1: Primary Document Metrics
    print("\n" + "-" * 95)
    print("A. Primary Chunk-Ranked Document Metrics (First-occurrence Deduplication, K=10)")
    print("-" * 95)
    header = f"{'System':<20} | {'Doc MRR':<10} | {'Recall@1':<10} | {'Recall@3':<10} | {'Recall@5':<10} | {'Latency (ms)':<12} | {'Eligible'}"
    print(header)
    print("-" * 95)
    for r in results:
        m = r["primary_document_metrics"]
        row = (
            f"{r['retriever_name']:<20} | "
            f"{m.mrr:<10.4f} | "
            f"{m.mean_recall_at_k[1]:<10.4f} | "
            f"{m.mean_recall_at_k[3]:<10.4f} | "
            f"{m.mean_recall_at_k[5]:<10.4f} | "
            f"{r['avg_latency_ms']:<12.2f} | "
            f"{m.num_eligible_queries}/{m.total_queries}"
        )
        print(row)

    # Table 2: Diagnostic Aggregated Document Metrics
    print("\n" + "-" * 95)
    print("B. Diagnostic Aggregated Document Metrics (Max Chunk Score per Document, K=10)")
    print("-" * 95)
    print(header)
    print("-" * 95)
    for r in results:
        m = r["diagnostic_aggregated_document_metrics"]
        row = (
            f"{r['retriever_name']:<20} | "
            f"{m.mrr:<10.4f} | "
            f"{m.mean_recall_at_k[1]:<10.4f} | "
            f"{m.mean_recall_at_k[3]:<10.4f} | "
            f"{m.mean_recall_at_k[5]:<10.4f} | "
            f"{r['avg_latency_ms']:<12.2f} | "
            f"{m.num_eligible_queries}/{m.total_queries}"
        )
        print(row)

    # Table 3: Exact Chunk Metrics
    print("\n" + "-" * 95)
    print("C. Exact-Chunk Retrieval Metrics (Raw Rank Cutoffs k in {1, 3, 5})")
    print("-" * 95)
    header_chunk = f"{'System':<20} | {'Chunk MRR':<10} | {'Recall@1':<10} | {'Recall@3':<10} | {'Recall@5':<10} | {'Latency (ms)':<12} | {'Eligible'}"
    print(header_chunk)
    print("-" * 95)
    for r in results:
        m = r["chunk_metrics"]
        row = (
            f"{r['retriever_name']:<20} | "
            f"{m.mrr:<10.4f} | "
            f"{m.mean_recall_at_k[1]:<10.4f} | "
            f"{m.mean_recall_at_k[3]:<10.4f} | "
            f"{m.mean_recall_at_k[5]:<10.4f} | "
            f"{r['avg_latency_ms']:<12.2f} | "
            f"{m.num_eligible_queries}/{m.total_queries}"
        )
        print(row)

    # Table 4: Abstention Diagnostics
    print("\n" + "-" * 95)
    print("D. Unanswerable Query Abstention Diagnostics (Decision Rule: S_max < threshold)")
    print("-" * 95)
    header_abs = f"{'System':<20} | {'Threshold':<10} | {'Abstention Rate (True)':<25} | {'False Abstention Rate':<22}"
    print(header_abs)
    print("-" * 95)
    for r in results:
        a = r["abstention_diagnostics"]
        row = (
            f"{r['retriever_name']:<20} | "
            f"{a.threshold:<10.4f} | "
            f"{a.abstention_rate*100:>5.1f}% ({a.abstentions_on_unanswerable}/{a.num_unanswerable_eligible}){'':<10} | "
            f"{a.false_abstention_rate*100:>5.1f}% ({a.false_abstentions_on_answerable}/{a.num_answerable_eligible})"
        )
        print(row)

    # Table 5: Pairwise Head-to-Head Comparison (RRF vs BM25 & RRF vs Dense)
    if len(results) >= 3:
        bm25_res = results[0]
        dense_res = results[1]
        rrf_res = results[2]

        print("\n" + "-" * 95)
        print("E. Head-to-Head Pairwise Chunk MRR Comparison across 28 Answerable Queries")
        print("-" * 95)

        def compare_pair(res_a, res_b, name_a, name_b):
            wins_a = 0
            wins_b = 0
            ties = 0
            for qid in res_a["per_query_chunk_results"]:
                rr_a = res_a["per_query_chunk_results"][qid].reciprocal_rank
                rr_b = res_b["per_query_chunk_results"][qid].reciprocal_rank
                if rr_a > rr_b + 1e-6:
                    wins_a += 1
                elif rr_b > rr_a + 1e-6:
                    wins_b += 1
                else:
                    ties += 1
            print(f"  * {name_a} vs {name_b}: {name_a} Wins = {wins_a}, {name_b} Wins = {wins_b}, Ties = {ties}")

        compare_pair(rrf_res, bm25_res, "RRF Hybrid", "BM25")
        compare_pair(rrf_res, dense_res, "RRF Hybrid", "Dense")

    print("\n" + "=" * 95)
    print("Notice: Small pilot benchmark (32 queries across 5 documents). Results demonstrate")
    print("relative retrieval alignment on verified provisions, not universal regulatory completeness.")
    print("=" * 95 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Unified Retrieval Evaluator for SEBI CSCRF")
    parser.add_argument("--dataset", type=str, default="evaluation/data/gold_evaluation_dataset.json", help="Path to gold dataset")
    parser.add_argument("--processed_dir", type=str, default="data/processed", help="Path to processed corpus")
    parser.add_argument("--indexes_dir", type=str, default="data/indexes", help="Path to indexes")
    parser.add_argument("--k", type=int, default=10, help="Raw chunk retrieval budget K")
    parser.add_argument("--rrf_k", type=int, default=60, help="RRF smoothing constant k")
    parser.add_argument("--candidate_k", type=int, default=50, help="Candidate pool size for RRF component retrieval")
    parser.add_argument("--bm25_threshold", type=float, default=5.0, help="BM25 max score abstention threshold")
    parser.add_argument("--dense_threshold", type=float, default=0.55, help="Dense max cosine score abstention threshold")
    parser.add_argument("--rrf_threshold", type=float, default=0.025, help="RRF max fused score abstention threshold")
    args = parser.parse_args()

    evaluator = UnifiedEvaluator(
        dataset_path=args.dataset,
        processed_dir=args.processed_dir,
        indexes_dir=args.indexes_dir,
        raw_budget_K=args.k,
    )

    print("Loading and validating gold evaluation dataset...")
    try:
        dataset = evaluator.load_and_validate_dataset()
        print(f"Dataset successfully validated! Total queries: {len(dataset.queries)}")
    except Exception as e:
        print(f"FATAL: Dataset validation failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Initialize Service
    print("Initializing RetrievalService...")
    dense_embedder = DenseEmbedder(model_name="BAAI/bge-small-en-v1.5")
    service = RetrievalService(
        processed_dir=args.processed_dir,
        indexes_dir=args.indexes_dir,
        dense_embedder=dense_embedder,
    )
    service.load_and_index_corpus()
    service.load_and_index_dense(auto_rebuild=True)

    results = []

    # 1. Evaluate BM25
    print("\nEvaluating Lexical BM25 retrieval...")
    bm25_res = evaluator.evaluate_retriever(
        retriever_name="BM25 Lexical",
        search_fn=lambda q, top_k: service.search(query=q, top_k=top_k),
        abstention_threshold=args.bm25_threshold,
    )
    results.append(bm25_res)

    # 2. Evaluate Dense
    print("Evaluating Dense Vector (BGE-Small) retrieval...")
    dense_res = evaluator.evaluate_retriever(
        retriever_name="Dense (bge-small)",
        search_fn=lambda q, top_k: service.search_dense(query=q, top_k=top_k),
        abstention_threshold=args.dense_threshold,
    )
    results.append(dense_res)

    # 3. Evaluate Hybrid (RRF)
    print("Evaluating Hybrid (RRF BM25 + Dense) retrieval...")
    rrf_res = evaluator.evaluate_retriever(
        retriever_name="Hybrid RRF (k=60)",
        search_fn=lambda q, top_k: service.search_hybrid(
            query=q,
            top_k=top_k,
            candidate_k=args.candidate_k,
            rrf_k=args.rrf_k,
        ),
        abstention_threshold=args.rrf_threshold,
    )
    results.append(rrf_res)

    # Print formatted side-by-side report
    print_evaluation_report(results)


if __name__ == "__main__":
    main()
