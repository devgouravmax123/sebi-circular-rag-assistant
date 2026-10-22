import json
import sys
from pathlib import Path
from typing import Dict, Any, List, Set, Optional

# Ensure backend directory is in sys.path
backend_path = Path(__file__).resolve().parent.parent / "backend"
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))

from fastapi.testclient import TestClient
from app.main import app
from app.api.v1.query import get_synthesis_service as api_get_synthesis_service
from app.synthesis.service import SynthesisService
from app.synthesis.mock_provider import MockSynthesisProvider
from app.schemas.safety_gate import AnswerDisposition


def evaluate_dataset(client: TestClient, dataset_path: str, dataset_label: str) -> Dict[str, Any]:
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

        # Call public query route via TestClient
        http_resp = client.post("/api/v1/query", json={"query": qtext, "top_k": 5})

        if http_resp.status_code != 200:
            status = f"HTTP_ERROR_{http_resp.status_code}"
            print(f"[{qid}] ({answerability}) -> HTTP {http_resp.status_code} Error: {http_resp.text[:120]}")
            results.append({
                "query_id": qid,
                "answerability": answerability,
                "http_status": http_resp.status_code,
                "error": http_resp.text,
                "evaluation_outcome": status,
                "retrieved_verified_chunk_ids": [],
                "emitted_citations": [],
            })
            continue

        resp_data = http_resp.json()
        raw_disp = resp_data.get("disposition")
        try:
            disp_enum = AnswerDisposition(raw_disp)
        except (ValueError, TypeError):
            status = f"MALFORMED_DISPOSITION_{raw_disp}"
            print(f"[{qid}] ({answerability}) -> Malformed disposition: {raw_disp}")
            results.append({
                "query_id": qid,
                "answerability": answerability,
                "disposition": raw_disp,
                "evaluation_outcome": status,
                "retrieved_verified_chunk_ids": [],
                "emitted_citations": [],
            })
            continue

        is_abstained = (disp_enum == AnswerDisposition.CONTROLLED_ABSTENTION)

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

        verified_excs = resp_data.get("verified_excerpts") or []
        superseded_excs = resp_data.get("historical_superseded_excerpts") or []
        summary_narr = resp_data.get("summary_narrative")
        abstention_rsn = resp_data.get("abstention_reason")

        # Collect verified chunk IDs (only if well-formed)
        retrieved_chunk_ids: List[str] = []
        if not is_abstained and isinstance(verified_excs, list):
            for ve in verified_excs:
                if isinstance(ve, dict) and "source_chunk_id" in ve and isinstance(ve["source_chunk_id"], str):
                    retrieved_chunk_ids.append(ve["source_chunk_id"])

        # Collect emitted citations from claim citations
        emitted_citations: List[str] = []
        claim_citations = resp_data.get("claim_citations") or []
        if isinstance(claim_citations, list):
            for cc in claim_citations:
                if isinstance(cc, dict):
                    # Check supporting_chunk_ids
                    supp_cids = cc.get("supporting_chunk_ids") or []
                    if isinstance(supp_cids, list):
                        for cid in supp_cids:
                            if isinstance(cid, str):
                                emitted_citations.append(cid)
                    # Check resolved citations provenance
                    cits = cc.get("citations") or []
                    if isinstance(cits, list):
                        for cit in cits:
                            if isinstance(cit, dict):
                                cit_src = cit.get("source_chunk_id")
                                if isinstance(cit_src, str) and cit_src not in supp_cids:
                                    emitted_citations.append(cit_src)

        results.append({
            "query_id": qid,
            "answerability": answerability,
            "disposition": disp_enum.value,
            "verified_excerpts": len(verified_excs),
            "historical_superseded": len(superseded_excs),
            "summary_narrative": summary_narr,
            "abstention_reason": abstention_rsn,
            "evaluation_outcome": status,
            "retrieved_verified_chunk_ids": retrieved_chunk_ids,
            "emitted_citations": emitted_citations,
        })
        print(f"[{qid}] ({answerability}) -> {disp_enum.value} | {status} | reason: {abstention_rsn or 'Accepted with evidence'}")

    false_abstention_rate = (ans_incorrectly_abstained / total_ans) if total_ans > 0 else 0.0
    unans_abstention_rate = (unans_correctly_abstained / total_unans) if total_unans > 0 else 0.0

    print("\n" + "="*80)
    print(f"CONFUSION MATRIX: {dataset_label}")
    print("="*80)
    print(f"Answerable Queries Accepted:             {ans_accepted} / {total_ans}")
    print(f"Answerable Queries Incorrectly Abstained: {ans_incorrectly_abstained} / {total_ans}")
    print(f"Unanswerable Queries Correctly Abstained: {unans_correctly_abstained} / {total_ans}")
    print(f"Unanswerable Queries Incorrectly Answered:{unans_incorrectly_answered} / {total_unans}")
    print(f"False-Abstention Rate on Answerable:      {false_abstention_rate:.2%}")
    print(f"Abstention Accuracy on Unanswerable:     {unans_abstention_rate:.2%}")
    print("="*80)

    # -------------------------------------------------------------------------
    # Evidence Grounding Quality Evaluation (for datasets with gold chunk labels)
    # -------------------------------------------------------------------------
    evidence_grounding_metrics: Optional[Dict[str, Any]] = None
    has_gold_chunks = any(
        isinstance(q.get("chunk_labels"), dict) and "gold_chunk_ids" in q["chunk_labels"]
        for q in queries
    )

    if has_gold_chunks:
        ans_queries_with_gold = [
            q for q in queries
            if q.get("answerability") == "answerable" and q.get("chunk_labels", {}).get("gold_chunk_ids") is not None
        ]
        total_ans_eval = len(ans_queries_with_gold)
        total_gold_chunks_across_queries = sum(
            len(q["chunk_labels"]["gold_chunk_ids"]) for q in ans_queries_with_gold
        )

        gold_chunks_found_in_excerpts = 0
        queries_with_at_least_one_gold = 0
        queries_with_no_gold = 0

        total_citation_references = 0
        valid_citation_references = 0
        gold_citation_references = 0

        res_by_qid = {r["query_id"]: r for r in results}

        for q in ans_queries_with_gold:
            qid = q["query_id"]
            gold_cids: Set[str] = set(q["chunk_labels"].get("gold_chunk_ids") or [])
            q_res = res_by_qid.get(qid, {})

            retrieved_chunk_ids: List[str] = q_res.get("retrieved_verified_chunk_ids") or []
            retrieved_cids_set = set(retrieved_chunk_ids)

            # Gold evidence recall across answerable queries
            overlap = gold_cids.intersection(retrieved_cids_set)
            gold_chunks_found_in_excerpts += len(overlap)

            if len(overlap) > 0:
                queries_with_at_least_one_gold += 1
            else:
                queries_with_no_gold += 1

            # Citations evaluation
            emitted_cits: List[str] = q_res.get("emitted_citations") or []
            for cit_cid in emitted_cits:
                total_citation_references += 1
                if cit_cid in retrieved_cids_set:
                    valid_citation_references += 1
                if cit_cid in gold_cids:
                    gold_citation_references += 1

        gold_evidence_recall = (
            (gold_chunks_found_in_excerpts / total_gold_chunks_across_queries)
            if total_gold_chunks_across_queries > 0 else 0.0
        )
        gold_presence_rate = (
            (queries_with_at_least_one_gold / total_ans_eval)
            if total_ans_eval > 0 else 0.0
        )

        citation_validity_str = (
            f"{(valid_citation_references / total_citation_references):.2%}"
            if total_citation_references > 0 else "N/A"
        )
        gold_citation_precision_str = (
            f"{(gold_citation_references / total_citation_references):.2%}"
            if total_citation_references > 0 else "N/A"
        )

        print("\n" + "="*80)
        print("EVIDENCE GROUNDING QUALITY (ANSWERABLE EVALUATION)")
        print("Pipeline-level evaluation using MockSynthesisProvider;")
        print("does not prove factual correctness or live LLM citation faithfulness.")
        print("="*80)
        print(f"Answerable Queries Evaluated:            {total_ans_eval}")
        print(f"Total Gold Chunk IDs Expected:           {total_gold_chunks_across_queries}")
        print(f"Total Gold Chunk IDs in Verified Excerpts: {gold_chunks_found_in_excerpts}")
        print(f"End-to-End Gold Evidence Recall:         {gold_evidence_recall:.2%}")
        print(f"Gold Evidence Presence Rate:             {gold_presence_rate:.2%} ({queries_with_at_least_one_gold} / {total_ans_eval})")
        print(f"Queries with No Gold Evidence:           {queries_with_no_gold} / {total_ans_eval}")
        print(f"Total Emitted Citation References:       {total_citation_references}")
        print(f"Citation Validity (Points to Verified):  {citation_validity_str} ({valid_citation_references} / {total_citation_references if total_citation_references > 0 else 'N/A'})")
        print(f"Gold Citation Precision (Points to Gold):{gold_citation_precision_str} ({gold_citation_references} / {total_citation_references if total_citation_references > 0 else 'N/A'})")
        print("="*80)

        evidence_grounding_metrics = {
            "disclaimer": "Pipeline-level evaluation with MockSynthesisProvider. Does not prove factual correctness or live LLM faithfulness.",
            "total_answerable_queries_evaluated": total_ans_eval,
            "total_gold_chunks_expected": total_gold_chunks_across_queries,
            "total_gold_chunks_retrieved_verified": gold_chunks_found_in_excerpts,
            "end_to_end_gold_evidence_recall": gold_evidence_recall,
            "gold_evidence_presence_rate": gold_presence_rate,
            "queries_with_at_least_one_gold": queries_with_at_least_one_gold,
            "queries_with_no_gold_evidence": queries_with_no_gold,
            "total_emitted_citations": total_citation_references,
            "valid_citations_count": valid_citation_references,
            "citation_validity": (valid_citation_references / total_citation_references) if total_citation_references > 0 else None,
            "gold_citations_count": gold_citation_references,
            "gold_citation_precision": (gold_citation_references / total_citation_references) if total_citation_references > 0 else None,
        }

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
        "evidence_grounding_quality": evidence_grounding_metrics,
        "per_query_results": results,
    }


