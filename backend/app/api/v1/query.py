"""
Query API router implementing POST /api/v1/query.
"""
from typing import Optional, Set, List
import logging
from fastapi import APIRouter, Depends, HTTPException, Request, status
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
from app.synthesis.service import SynthesisService, get_synthesis_service
from app.core.rate_limiter import get_query_rate_limiter

from app.grounding.classifier import QueryIntent, classify_query_intent
from app.grounding.relevance import filter_relevant_candidates, RelevanceDecisionCode
from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["query"])

# Service singletons (lazy-loaded or dependency-injected)
_retrieval_service: Optional[RetrievalService] = None
_safety_gate: Optional[AnswerSafetyGate] = None


def get_retrieval_service(settings: Settings = Depends(get_settings)) -> RetrievalService:
    """Dependency provider for the shared RetrievalService instance."""
    global _retrieval_service
    if (
        _retrieval_service is None
        or str(_retrieval_service.processed_dir) != settings.processed_dir
        or str(_retrieval_service.indexes_dir) != settings.indexes_dir
        or _retrieval_service.retrieval_mode != settings.retrieval_mode.strip().lower()
    ):
        service = RetrievalService(
            processed_dir=settings.processed_dir,
            indexes_dir=settings.indexes_dir,
            retrieval_mode=settings.retrieval_mode,
        )
        service.load_and_index_corpus()
        service.load_and_index_dense()
        _retrieval_service = service
    else:
        # Check if active index snapshot was updated and reload atomically
        _retrieval_service.reload_if_stale()
    return _retrieval_service


def get_safety_gate(settings: Settings = Depends(get_settings)) -> AnswerSafetyGate:
    """Dependency provider for the shared AnswerSafetyGate instance."""
    global _safety_gate
    if _safety_gate is None or str(_safety_gate.processed_dir) != settings.processed_dir:
        _safety_gate = AnswerSafetyGate(processed_dir=settings.processed_dir)
    return _safety_gate


from app.schemas.retrieval import RetrievalFilter


