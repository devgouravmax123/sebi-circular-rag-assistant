"""
Query API router implementing POST /api/v1/query.
"""
from typing import Optional, Set, List
import logging
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError

from app.schemas.query import QueryRequest, QueryResponse
from app.schemas.safety_gate import (
    AnswerDisposition,
    CandidateExcerptDraft,
    ExcerptDraftPayload,
    SafetyGateResponse,
    ValidatedExcerptItem,
)
from app.retrieval.service import RetrievalService
from app.grounding.safety_gate import AnswerSafetyGate

from app.grounding.classifier import QueryIntent, classify_query_intent
from app.grounding.relevance import filter_relevant_candidates, RelevanceDecisionCode

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["query"])

# Service singletons (lazy-loaded or dependency-injected)
_retrieval_service: Optional[RetrievalService] = None
_safety_gate: Optional[AnswerSafetyGate] = None


def get_retrieval_service() -> RetrievalService:
    """Dependency provider for the shared RetrievalService instance."""
    global _retrieval_service
    if _retrieval_service is None:
        service = RetrievalService(processed_dir="data/processed", indexes_dir="data/indexes")
        service.load_and_index_corpus()
        service.load_and_index_dense()
        _retrieval_service = service
    return _retrieval_service


def get_safety_gate() -> AnswerSafetyGate:
    """Dependency provider for the shared AnswerSafetyGate instance."""
    global _safety_gate
    if _safety_gate is None:
        _safety_gate = AnswerSafetyGate(processed_dir="data/processed")
    return _safety_gate


def build_response_narrative(
    disposition: AnswerDisposition,
    verified_excerpts: List[ValidatedExcerptItem],
    historical_superseded_excerpts: List[ValidatedExcerptItem],
    include_superseded: bool,
    intent: Optional[QueryIntent] = None,
) -> str:
    """
    Constructs a deterministic, accurate summary narrative matching the exact visible returned excerpts.
    """
    if disposition == AnswerDisposition.CONTROLLED_ABSTENTION:
        if intent == QueryIntent.OUT_OF_SCOPE_OR_UNESTABLISHED:
            return "Controlled Abstention: The query is outside the demonstrated regulatory scope of the 5-document SEBI CSCRF corpus."
        if len(historical_superseded_excerpts) > 0 and not verified_excerpts:
            return (
                "Controlled Abstention: All relevant excerpts in the corpus for this current-compliance query "
                "are SUPERSEDED by subsequent circulars."
            )
        return "Controlled Abstention: Zero candidate passages found in the 5-document SEBI CSCRF corpus for this query."

    if disposition == AnswerDisposition.REJECTED_UNSAFE:
        return "Draft payload rejected: malformed structure, unexpected fields, or duplicate excerpt IDs."

    # For verified/qualified responses:
    v_count = len(verified_excerpts)
    s_count = len(historical_superseded_excerpts)

    if disposition == AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS:
        if include_superseded and s_count > 0:
            return (
                f"Verified Historical Excerpts: {v_count} active excerpt(s) with fully resolved graph status, "
                f"plus {s_count} separated historical superseded excerpt(s)."
            )
        return (
            f"Verified Historical Excerpts: {v_count} excerpt(s) verified against authoritative SEBI CSCRF corpus "
            f"with fully resolved graph status."
        )

    # QUALIFIED_HISTORICAL_EXCERPTS
    if v_count > 0:
        if include_superseded and s_count > 0:
            return (
                f"Qualified Historical Excerpts: {v_count} excerpt(s) verified as authentic text from SEBI CSCRF circulars "
                f"(with {s_count} separated historical superseded excerpt(s)). Provisions with UNRESOLVED status reflect historical "
                f"baseline circular content; active legal currency is not independently established beyond the 5-document pilot."
            )
        return (
            f"Qualified Historical Excerpts: {v_count} excerpt(s) verified as authentic text from SEBI CSCRF circulars. "
            f"Provisions with UNRESOLVED status reflect historical baseline circular content; active legal currency "
            f"is not independently established beyond the 5-document pilot."
        )
    elif s_count > 0:
        return (
            f"Historical Excerpts: {s_count} excerpt(s) verified from earlier circulars, "
            f"which are superseded in the current framework."
        )

    return "Controlled Abstention: No verified excerpts returned."


