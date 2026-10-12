"""
Reciprocal Rank Fusion (RRF) for hybrid lexical and dense retrieval.

Implements standard chunk-level RRF:
    RRF(c) = sum_r 1 / (k + rank_r(c))

where:
- r is a component retriever (e.g. BM25, Dense).
- rank_r(c) is the 1-based rank of chunk c in retriever r's candidate list.
- k is a smoothing parameter (default = 60).
- Chunks not in retriever r's list contribute 0.
- Ties in fused score are broken deterministically by chunk_id ascending.
"""
from typing import List, Dict, Any, Optional
from app.schemas.retrieval import RetrievalResult, RetrievalResponse


def reciprocal_rank_fusion(
    ranked_lists: List[List[RetrievalResult]],
    k: int = 60,
    top_k: int = 5,
    query: str = "",
) -> RetrievalResponse:
    """
    Fuse multiple ranked lists of RetrievalResult items using Reciprocal Rank Fusion (RRF).

    Args:
        ranked_lists: List of candidate result lists (e.g. [bm25_results, dense_results]).
        k: RRF smoothing constant (default = 60).
        top_k: Number of fused results to return.
        query: Query string for populating RetrievalResponse.

    Returns:
        RetrievalResponse containing top_k fused results with metadata tracking component ranks/scores.
    """
    if k <= 0:
        raise ValueError(f"RRF smoothing parameter k must be positive, got {k}")
    if top_k < 0:
        raise ValueError(f"top_k must be non-negative, got {top_k}")

    if not ranked_lists or top_k == 0:
        return RetrievalResponse(query=query, total_results=0, results=[])

    # Map chunk_id to fused score, candidate item provenance, and component rank/score diagnostics
    fused_scores: Dict[str, float] = {}
    chunk_items: Dict[str, RetrievalResult] = {}
    component_diagnostics: Dict[str, Dict[str, Any]] = {}

    for list_idx, candidate_list in enumerate(ranked_lists):
        retriever_tag = f"retriever_{list_idx}"
        seen_chunks_in_list: set = set()

        one_based_rank = 1
        for res in candidate_list:
            cid = res.chunk_id
            # Deduplicate chunk IDs within the same candidate list
            if cid in seen_chunks_in_list:
                continue
            seen_chunks_in_list.add(cid)

            # Store representative item if not seen
            if cid not in chunk_items:
                chunk_items[cid] = res
                component_diagnostics[cid] = {}

            # RRF score addition
            rrf_contribution = 1.0 / (k + one_based_rank)
            fused_scores[cid] = fused_scores.get(cid, 0.0) + rrf_contribution

            # Record component diagnostics
            component_diagnostics[cid][f"{retriever_tag}_rank"] = one_based_rank
            component_diagnostics[cid][f"{retriever_tag}_score"] = res.score

            one_based_rank += 1

    # Deterministic sorting: descending score, ascending chunk_id for tie-breaking
    sorted_chunk_ids = sorted(
        fused_scores.keys(),
        key=lambda cid: (-fused_scores[cid], cid),
    )

    results: List[RetrievalResult] = []
    for final_rank, cid in enumerate(sorted_chunk_ids[:top_k], start=1):
        base_item = chunk_items[cid]
        # Merge metadata with component diagnostics
        combined_metadata = dict(base_item.metadata)
        combined_metadata.update(component_diagnostics[cid])
        combined_metadata["rrf_score"] = fused_scores[cid]

        fused_item = RetrievalResult(
            chunk_id=base_item.chunk_id,
            document_id=base_item.document_id,
            title=base_item.title,
            circular_number=base_item.circular_number,
            issue_date=base_item.issue_date,
            effective_date=base_item.effective_date,
            regulator=base_item.regulator,
            domain=base_item.domain,
            sha256=base_item.sha256,
            page_start=base_item.page_start,
            page_end=base_item.page_end,
            text=base_item.text,
            score=fused_scores[cid],
            rank=final_rank,
            is_ocr=base_item.is_ocr,
            metadata=combined_metadata,
        )
        results.append(fused_item)

    return RetrievalResponse(
        query=query,
        total_results=len(results),
        results=results,
    )
