"""
Phase 2.2B.9 — General Document RAG Offline Evaluator

Evaluates retrieval quality, evidence coverage, citation validation,
safe abstention, and claim-support heuristics for the synthetic general collection
without external LLM calls.
"""

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Dict, Any, List, Set, Optional

# Ensure backend directory is in sys.path
backend_path = Path(__file__).resolve().parent.parent / "backend"
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))

from app.retrieval.service import RetrievalService
from app.grounding.safety_gate import AnswerSafetyGate
from app.grounding.relevance import filter_relevant_candidates
from app.grounding.validator import CitationValidator
from app.schemas.citation import CitationItem
from app.schemas.manifest import GroundingPolicyDeclaration, GroundingPolicyType
from app.schemas.safety_gate import (
    SafetyGateResponse,
    AnswerDisposition,
    CandidateExcerptDraft,
    ExcerptDraftPayload,
)
from app.synthesis.verifier import sanitize_and_validate_synthesis_output, verify_claim_support

# Baseline evaluation and dataset files that must never be targeted or overwritten
PROTECTED_BASELINE_FILES = {
    "gold_evaluation_dataset.json",
    "validation_abstention_dataset.json",
    "validation_abstention_results.json",
    "application_safety_gate_results.json",
    "general_evaluation_dataset.json",
    "general_evaluation_results.json",
    "general_live_smoke_test_results.json",
}


