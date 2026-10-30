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
from app.grounding.validator import CitationValidator
from app.synthesis.service import SynthesisService, get_synthesis_service
from app.core.rate_limiter import get_query_rate_limiter

from app.grounding.classifier import QueryIntent, classify_query_intent
from app.grounding.relevance import filter_relevant_candidates, RelevanceDecisionCode
from app.core.config import Settings, get_settings
from app.ingestion.collection_registry import CollectionRegistry, UnknownCollectionError
from app.schemas.manifest import (
    CollectionStatus,
    CollectionSummary,
    GroundingPolicyDeclaration,
    GroundingPolicyType,
)
from app.schemas.document import resolve_collection_identity

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["query"])

# Service singletons (lazy-loaded or dependency-injected)
_retrieval_service: Optional[RetrievalService] = None
_safety_gate: Optional[AnswerSafetyGate] = None
_collection_registry: Optional[CollectionRegistry] = None


def get_collection_registry(settings: Settings = Depends(get_settings)) -> CollectionRegistry:
    """Dependency provider for the shared CollectionRegistry instance."""
    global _collection_registry
    if (
        _collection_registry is None
        or str(_collection_registry.indexes_dir) != settings.indexes_dir
    ):
        _collection_registry = CollectionRegistry(
            manifests_dir="data/manifests",
            indexes_dir=settings.indexes_dir,
        )
    return _collection_registry


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


def get_safety_gate(
    settings: Settings = Depends(get_settings),
    retrieval_service: Optional[RetrievalService] = Depends(get_retrieval_service),
) -> AnswerSafetyGate:
    """Dependency provider for the shared AnswerSafetyGate instance."""
    global _safety_gate
    retrieval = retrieval_service if retrieval_service is not None else get_retrieval_service()
    # Check if safety gate cache needs initialization or refresh
    expected_chunks = len(retrieval.chunks) if retrieval else 0
    current_validator_chunks = (
        len(_safety_gate.validator.corpus_chunks)
        if _safety_gate and _safety_gate.validator and _safety_gate.validator.corpus_chunks
        else 0
    )
    if (
        _safety_gate is None
        or str(_safety_gate.processed_dir) != settings.processed_dir
        or (_safety_gate.retrieval_service is not retrieval)
        or (expected_chunks > 0 and current_validator_chunks == 0)
    ):
        corpus_chunks = {c.chunk_id: c for c in retrieval.chunks} if retrieval and retrieval.chunks else None
        corpus_docs = retrieval.documents if retrieval and retrieval.documents else None
        validator = CitationValidator(
            processed_dir=settings.processed_dir,
            corpus_chunks=corpus_chunks,
            corpus_documents=corpus_docs,
        )
        _safety_gate = AnswerSafetyGate(
            processed_dir=settings.processed_dir,
            validator=validator,
            retrieval_service=retrieval,
        )
    return _safety_gate


from app.schemas.retrieval import RetrievalFilter


def build_response_narrative(
    disposition: AnswerDisposition,
    verified_excerpts: List[ValidatedExcerptItem],
    historical_superseded_excerpts: List[ValidatedExcerptItem],
    include_superseded: bool,
    intent: Optional[QueryIntent] = None,
    domain: Optional[str] = None,
    candidate_count: Optional[int] = None,
    abstention_reason: Optional[str] = None,
    collection_name: Optional[str] = None,
    policy: Optional[GroundingPolicyDeclaration] = None,
) -> str:
    """
    Constructs a deterministic, accurate summary narrative matching the exact visible returned excerpts.
    Respects collection identity and grounding policy type.
    """
    is_general_static = policy is not None and policy.policy_type == GroundingPolicyType.GENERAL_STATIC
    if collection_name:
        corpus_label = collection_name
    elif domain:
        corpus_label = f"SEBI {domain} corpus"
    else:
        corpus_label = "SEBI regulatory corpus"

    if disposition == AnswerDisposition.CONTROLLED_ABSTENTION:
        if intent == QueryIntent.OUT_OF_SCOPE_OR_UNESTABLISHED:
            return f"Controlled Abstention: The query is outside the demonstrated scope of {corpus_label}."
        if len(historical_superseded_excerpts) > 0 and not verified_excerpts:
            return (
                f"Controlled Abstention: All relevant excerpts in {corpus_label} for this current-compliance query "
                "are SUPERSEDED by subsequent circulars."
            )
        if candidate_count is not None and candidate_count > 0 and not verified_excerpts:
            return (
                f"Controlled Abstention: Candidate passages were retrieved from {corpus_label}, "
                "but failed server-side safety and quotation verification."
            )
        return f"Controlled Abstention: Zero candidate passages found in {corpus_label} for this query."

    if disposition == AnswerDisposition.REJECTED_UNSAFE:
        return "Draft payload rejected: malformed structure, unexpected fields, or duplicate excerpt IDs."

    # For verified/qualified responses:
    v_count = len(verified_excerpts)
    s_count = len(historical_superseded_excerpts)

    if is_general_static:
        return f"Verified Excerpts: {v_count} excerpt(s) verified against authoritative {corpus_label}."

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