def build_response_narrative(
    disposition: AnswerDisposition,
    verified_excerpts: List[ValidatedExcerptItem],
    historical_superseded_excerpts: List[ValidatedExcerptItem],
    include_superseded: bool,
    intent: Optional[QueryIntent] = None,
    domain: Optional[str] = None,
) -> str:
    """
    Constructs a deterministic, accurate summary narrative matching the exact visible returned excerpts.
    """
    corpus_label = f"SEBI {domain} corpus" if domain else "SEBI regulatory corpus"

    if disposition == AnswerDisposition.CONTROLLED_ABSTENTION:
        if intent == QueryIntent.OUT_OF_SCOPE_OR_UNESTABLISHED:
            return f"Controlled Abstention: The query is outside the demonstrated regulatory scope of the {corpus_label}."
        if len(historical_superseded_excerpts) > 0 and not verified_excerpts:
            return (
                f"Controlled Abstention: All relevant excerpts in the {corpus_label} for this current-compliance query "
                "are SUPERSEDED by subsequent circulars."
            )
        return f"Controlled Abstention: Zero candidate passages found in the {corpus_label} for this query."

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
            f"Verified Historical Excerpts: {v_count} excerpt(s) verified against authoritative {corpus_label} "
            f"with fully resolved graph status."
        )

    # QUALIFIED_HISTORICAL_EXCERPTS
    if v_count > 0:
        if include_superseded and s_count > 0:
            return (
                f"Qualified Historical Excerpts: {v_count} excerpt(s) verified as authentic text from {corpus_label} "
                f"(with {s_count} separated historical superseded excerpt(s)). Provisions with UNRESOLVED status reflect historical "
                f"circular content; active legal currency is not independently established beyond the pilot."
            )
        return (
            f"Qualified Historical Excerpts: {v_count} excerpt(s) verified as authentic text from {corpus_label}. "
            f"Provisions with UNRESOLVED status reflect historical circular content; active legal currency "
            f"is not independently established beyond the pilot."
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
    summary="Query SEBI regulatory circulars with deterministic grounding",
    description=(
        "Executes hybrid retrieval over the published SEBI corpus with optional domain and date filtering, "
        "verifies excerpts through the AnswerSafetyGate, and returns grounded excerpts or controlled abstention."
    ),
)
async def query_cscrf(
    request: QueryRequest,
    http_request: Request,
    retrieval_service: Optional[RetrievalService] = Depends(get_retrieval_service),
    safety_gate: Optional[AnswerSafetyGate] = Depends(get_safety_gate),
    synthesis_service: Optional[SynthesisService] = Depends(get_synthesis_service),
) -> QueryResponse:
    # Phase 10.1.20: Enforce in-memory sliding-window rate limit on public query endpoint
    limiter = get_query_rate_limiter()
    limiter.check_rate_limit(http_request)

    if retrieval_service is None or not hasattr(retrieval_service, "search_hybrid"):
        retrieval_service = get_retrieval_service()
    if safety_gate is None or not hasattr(safety_gate, "evaluate_draft"):
        safety_gate = get_safety_gate()
    if synthesis_service is None or not hasattr(synthesis_service, "synthesize_answer"):
        synthesis_service = get_synthesis_service()

    query = request.query.strip()
    top_k = request.top_k
    include_superseded = request.include_superseded

    # Construct optional multi-domain retrieval filter
    filters = None
    if any([request.domain, request.document_id, request.start_date, request.end_date, request.regulator]):
        filters = RetrievalFilter(
            regulator=request.regulator,
            domain=request.domain,
            document_id=request.document_id,
            start_date=request.start_date,
            end_date=request.end_date,
        )

    # 1. Execute server-side hybrid retrieval with pre-fusion filters
    try:
        retrieval_response = retrieval_service.search_hybrid(
            query=query,
            top_k=50,
            candidate_k=50,
            rrf_k=60,
            filters=filters,
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

    corpus_label = f"SEBI {request.domain} corpus" if request.domain else "SEBI regulatory corpus"

    if intent == QueryIntent.OUT_OF_SCOPE_OR_UNESTABLISHED:
        return QueryResponse(
            query=query,
            disposition=AnswerDisposition.CONTROLLED_ABSTENTION,
            verified_excerpts=[],
            historical_superseded_excerpts=[],
            summary_narrative=f"Controlled Abstention: The query is outside the demonstrated regulatory scope of the {corpus_label}.",
            technical_validation_passed=False,
            retrieval_candidate_count=len(retrieval_response.results),
            abstention_reason=f"Query subject is outside the scope of the {corpus_label}.",
            regulatory_warnings=[],
            synthesis_status="ABSTAINED",
            answer_text="Controlled Abstention: The query is outside the demonstrated regulatory scope of the corpus.",
            claim_citations=[],
            unresolved_subquestions=[],
            synthesis_failure_reason=f"Query subject is outside the scope of the {corpus_label}.",
        )

    authorized_chunk_ids: Set[str] = {r.chunk_id for r in relevant_results}

    # 3. Check for zero relevant candidates -> Controlled Abstention
    if not relevant_results or len(authorized_chunk_ids) == 0:
        # Check if any candidates were evaluated and rejected for insufficient topical coverage
        low_coverage_assessments = [
            a for a in assessments if a.decision_code == RelevanceDecisionCode.INSUFFICIENT_RELEVANCE
        ]
        if low_coverage_assessments:
            abstention_reason = (
                f"Insufficient evidence: candidate passages in the {corpus_label} do not substantively cover the query topic."
            )
            narrative = (
                f"Controlled Abstention: Insufficient evidence found in the {corpus_label} to substantiate an answer for this query."
            )
        else:
            abstention_reason = f"No relevant circular passages identified in the registered {corpus_label}."
            narrative = f"Controlled Abstention: Zero candidate passages found in the {corpus_label} for this query."

        return QueryResponse(
            query=query,
            disposition=AnswerDisposition.CONTROLLED_ABSTENTION,
            verified_excerpts=[],
            historical_superseded_excerpts=[],
            summary_narrative=narrative,
            technical_validation_passed=False,
            retrieval_candidate_count=len(retrieval_response.results),
            abstention_reason=abstention_reason,
            regulatory_warnings=[],
            synthesis_status="ABSTAINED",
            answer_text=narrative,
            claim_citations=[],
            unresolved_subquestions=[],
            synthesis_failure_reason=abstention_reason,
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
        domain=request.domain,
    )

    # 7. Execute Grounded LLM Synthesis over verified excerpts (Phase 8E)
    synthesis_result = synthesis_service.synthesize_answer(
        query=query,
        verified_excerpts=gate_response.verified_excerpts,
        disposition=gate_response.disposition,
        regulatory_warnings=gate_response.regulatory_warnings,
        domain=request.domain,
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
        answer_text=synthesis_result.answer,
        synthesis_status=synthesis_result.status.value,
        claim_citations=synthesis_result.claims,
        unresolved_subquestions=synthesis_result.unresolved_subquestions,
        synthesis_failure_reason=synthesis_result.failure_reason,
    )