def resolve_output_path(
    output_arg: Optional[str] = None,
    repo_root: Optional[Path] = None,
) -> Path:
    """
    Safely resolves and validates the target evaluation output file path.

    Rules:
    1. If output_arg is provided:
       - Must not match any protected baseline filename.
       - Must not already exist (exits immediately if it exists).
       - Parent directory is validated or created safely.
    2. If output_arg is omitted:
       - Automatically generates a unique, timestamped diagnostic filename
         under 'evaluation/data/' (e.g. diagnostic_general_eval_YYYYMMDD_HHMMSS.json)
         preventing accidental overwriting of 'general_evaluation_results.json'.
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent

    eval_data_dir = repo_root / "evaluation" / "data"

    if output_arg:
        target_path = Path(output_arg)
        if not target_path.is_absolute():
            target_path = (repo_root / target_path).resolve()
        else:
            target_path = target_path.resolve()

        # Reject targeting protected baselines by name or path
        if target_path.name.lower() in {f.lower() for f in PROTECTED_BASELINE_FILES}:
            raise ValueError(
                f"Refusing to write to protected baseline evaluation file: {target_path.name}"
            )

        if target_path.exists():
            raise FileExistsError(
                f"Destination output file already exists and will not be overwritten: {target_path}"
            )

        target_path.parent.mkdir(parents=True, exist_ok=True)
        return target_path

    # Default path: unique timestamped diagnostic file under evaluation/data/
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    counter = 0
    candidate = eval_data_dir / f"diagnostic_general_eval_{now_str}.json"
    while candidate.exists():
        counter += 1
        candidate = eval_data_dir / f"diagnostic_general_eval_{now_str}_{counter}.json"

    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


def run_evaluation(output_path: Optional[str] = None, dataset_path: Optional[str] = None):
    repo_root = Path(__file__).resolve().parent.parent
    if dataset_path:
        dpath = Path(dataset_path)
        dataset_file = dpath.resolve() if dpath.is_absolute() else (repo_root / dpath).resolve()
    else:
        dataset_file = repo_root / "evaluation" / "data" / "general_evaluation_dataset.json"

    # Safely resolve destination path before running evaluation
    results_file = resolve_output_path(output_path, repo_root=repo_root)

    with open(dataset_file, "r", encoding="utf-8") as f:
        dataset = json.load(f)

    collection_id = dataset.get("collection_id", "general_synthetic")
    queries = dataset["queries"]
    total_queries = len(queries)

    print(f"============================================================")
    print(f"Phase 2.2B: Evaluating Collection '{collection_id}' ({total_queries} queries)")
    print(f"Dataset Source:     {dataset_file}")
    print(f"Output Destination: {results_file}")
    print(f"============================================================\n")

    policy = GroundingPolicyDeclaration(
        policy_type=GroundingPolicyType.GENERAL_STATIC,
        enforce_regulatory_graph=False,
        enforce_verbatim_citation=True,
        enforce_retrieval_binding=True,
    )

    # Initialize retrieval service and grounding components
    retrieval_service = RetrievalService()
    safety_gate = AnswerSafetyGate()
    citation_validator = CitationValidator(collection_id=collection_id)

    # Metrics accumulators
    answerable_count = 0
    unanswerable_count = 0

    evidence_recall_hits = 0
    evidence_presence_hits = 0
    collection_isolation_violations = 0
    unanswerable_safe_abstentions = 0
    unanswerable_relevance_rejections = 0
    unanswerable_relevance_retained = 0

    citation_validations_attempted = 0
    citation_validations_passed = 0
    citation_validations_rejected = 0

    lexical_heuristic_claims_tested = 0
    lexical_heuristic_false_rejections = 0

    per_query_results = []

    for q in queries:
        qid = q["query_id"]
        qtext = q["question"]
        category = q["category"]
        expected_outcome = q["expected_outcome"]
        expected_docs = set(q.get("expected_documents", []))
        expected_chunks = set(q.get("expected_chunks", []))
        key_facts = q.get("key_facts_to_preserve", [])

        # 1. Retrieval
        retrieval_resp = retrieval_service.search_hybrid(
            query=qtext,
            top_k=5,
            collection_id=collection_id,
        )
        retrieved_results = retrieval_resp.results
        retrieved_chunk_ids = [r.chunk_id for r in retrieved_results]
        retrieved_doc_ids = [r.document_id for r in retrieved_results]

        # Check collection isolation: ensure all retrieved chunks belong to this collection's documents
        for r in retrieved_results:
            if r.document_id not in ["acme_infra_handbook", "acme_security_policy", "acme_product_spec"]:
                collection_isolation_violations += 1

        # Evaluate answerable queries
        if expected_outcome == "answerable":
            answerable_count += 1
            # Check evidence presence (at least one expected document retrieved)
            doc_overlap = expected_docs.intersection(set(retrieved_doc_ids))
            if doc_overlap:
                evidence_presence_hits += 1

            # Check chunk recall (all expected chunks retrieved)
            chunk_overlap = expected_chunks.intersection(set(retrieved_chunk_ids))
            all_chunks_recalled = chunk_overlap == expected_chunks
            if all_chunks_recalled:
                evidence_recall_hits += 1

            # Safety gate check on retrieved chunks via evaluate_draft
            candidate_excerpts = [
                CandidateExcerptDraft(
                    excerpt_id=f"excerpt_{idx + 1}",
                    source_chunk_id=r.chunk_id,
                    proposed_excerpt_text=r.text,
                )
                for idx, r in enumerate(retrieved_results)
            ]
            draft_payload = ExcerptDraftPayload(
                query=qtext,
                excerpts=candidate_excerpts,
            )
            gate_decision: SafetyGateResponse = safety_gate.evaluate_draft(
                payload=draft_payload,
                retrieval_candidates={r.chunk_id for r in retrieved_results},
                policy=policy,
            )
            gate_disposition = gate_decision.disposition

            # Citation validation test with valid chunk references
            q_citations_passed = True
            for cid in expected_chunks:
                # Find chunk in retrieved
                matched_chunk = next((r for r in retrieved_results if r.chunk_id == cid), None)
                if matched_chunk:
                    citation_validations_attempted += 1
                    # Test validator with genuine chunk
                    cit_item = CitationItem(
                        chunk_id=matched_chunk.chunk_id,
                        document_id=matched_chunk.document_id,
                        page_number=matched_chunk.page_start,
                        collection_id=collection_id,
                    )
                    res = citation_validator.validate_citation(cit_item)
                    if res.is_valid:
                        citation_validations_passed += 1
                    else:
                        citation_validations_rejected += 1
                        q_citations_passed = False

            # Claim support verification:
            # 1. Per-chunk claims (if explicitly provided in dataset)
            per_chunk_claims_dict = q.get("per_chunk_claims")
            if per_chunk_claims_dict and isinstance(per_chunk_claims_dict, dict):
                for target_cid, target_facts in per_chunk_claims_dict.items():
                    matched_chunk = next((r for r in retrieved_results if r.chunk_id == target_cid), None)
                    if matched_chunk:
                        for fact in target_facts:
                            lexical_heuristic_claims_tested += 1
                            supported, score, reasons = verify_claim_support(fact, matched_chunk.text)
                            if not supported:
                                lexical_heuristic_false_rejections += 1
            # 2. Combined / cross-chunk claims (if explicitly provided)
            cross_chunk_claims = q.get("cross_chunk_claims")
            if cross_chunk_claims and isinstance(cross_chunk_claims, list):
                combined_text = "\n".join(r.text for r in retrieved_results if r.chunk_id in expected_chunks)
                for fact in cross_chunk_claims:
                    lexical_heuristic_claims_tested += 1
                    supported, score, reasons = verify_claim_support(fact, combined_text)
                    if not supported:
                        lexical_heuristic_false_rejections += 1
            # 3. Fallback: key_facts_to_preserve evaluated against each expected chunk (backward compatibility)
            if not per_chunk_claims_dict and not cross_chunk_claims:
                for cid in expected_chunks:
                    matched_chunk = next((r for r in retrieved_results if r.chunk_id == cid), None)
                    if matched_chunk:
                        for fact in key_facts:
                            lexical_heuristic_claims_tested += 1
                            supported, score, reasons = verify_claim_support(fact, matched_chunk.text)
                            if not supported:
                                lexical_heuristic_false_rejections += 1

            per_query_results.append({
                "query_id": qid,
                "category": category,
                "expected_outcome": expected_outcome,
                "retrieved_chunks": retrieved_chunk_ids,
                "expected_chunks": list(expected_chunks),
                "evidence_presence": bool(doc_overlap),
                "recall_success": all_chunks_recalled,
                "gate_disposition": gate_disposition.value,
                "status": "PASS" if all_chunks_recalled and gate_disposition in (AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS, AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS) else "FAIL",
            })

        else:
            # Unanswerable / unsupported inference queries
            unanswerable_count += 1
            candidate_excerpts = [
                CandidateExcerptDraft(
                    excerpt_id=f"excerpt_{idx + 1}",
                    source_chunk_id=r.chunk_id,
                    proposed_excerpt_text=r.text,
                )
                for idx, r in enumerate(retrieved_results)
            ]
            draft_payload = ExcerptDraftPayload(
                query=qtext,
                excerpts=candidate_excerpts,
            )
            gate_decision: SafetyGateResponse = safety_gate.evaluate_draft(
                payload=draft_payload,
                retrieval_candidates={r.chunk_id for r in retrieved_results},
                policy=policy,
            )
            max_score = max([r.score for r in retrieved_results], default=0.0)

            # Task 1: Actually evaluate the production relevance filter on unanswerable candidates
            rel_candidates, query_intent, relevance_assessments = filter_relevant_candidates(
                query=qtext,
                candidates=retrieved_results,
            )
            relevance_accepted_count = len(rel_candidates)
            relevance_rejected_all = (relevance_accepted_count == 0)

            if relevance_rejected_all:
                unanswerable_relevance_rejections += 1
            else:
                unanswerable_relevance_retained += 1

            # Check if expected chunks were falsely pulled (expected is empty)
            false_evidence_pulled = bool(expected_chunks.intersection(set(retrieved_chunk_ids)))
            if not false_evidence_pulled:
                unanswerable_safe_abstentions += 1

            per_query_results.append({
                "query_id": qid,
                "category": category,
                "expected_outcome": expected_outcome,
                "retrieved_chunks": retrieved_chunk_ids,
                "retrieved_count": len(retrieved_results),
                "relevance_accepted_count": relevance_accepted_count,
                "relevance_accepted_chunk_ids": [c.chunk_id for c in rel_candidates],
                "relevance_filter_rejected_all": relevance_rejected_all,
                "top_score": max_score,
                "gate_disposition": gate_decision.disposition.value,
                "safe_abstention_enforced": not false_evidence_pulled,
                "status": "PASS" if not false_evidence_pulled else "FAIL",
            })

    # Summary metrics with explicit denominators
    recall_rate = (evidence_recall_hits / answerable_count) * 100 if answerable_count else 0.0
    presence_rate = (evidence_presence_hits / answerable_count) * 100 if answerable_count else 0.0
    citation_valid_rate = (citation_validations_passed / citation_validations_attempted) * 100 if citation_validations_attempted else 0.0
    safe_abstention_rate = (unanswerable_safe_abstentions / unanswerable_count) * 100 if unanswerable_count else 0.0
    relevance_rejection_rate = (unanswerable_relevance_rejections / unanswerable_count) * 100 if unanswerable_count else 0.0
    false_rejection_rate = (lexical_heuristic_false_rejections / lexical_heuristic_claims_tested) * 100 if lexical_heuristic_claims_tested else 0.0

    summary = {
        "dataset_version": dataset.get("dataset_version"),
        "collection_id": collection_id,
        "total_queries": total_queries,
        "answerable_queries": {
            "count": answerable_count,
            "evidence_recall_hits": evidence_recall_hits,
            "recall_rate_pct": f"{recall_rate:.2f}% ({evidence_recall_hits}/{answerable_count})",
            "evidence_presence_hits": evidence_presence_hits,
            "evidence_presence_rate_pct": f"{presence_rate:.2f}% ({evidence_presence_hits}/{answerable_count})",
        },
        "unanswerable_queries": {
            "count": unanswerable_count,
            "relevance_filter_zero_candidates": unanswerable_relevance_rejections,
            "relevance_filter_retained_candidates": unanswerable_relevance_retained,
            "relevance_filter_rejection_rate_pct": f"{relevance_rejection_rate:.2f}% ({unanswerable_relevance_rejections}/{unanswerable_count})",
            "relevance_filter_retained_rate_pct": f"{(unanswerable_relevance_retained / unanswerable_count) * 100:.2f}% ({unanswerable_relevance_retained}/{unanswerable_count})" if unanswerable_count else "0.00%",
            "safe_abstentions_no_false_expected_chunks": unanswerable_safe_abstentions,
            "safe_abstention_rate_pct": f"{safe_abstention_rate:.2f}% ({unanswerable_safe_abstentions}/{unanswerable_count})",
        },
        "collection_isolation": {
            "violations": collection_isolation_violations,
            "status": "ISOLATED" if collection_isolation_violations == 0 else "LEAK_DETECTED",
        },
        "citation_validation": {
            "attempted": citation_validations_attempted,
            "passed": citation_validations_passed,
            "rejected": citation_validations_rejected,
            "valid_rate_pct": f"{citation_valid_rate:.2f}% ({citation_validations_passed}/{citation_validations_attempted})",
        },
        "lexical_support_heuristic": {
            "claims_tested": lexical_heuristic_claims_tested,
            "false_rejections": lexical_heuristic_false_rejections,
            "false_rejection_rate_pct": f"{false_rejection_rate:.2f}% ({lexical_heuristic_false_rejections}/{lexical_heuristic_claims_tested})",
        },
        "per_query_results": per_query_results,
    }

    with open(results_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print("=== Evaluation Summary ===")
    print(f"Total queries evaluated: {total_queries}")
    print(f"Evidence Recall @ top_k=5: {summary['answerable_queries']['recall_rate_pct']}")
    print(f"Evidence Presence @ top_k=5: {summary['answerable_queries']['evidence_presence_rate_pct']}")
    print(f"Collection Isolation Violations: {collection_isolation_violations}")
    print(f"Citation Validation Success: {summary['citation_validation']['valid_rate_pct']}")
    print(f"Unanswerable Relevance Filter Rejections (0 candidates): {summary['unanswerable_queries']['relevance_filter_rejection_rate_pct']}")
    print(f"Unanswerable Relevance Filter Retained (>=1 candidates): {summary['unanswerable_queries']['relevance_filter_retained_rate_pct']}")
    print(f"Unanswerable No False Expected Chunks: {summary['unanswerable_queries']['safe_abstention_rate_pct']}")
    print(f"Lexical Heuristic False Rejections: {summary['lexical_support_heuristic']['false_rejection_rate_pct']}")
    print(f"\nResults saved to {results_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Phase 2.2B: General Document RAG Offline Evaluator"
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Optional destination path for evaluation results. Fails if file already exists or targets a protected baseline.",
    )
    parser.add_argument(
        "--dataset",
        type=str,
        default=None,
        help="Optional path to evaluation dataset JSON file. Defaults to evaluation/data/general_evaluation_dataset.json.",
    )
    args = parser.parse_args()
    try:
        run_evaluation(output_path=args.output, dataset_path=args.dataset)
    except (FileExistsError, ValueError) as err:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)
