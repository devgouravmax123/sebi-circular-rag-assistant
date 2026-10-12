import asyncio
import json
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, "backend")

from app.api.v1.query import get_retrieval_service, get_safety_gate, query_cscrf
from app.schemas.query import QueryRequest
from app.schemas.safety_gate import AnswerDisposition

async def evaluate_dataset(dataset_path: str, dataset_label: str):
    retrieval_service = get_retrieval_service()
    safety_gate = get_safety_gate()

    with open(dataset_path, "r", encoding="utf-8") as f:
        dataset = json.load(f)

    queries = dataset["queries"]
    print(f"\nEvaluating {dataset_label} ({len(queries)} queries)...")

    results = []
    ans_accepted = 0
    ans_incorrectly_abstained = 0
    unans_correctly_abstained = 0
    unans_incorrectly_answered = 0

    total_ans = sum(1 for q in queries if q["answerability"] == "answerable")
    total_unans = sum(1 for q in queries if q["answerability"] == "unanswerable")

    for q in queries:
        qid = q["query_id"]
        qtext = q["query_text"]
        answerability = q["answerability"]

        req = QueryRequest(query=qtext, top_k=5)
        resp = await query_cscrf(req, retrieval_service=retrieval_service, safety_gate=safety_gate)

        is_abstained = (resp.disposition == AnswerDisposition.CONTROLLED_ABSTENTION)

        if answerability == "answerable":
            if not is_abstained:
                ans_accepted += 1
                status = "ACCEPTED (CORRECT)"
            else:
                ans_incorrectly_abstained += 1
                status = "ABSTAINED (FALSE ABSTENTION)"
        else:
            if is_abstained:
                unans_correctly_abstained += 1
                status = "ABSTAINED (CORRECT)"
            else:
                unans_incorrectly_answered += 1
                status = "ANSWERED (FALSE ACCEPTANCE)"

        results.append({
            "query_id": qid,
            "answerability": answerability,
            "disposition": resp.disposition.value,
            "verified_excerpts": len(resp.verified_excerpts),
            "historical_superseded": len(resp.historical_superseded_excerpts),
            "summary_narrative": resp.summary_narrative,
            "abstention_reason": resp.abstention_reason,
            "evaluation_outcome": status,
        })
        print(f"[{qid}] ({answerability}) -> {resp.disposition.value} | {status} | reason: {resp.abstention_reason or 'Accepted with evidence'}")

    false_abstention_rate = (ans_incorrectly_abstained / total_ans) if total_ans > 0 else 0.0
    unans_abstention_rate = (unans_correctly_abstained / total_unans) if total_unans > 0 else 0.0

    print("\n" + "="*80)
    print(f"CONFUSION MATRIX: {dataset_label}")
    print("="*80)
    print(f"Answerable Queries Accepted:             {ans_accepted} / {total_ans}")
    print(f"Answerable Queries Incorrectly Abstained: {ans_incorrectly_abstained} / {total_ans}")
    print(f"Unanswerable Queries Correctly Abstained: {unans_correctly_abstained} / {total_unans}")
    print(f"Unanswerable Queries Incorrectly Answered:{unans_incorrectly_answered} / {total_unans}")
    print(f"False-Abstention Rate on Answerable:      {false_abstention_rate:.2%}")
    print(f"Abstention Accuracy on Unanswerable:     {unans_abstention_rate:.2%}")
    print("="*80)

    return {
        "dataset": dataset_label,
        "total_queries": len(queries),
        "total_answerable": total_ans,
        "total_unanswerable": total_unans,
        "answerable_accepted": ans_accepted,
        "answerable_incorrectly_abstained": ans_incorrectly_abstained,
        "unanswerable_correctly_abstained": unans_correctly_abstained,
        "unanswerable_incorrectly_answered": unans_incorrectly_answered,
        "false_abstention_rate": false_abstention_rate,
        "unanswerable_abstention_rate": unans_abstention_rate,
        "per_query_results": results,
    }

async def main():
    # 1. Evaluate golden benchmark dataset
    golden_metrics = await evaluate_dataset(
        "evaluation/data/gold_evaluation_dataset.json",
        "GOLDEN EVALUATION DATASET (32 QUERIES)"
    )
    with open("evaluation/data/application_safety_gate_results.json", "w", encoding="utf-8") as out_f:
        json.dump(golden_metrics, out_f, indent=2)

    # 2. Evaluate separate development / validation dataset
    val_metrics = await evaluate_dataset(
        "evaluation/data/validation_abstention_dataset.json",
        "SEPARATE DEV/VALIDATION DATASET (20 QUERIES)"
    )
    with open("evaluation/data/validation_abstention_results.json", "w", encoding="utf-8") as out_f:
        json.dump(val_metrics, out_f, indent=2)

if __name__ == "__main__":
    asyncio.run(main())