def main():
    # Save any pre-existing override so global application state can be faithfully restored
    had_previous_override = api_get_synthesis_service in app.dependency_overrides
    previous_override = app.dependency_overrides.get(api_get_synthesis_service)

    # Isolate synthesis with MockSynthesisProvider so evaluation uses 0 tokens/network
    app.dependency_overrides[api_get_synthesis_service] = lambda: SynthesisService(provider=MockSynthesisProvider())

    try:
        client = TestClient(app)

        # 1. Evaluate golden benchmark dataset
        golden_metrics = evaluate_dataset(
            client,
            "evaluation/data/gold_evaluation_dataset.json",
            "GOLDEN EVALUATION DATASET (32 QUERIES)"
        )
        with open("evaluation/data/application_safety_gate_results.json", "w", encoding="utf-8") as out_f:
            json.dump(golden_metrics, out_f, indent=2)

        # 2. Evaluate separate development / validation dataset
        val_metrics = evaluate_dataset(
            client,
            "evaluation/data/validation_abstention_dataset.json",
            "SEPARATE DEV/VALIDATION DATASET (20 QUERIES)"
        )
        with open("evaluation/data/validation_abstention_results.json", "w", encoding="utf-8") as out_f:
            json.dump(val_metrics, out_f, indent=2)
    finally:
        # Faithfully restore pre-existing override or remove the evaluator override
        if had_previous_override and previous_override is not None:
            app.dependency_overrides[api_get_synthesis_service] = previous_override
        else:
            app.dependency_overrides.pop(api_get_synthesis_service, None)


if __name__ == "__main__":
    main()