@router.post(
    "/query",
    response_model=QueryResponse,
    status_code=status.HTTP_200_OK,
    summary="Query SEBI CSCRF regulatory circulars with deterministic grounding",
    description=(
        "Executes hybrid retrieval over the 5-document SEBI CSCRF corpus, verifies excerpts "
        "through the AnswerSafetyGate, and returns grounded historical excerpts or controlled abstention."
    ),
)
async def query_cscrf(
    request: QueryRequest,
    retrieval_service: RetrievalService = Depends(get_retrieval_service),
    safety_gate: AnswerSafetyGate = Depends(get_safety_gate),
) -> QueryResponse:
    query = request.query.strip()
    top_k = request.top_k
    include_superseded = request.include_superseded

    # 1. Execute server-side hybrid retrieval
    # Retrieve top 20 fused candidates to ensure candidate filtering has full scope of candidate circulars
    try:
        retrieval_response = retrieval_service.search_hybrid(
            query=query,
            top_k=20,
            candidate_k=50,
            rrf_k=60,
        )
    except Exception as e:
        logger.error(f"Retrieval execution failure for query length {len(query)}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred while searching the regulatory index.",
        )

    # 2. Filter candidates through relevance safeguards
    relevant_results, intent, assessments = filter_relevant_candidates(query, retrieval_response.results)

    # Limit relevant results to requested top_k after intent-aware filtering and sorting
    relevant_results = relevant_results[:top_k]

    if intent == QueryIntent.OUT_OF_SCOPE_OR_UNESTABLISHED:
        return QueryResponse(
            query=query,
            disposition=AnswerDisposition.CONTROLLED_ABSTENTION,
            verified_excerpts=[],
            historical_superseded_excerpts=[],
            summary_narrative="Controlled Abstention: The query is outside the demonstrated regulatory scope of the 5-document SEBI CSCRF corpus.",
            technical_validation_passed=False,
            retrieval_candidate_count=len(retrieval_response.results),
            abstention_reason="Query subject is outside the scope of the 5-document cybersecurity framework pilot corpus.",
            regulatory_warnings=[],
        )

    authorized_chunk_ids: Set[str] = {r.chunk_id for r in relevant_results}

    # 3. Check for zero relevant candidates -> Controlled Abstention
    if not relevant_results or len(authorized_chunk_ids) == 0:
        return QueryResponse(
            query=query,
            disposition=AnswerDisposition.CONTROLLED_ABSTENTION,
            verified_excerpts=[],
            historical_superseded_excerpts=[],
            summary_narrative="Controlled Abstention: Zero candidate passages found in the 5-document SEBI CSCRF corpus for this query.",
            technical_validation_passed=False,
            retrieval_candidate_count=len(retrieval_response.results),
            abstention_reason="No relevant circular passages identified in the registered corpus.",
            regulatory_warnings=[],
        )

    # 4. Form deterministic excerpt draft from authorized relevant candidates
    candidate_excerpts = []
    for idx, r in enumerate(relevant_results):
        candidate_excerpts.append(
            CandidateExcerptDraft(
                excerpt_id=f"excerpt_{idx + 1}",
                source_chunk_id=r.chunk_id,
                proposed_excerpt_text=r.text,
            )
        )

    draft_payload = ExcerptDraftPayload(
        query=query,
        excerpts=candidate_excerpts,
    )

    # 4. Evaluate through AnswerSafetyGate
    try:
        gate_response: SafetyGateResponse = safety_gate.evaluate_draft(
            payload=draft_payload,
            retrieval_candidates=authorized_chunk_ids,
        )
    except Exception as e:
        logger.error(f"Safety gate evaluation failure for query length {len(query)}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred during regulatory safety verification.",
        )

    # 5. Handle include_superseded flag: if False, filter out historical_superseded_excerpts from response list
    superseded_to_return = gate_response.historical_superseded_excerpts if include_superseded else []

    # 6. Rebuild summary narrative based strictly on the final returned visible excerpts
    final_narrative = build_response_narrative(
        disposition=gate_response.disposition,
        verified_excerpts=gate_response.verified_excerpts,
        historical_superseded_excerpts=superseded_to_return,
        include_superseded=include_superseded,
        intent=intent,
    )

    return QueryResponse(
        query=gate_response.query,
        disposition=gate_response.disposition,
        verified_excerpts=gate_response.verified_excerpts,
        historical_superseded_excerpts=superseded_to_return,
        summary_narrative=final_narrative,
        technical_validation_passed=gate_response.technical_validation_passed,
        retrieval_candidate_count=gate_response.retrieval_candidate_count,
        abstention_reason=gate_response.abstention_reason,
        regulatory_warnings=gate_response.regulatory_warnings,
        pilot_corpus_scope_limitation=gate_response.pilot_corpus_scope_limitation,
    )