@router.get(
    "/collections",
    response_model=List[CollectionSummary],
    status_code=status.HTTP_200_OK,
    summary="List available queryable document collections",
    description="Returns discovery metadata for enabled collections that have an active published snapshot.",
)
async def list_available_collections(
    collection_registry: Optional[CollectionRegistry] = Depends(get_collection_registry),
    retrieval_service: Optional[RetrievalService] = Depends(get_retrieval_service),
) -> List[CollectionSummary]:
    """
    Returns public discovery summaries for registered collections.
    Strictly filters to collections that are enabled AND have an active, verified published snapshot.
    """
    if collection_registry is None or not hasattr(collection_registry, "list_collections"):
        collection_registry = get_collection_registry()
    if retrieval_service is None or not hasattr(retrieval_service, "get_active_snapshot_dir"):
        retrieval_service = get_retrieval_service()

    summaries: List[CollectionSummary] = []
    # Deterministic alphabetical iteration over registered collections
    for manifest in collection_registry.list_collections():
        cid = manifest.collection_id
        status_val = collection_registry.get_collection_status(cid)
        # Exclude disabled or unpublished collections
        if status_val != CollectionStatus.ACTIVE or not manifest.enabled:
            continue

        # Double-check usable published snapshot directory via retrieval_service
        snap_dir = retrieval_service.get_active_snapshot_dir(cid)
        if snap_dir is None or not snap_dir.exists():
            continue

        summaries.append(
            CollectionSummary(
                collection_id=cid,
                name=manifest.name,
                description=manifest.description,
                domain=manifest.domain,
                regulator=manifest.regulator,
                policy_type=manifest.policy.policy_type,
                status=CollectionStatus.ACTIVE,
            )
        )

    return summaries


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
    collection_registry: Optional[CollectionRegistry] = Depends(get_collection_registry),
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
    if collection_registry is None or not hasattr(collection_registry, "has_collection"):
        collection_registry = get_collection_registry()

    # Phase 2.2B.1: Validate and resolve collection identity against CollectionRegistry
    requested_cid = request.collection_id.strip() if request.collection_id else "sebi_cscrf"
    if not collection_registry.has_collection(requested_cid):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Collection '{requested_cid}' is not registered.",
        )

    coll_status = collection_registry.get_collection_status(requested_cid)
    if coll_status == CollectionStatus.DISABLED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Collection '{requested_cid}' is disabled.",
        )
    if coll_status == CollectionStatus.UNPUBLISHED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Collection '{requested_cid}' is unpublished; no active published index snapshot was found.",
        )

    resolved_cid = requested_cid
    try:
        collection_manifest = collection_registry.get_collection(resolved_cid)
        collection_name = collection_manifest.name if collection_manifest else resolved_cid
    except Exception:
        collection_manifest = None
        collection_name = resolved_cid

    # Phase 2.2B.2: Authoritatively resolve collection's grounding policy from validated manifest
    try:
        grounding_policy = collection_registry.get_grounding_policy(resolved_cid)
    except Exception as e:
        logger.error(f"Failed to resolve grounding policy for collection '{resolved_cid}': {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Collection '{resolved_cid}' has an invalid or unresolvable grounding policy.",
        )

    if grounding_policy is None or grounding_policy.policy_type not in (
        GroundingPolicyType.SEBI_CSCRF_STRICT,
        GroundingPolicyType.GENERAL_STATIC,
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Collection '{resolved_cid}' declares an unsupported or invalid grounding policy.",
        )

    # Phase 5.2B: Define collection-aware scope notice based on authoritative policy
    if grounding_policy.policy_type == GroundingPolicyType.GENERAL_STATIC:
        collection_scope_notice = (
            f"Static document collection: excerpts verified verbatim from {collection_name} ({resolved_cid})."
        )
    elif grounding_policy.policy_type == GroundingPolicyType.SEBI_CSCRF_STRICT:
        collection_scope_notice = (
            "Educational & Pilot Assistant: Operating strictly over the 5-document SEBI CSCRF circular corpus "
            "(CIR/2024/113, CIR/2025/045, CIR/2025/060, CIR/2025/096, CIR/2025/119). "
            "Excerpts represent technical document grounding only and do not establish universal legal currency."
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Collection '{resolved_cid}' has an unrecognized grounding policy type.",
        )

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

    # 1. Execute server-side hybrid retrieval with collection dispatch
    try:
        retrieval_response = retrieval_service.search_hybrid(
            query=query,
            top_k=50,
            candidate_k=50,
            rrf_k=60,
            filters=filters,
            collection_id=resolved_cid,
        )
    except Exception as e:
        logger.error(f"Retrieval execution failure for query length {len(query)}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred while searching the regulatory index.",
        )

    # 2. Filter candidates through relevance safeguards
    relevant_results, intent, assessments = filter_relevant_candidates(query, retrieval_response.results)

    # Enforce strict collection ownership on retrieved candidates before evidence assembly
    isolated_relevant_results = []
    service_chunks = None
    if hasattr(retrieval_service, "get_chunks"):
        try:
            res = retrieval_service.get_chunks(resolved_cid)
            if isinstance(res, list):
                service_chunks = res
        except Exception:
            service_chunks = None
    if not isinstance(service_chunks, list):
        service_chunks = getattr(retrieval_service, "chunks", None)
    service_chunk_map = {c.chunk_id: c for c in service_chunks} if isinstance(service_chunks, list) else {}

    service_docs = None
    if hasattr(retrieval_service, "get_documents"):
        try:
            res = retrieval_service.get_documents(resolved_cid)
            if isinstance(res, dict):
                service_docs = res
        except Exception:
            service_docs = None
    if not isinstance(service_docs, dict):
        service_docs = getattr(retrieval_service, "documents", None)
    service_docs = service_docs if isinstance(service_docs, dict) else {}

    for r in relevant_results:
        chunk_coll = getattr(r, "collection_id", None) or (r.metadata.get("collection_id") if hasattr(r, "metadata") and r.metadata else None)
        resolved_chunk_coll = resolve_collection_identity(r.document_id, chunk_coll)
        # Check against loaded collection state chunks and documents if available
        if not resolved_chunk_coll and r.chunk_id in service_chunk_map:
            sc = service_chunk_map[r.chunk_id]
            resolved_chunk_coll = resolve_collection_identity(sc.document_id, getattr(sc, "collection_id", None))
        if not resolved_chunk_coll and r.document_id in service_docs:
            sd = service_docs[r.document_id]
            resolved_chunk_coll = resolve_collection_identity(sd.document_id, getattr(sd, "collection_id", None))

        if resolved_chunk_coll == resolved_cid:
            isolated_relevant_results.append(r)
        else:
            logger.warning(
                "Quarantining candidate chunk '%s' (doc '%s'): collection '%s' does not match requested collection '%s'.",
                r.chunk_id,
                r.document_id,
                resolved_chunk_coll,
                resolved_cid,
            )

    relevant_results = isolated_relevant_results[:top_k]

    corpus_label = f"SEBI {request.domain} corpus" if request.domain else f"{collection_name} ({resolved_cid})"

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
            pilot_corpus_scope_limitation=collection_scope_notice,
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
                f"Insufficient evidence: candidate passages in {corpus_label} do not substantively cover the query topic."
            )
            narrative = (
                f"Controlled Abstention: Insufficient evidence found in {corpus_label} to substantiate an answer for this query."
            )
        else:
            if grounding_policy.policy_type == GroundingPolicyType.GENERAL_STATIC:
                abstention_reason = f"No relevant document passages identified in the registered {corpus_label}."
            else:
                abstention_reason = f"No relevant circular passages identified in the registered {corpus_label}."
            narrative = f"Controlled Abstention: Zero candidate passages found in {corpus_label} for this query."

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
            pilot_corpus_scope_limitation=collection_scope_notice,
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

    # 4. Evaluate through AnswerSafetyGate using validated manifest grounding policy
    try:
        gate_response: SafetyGateResponse = safety_gate.evaluate_draft(
            payload=draft_payload,
            retrieval_candidates=authorized_chunk_ids,
            policy=grounding_policy,
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
        candidate_count=gate_response.retrieval_candidate_count,
        abstention_reason=gate_response.abstention_reason,
        collection_name=f"{collection_name} ({resolved_cid})",
        policy=grounding_policy,
    )

    # Stamp resolved collection_id onto verified and superseded excerpts for provenance traceability
    for exc in gate_response.verified_excerpts:
        if hasattr(exc, "collection_id"):
            exc.collection_id = resolved_cid
    for exc in superseded_to_return:
        if hasattr(exc, "collection_id"):
            exc.collection_id = resolved_cid

    # Phase 2.2B.7 / Phase 5.2B: Apply authoritative collection_scope_notice
    # 7. Execute Grounded Synthesis over verified excerpts with authoritative policy
    # Allowed strictly for validated SEBI_CSCRF_STRICT and GENERAL_STATIC policies.
    policy_str = (
        grounding_policy.policy_type.value
        if hasattr(grounding_policy.policy_type, "value")
        else str(grounding_policy.policy_type)
    )

    synthesis_result = synthesis_service.synthesize_answer(
        query=query,
        verified_excerpts=gate_response.verified_excerpts,
        disposition=gate_response.disposition,
        regulatory_warnings=gate_response.regulatory_warnings,
        domain=request.domain,
        collection_id=resolved_cid,
        policy_type=policy_str,
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
        pilot_corpus_scope_limitation=collection_scope_notice,
        answer_text=synthesis_result.answer,
        synthesis_status=synthesis_result.status.value,
        claim_citations=synthesis_result.claims,
        unresolved_subquestions=synthesis_result.unresolved_subquestions,
        synthesis_failure_reason=synthesis_result.failure_reason,
    )
