"""
Deterministic answer safety gate for the SEBI CSCRF Regulatory Q&A Assistant.

Enforces:
1. Retrieval-bound verification: candidate chunks must belong to authorized server-side retrieval set.
2. Rejection of untrusted model classifications and strict server-side quotation verification.
3. Safe rendering of server-validated excerpts with authoritative corpus metadata.
4. Handling of current vs. historical queries, separating superseded excerpts and abstaining when all evidence is superseded.
5. Conservative disposition assignment (VERIFIED_HISTORICAL_EXCERPTS, QUALIFIED_HISTORICAL_EXCERPTS, CONTROLLED_ABSTENTION, REJECTED_UNSAFE).
6. No leakage of raw untrusted draft text or unchecked model prose.
"""
import re
from typing import List, Dict, Optional, Any, Union, Set
from pathlib import Path

from app.schemas.document import ChunkRecord, DocumentRecord
from app.schemas.grounding import GroundedEvidenceItem, RegulatoryStatus
from app.schemas.graph_collection import GraphEdgeCollection
from app.schemas.cscrf_graph_data import create_verified_cscrf_graph
from app.schemas.retrieval import RetrievalResult, RetrievalResponse
from app.retrieval.service import RetrievalService
from app.grounding.resolver import ConservativeGraphResolver
from app.grounding.validator import CitationValidator
from app.schemas.citation import CitationItem, CitationStatus
from app.schemas.manifest import GroundingPolicyDeclaration, GroundingPolicyType
from app.schemas.safety_gate import (
    AnswerDisposition,
    CandidateExcerptDraft,
    ExcerptDraftPayload,
    ValidatedExcerptItem,
    SafetyGateResponse,
)


def is_current_status_query(query: str) -> bool:
    """
    Conservative detection of queries inquiring about current compliance, active obligations,
    present deadlines, or applicability.
    """
    q_lower = query.lower()
    current_keywords = [
        "current", "currently", "now", "today", "active", "applicable", "deadline",
        "latest", "in force", "compliance timeline", "effective date", "mandated",
        "mandatory", "shall", "must", "timeline", "when do", "is it required"
    ]
    return any(kw in q_lower for kw in current_keywords)


class AnswerSafetyGate:
    """
    Deterministic Safety Gate evaluating untrusted draft excerpts against corpus, retrieval set, and verified graph.
    """

    def __init__(
        self,
        processed_dir: Union[str, Path] = "data/processed",
        graph_collection: Optional[GraphEdgeCollection] = None,
        validator: Optional[CitationValidator] = None,
        resolver: Optional[ConservativeGraphResolver] = None,
        retrieval_service: Optional[RetrievalService] = None,
    ):
        self.processed_dir = Path(processed_dir)
        self.graph = graph_collection if graph_collection is not None else create_verified_cscrf_graph()
        self.validator = validator if validator is not None else CitationValidator(
            processed_dir=self.processed_dir,
            graph_collection=self.graph,
        )
        self.resolver = resolver if resolver is not None else ConservativeGraphResolver(
            graph_collection=self.graph,
        )
        self.retrieval_service = retrieval_service

    def evaluate_draft(
        self,
        payload: Union[ExcerptDraftPayload, Dict[str, Any]],
        retrieval_candidates: Optional[Union[List[RetrievalResult], List[ChunkRecord], Set[str]]] = None,
        policy: Optional[GroundingPolicyDeclaration] = None,
    ) -> SafetyGateResponse:
        """
        Evaluates untrusted draft payload, performs retrieval-candidate binding,
        server-side quotation and status validation, and constructs a deterministic SafetyGateResponse.
        Enforces policy-aware dispatch based on the collection's GroundingPolicyDeclaration.
        """
        active_policy = policy or GroundingPolicyDeclaration(
            policy_type=GroundingPolicyType.SEBI_CSCRF_STRICT,
            enforce_regulatory_graph=True,
            enforce_verbatim_citation=True,
            enforce_retrieval_binding=True,
        )
        # 1. Parse and validate untrusted input payload structure
        if isinstance(payload, dict):
            try:
                payload = ExcerptDraftPayload(**payload)
            except Exception as e:
                return SafetyGateResponse(
                    query=payload.get("query", "") if isinstance(payload, dict) else "",
                    disposition=AnswerDisposition.REJECTED_UNSAFE,
                    verified_excerpts=[],
                    historical_superseded_excerpts=[],
                    summary_narrative="Draft payload rejected: malformed structure, unexpected fields, or duplicate excerpt IDs.",
                    technical_validation_passed=False,
                    retrieval_candidate_count=0,
                    abstention_reason=f"Payload parsing failed: {str(e)}",
                    regulatory_warnings=[],
                )

        query = payload.query.strip()
        if not query:
            return SafetyGateResponse(
                query="",
                disposition=AnswerDisposition.CONTROLLED_ABSTENTION,
                verified_excerpts=[],
                historical_superseded_excerpts=[],
                summary_narrative="Query is empty.",
                technical_validation_passed=False,
                retrieval_candidate_count=0,
                abstention_reason="Empty query provided.",
                regulatory_warnings=[],
            )

        # 2. Determine authorized candidate chunk set from server-side retrieval
        authorized_chunk_ids: Optional[Set[str]] = None
        candidate_count = 0

        if retrieval_candidates is not None:
            if isinstance(retrieval_candidates, set):
                authorized_chunk_ids = retrieval_candidates
            else:
                authorized_chunk_ids = {
                    c.chunk_id for c in retrieval_candidates if hasattr(c, "chunk_id")
                }
            candidate_count = len(authorized_chunk_ids)
        elif self.retrieval_service is not None:
            try:
                # Perform server-side hybrid retrieval for the query
                ret_resp = self.retrieval_service.search_hybrid(query, top_k=10, candidate_k=20)
                authorized_chunk_ids = {r.chunk_id for r in ret_resp.results}
                candidate_count = len(authorized_chunk_ids)
            except Exception as e:
                return SafetyGateResponse(
                    query=query,
                    disposition=AnswerDisposition.CONTROLLED_ABSTENTION,
                    verified_excerpts=[],
                    historical_superseded_excerpts=[],
                    summary_narrative=f"Server-side retrieval failed: {str(e)}",
                    technical_validation_passed=False,
                    retrieval_candidate_count=0,
                    abstention_reason="Server-side retrieval execution error.",
                    regulatory_warnings=[],
                )

        # Fail closed if retrieval was run and returned zero candidate chunks
        if authorized_chunk_ids is not None and len(authorized_chunk_ids) == 0:
            return SafetyGateResponse(
                query=query,
                disposition=AnswerDisposition.CONTROLLED_ABSTENTION,
                verified_excerpts=[],
                historical_superseded_excerpts=[],
                summary_narrative="Controlled Abstention: Zero candidate chunks retrieved from the registered regulatory corpus for this query.",
                technical_validation_passed=False,
                retrieval_candidate_count=0,
                abstention_reason="No candidate passages found in registered corpus for query.",
                regulatory_warnings=[],
            )


        if not payload.excerpts:
            return SafetyGateResponse(
                query=query,
                disposition=AnswerDisposition.CONTROLLED_ABSTENTION,
                verified_excerpts=[],
                historical_superseded_excerpts=[],
                summary_narrative="No candidate excerpts provided in draft payload.",
                technical_validation_passed=False,
                retrieval_candidate_count=candidate_count,
                abstention_reason="No candidate excerpts submitted for verification.",
                regulatory_warnings=[],
            )

        # 3. Server-side excerpt validation
        verified_items: List[ValidatedExcerptItem] = []
        superseded_items: List[ValidatedExcerptItem] = []
        validation_errors: List[str] = []
        warnings: List[str] = []

        is_general_static = (active_policy.policy_type == GroundingPolicyType.GENERAL_STATIC)
        enforce_graph = active_policy.enforce_regulatory_graph and not is_general_static

        for draft in payload.excerpts:
            chunk_id = draft.source_chunk_id.strip()
            proposed_text = draft.proposed_excerpt_text

            # Check candidate authorization against server-side retrieval set
            if active_policy.enforce_retrieval_binding and authorized_chunk_ids is not None and chunk_id not in authorized_chunk_ids:
                validation_errors.append(
                    f"Candidate '{draft.excerpt_id}': Chunk '{chunk_id}' is not in authorized retrieval candidate set."
                )
                continue

            # Check chunk existence in registered corpus or retrieval service
            chunk = self.validator.corpus_chunks.get(chunk_id)
            doc = self.validator.corpus_documents.get(chunk.document_id) if chunk else None

            # Fallback to retrieval service for collection chunks if not loaded in default validator
            if chunk is None and self.retrieval_service is not None:
                # First check across loaded collection states in retrieval_service
                if hasattr(self.retrieval_service, "_collection_states"):
                    for st in self.retrieval_service._collection_states.values():
                        for sc in st.chunks:
                            if sc.chunk_id == chunk_id:
                                chunk = sc
                                doc = st.documents.get(chunk.document_id)
                                break
                        if chunk:
                            break

                # If still not found, check default service chunks
                if chunk is None:
                    service_chunks = getattr(self.retrieval_service, "chunks", None)
                    if service_chunks:
                        for sc in service_chunks:
                            if sc.chunk_id == chunk_id:
                                chunk = sc
                                break
                    if chunk is not None:
                        service_docs = getattr(self.retrieval_service, "documents", None) or {}
                        doc = service_docs.get(chunk.document_id)

            if chunk is None:
                validation_errors.append(f"Candidate '{draft.excerpt_id}': Chunk '{chunk_id}' does not exist in corpus.")
                continue

            # Strict quotation validation using CitationValidator rules (verbatim substring match)
            if active_policy.enforce_verbatim_citation:
                cit = CitationItem(
                    chunk_id=chunk_id,
                    quote=proposed_text,
                )
                # Temporarily ensure chunk is in validator for verbatim check
                added_temp = False
                if chunk_id not in self.validator.corpus_chunks:
                    self.validator.corpus_chunks[chunk_id] = chunk
                    added_temp = True

                try:
                    c_res = self.validator.validate_citation(cit)
                finally:
                    if added_temp:
                        self.validator.corpus_chunks.pop(chunk_id, None)

                if not c_res.is_valid:
                    validation_errors.append(
                        f"Candidate '{draft.excerpt_id}': Proposed text is not a verbatim substring of chunk '{chunk_id}'."
                    )
                    continue

            # Policy dispatch: resolve regulatory status
            if enforce_graph:
                # SEBI CSCRF Strict graph resolution
                temp_result = RetrievalResult(
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    title=doc.title if doc else None,
                    circular_number=doc.circular_number if doc else None,
                    issue_date=doc.issue_date if doc else None,
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                    text=chunk.text,
                    score=1.0,
                    rank=1,
                    is_ocr=chunk.is_ocr,
                )
                grounded_item = self.resolver.resolve_item(temp_result)

                validated_item = ValidatedExcerptItem(
                    excerpt_id=draft.excerpt_id,
                    source_chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    circular_number=doc.circular_number if doc else None,
                    title=doc.title if doc else None,
                    issue_date=doc.issue_date if doc else None,
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                    excerpt_text=proposed_text.strip(),
                    regulatory_status=grounded_item.regulatory_status,
                    status_reason=grounded_item.status_reason,
                    applicable_edge_ids=grounded_item.applicable_edge_ids,
                    acting_document_ids=grounded_item.acting_document_ids,
                )

                if grounded_item.regulatory_status == RegulatoryStatus.UNRESOLVED:
                    warnings.append(
                        f"Excerpt '{draft.excerpt_id}' from {chunk.document_id} has UNRESOLVED status; "
                        f"it represents historical baseline circular text whose active legal currency is not established beyond the pilot."
                    )
                    verified_items.append(validated_item)
                elif grounded_item.regulatory_status == RegulatoryStatus.SUPERSEDED:
                    warnings.append(
                        f"Excerpt '{draft.excerpt_id}' from {chunk.document_id} is SUPERSEDED by newer circulars in the corpus."
                    )
                    superseded_items.append(validated_item)
                elif grounded_item.regulatory_status == RegulatoryStatus.PARTIALLY_AMENDED:
                    warnings.append(
                        f"Excerpt '{draft.excerpt_id}' from {chunk.document_id} belongs to a provision subject to partial amendment."
                    )
                    verified_items.append(validated_item)
                else:
                    verified_items.append(validated_item)
            else:
                # GENERAL_STATIC: General document collection with no SEBI graph or temporal-supersession.
                # Section 3: Do not automatically label general document excerpts as ACTIVE_CURRENT merely
                # because their quotations match the source. Verified quotation establishes textual provenance;
                # it does not establish active legal currency.
                validated_item = ValidatedExcerptItem(
                    excerpt_id=draft.excerpt_id,
                    source_chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    circular_number=getattr(doc, "circular_number", None) if doc else None,
                    title=getattr(doc, "title", None) if doc else None,
                    issue_date=getattr(doc, "issue_date", None) if doc else None,
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                    excerpt_text=proposed_text.strip(),
                    regulatory_status=RegulatoryStatus.INFORMATIONAL,
                    status_reason="Static document excerpt verified verbatim from registered collection; textual provenance established without legal currency evaluation.",
                    applicable_edge_ids=[],
                    acting_document_ids=[],
                )
                verified_items.append(validated_item)

        # Deduplicate warnings
        unique_warnings = list(dict.fromkeys(warnings))

        # 4. Deterministic Disposition Determination
        total_surviving = len(verified_items) + len(superseded_items)
        if total_surviving == 0:
            abstention_msg = (
                "Controlled Abstention: Zero candidate excerpts passed server-side verification. "
                f"Validation errors: {'; '.join(validation_errors)}"
            )
            return SafetyGateResponse(
                query=query,
                disposition=AnswerDisposition.CONTROLLED_ABSTENTION,
                verified_excerpts=[],
                historical_superseded_excerpts=[],
                summary_narrative=abstention_msg,
                technical_validation_passed=False,
                retrieval_candidate_count=candidate_count,
                abstention_reason="All candidate excerpts failed server-side mechanical, retrieval-binding, or quotation verification.",
                regulatory_warnings=unique_warnings,
            )

        if is_general_static:
            # Under GENERAL_STATIC policy, return verified historical excerpts without SEBI supersession separation
            return SafetyGateResponse(
                query=query,
                disposition=AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS,
                verified_excerpts=verified_items,
                historical_superseded_excerpts=[],
                summary_narrative=f"Verified Excerpts: {len(verified_items)} excerpt(s) verified verbatim from registered document collection.",
                technical_validation_passed=True,
                retrieval_candidate_count=candidate_count,
                abstention_reason=None,
                regulatory_warnings=unique_warnings,
            )

        # Handle queries asking about current status / obligations (SEBI-specific)
        asks_current = is_current_status_query(query)

        # If all surviving excerpts are superseded
        if len(verified_items) == 0 and len(superseded_items) > 0:
            if asks_current:
                return SafetyGateResponse(
                    query=query,
                    disposition=AnswerDisposition.CONTROLLED_ABSTENTION,
                    verified_excerpts=[],
                    historical_superseded_excerpts=superseded_items,
                    summary_narrative=(
                        "Controlled Abstention: All relevant excerpts in the corpus for this current-compliance query "
                        "are SUPERSEDED by subsequent circulars."
                    ),
                    technical_validation_passed=True,
                    retrieval_candidate_count=candidate_count,
                    abstention_reason="Query asks for current obligations, but all matching corpus provisions are superseded.",
                    regulatory_warnings=unique_warnings,
                )
            else:
                # Historical query where superseded text is explicitly labeled
                return SafetyGateResponse(
                    query=query,
                    disposition=AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS,
                    verified_excerpts=[],
                    historical_superseded_excerpts=superseded_items,
                    summary_narrative=(
                        f"Historical Excerpts: {len(superseded_items)} excerpt(s) verified from earlier circulars, "
                        f"which are superseded in the current framework."
                    ),
                    technical_validation_passed=True,
                    retrieval_candidate_count=candidate_count,
                    abstention_reason=None,
                    regulatory_warnings=unique_warnings,
                )

        # Check for UNRESOLVED status
        has_unresolved = any(v.regulatory_status == RegulatoryStatus.UNRESOLVED for v in verified_items)

        if not has_unresolved:
            disposition = AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS
            narrative = (
                f"Verified Historical Excerpts: {len(verified_items)} excerpt(s) verified against authoritative SEBI CSCRF corpus "
                f"with fully resolved graph status."
            )
        else:
            disposition = AnswerDisposition.QUALIFIED_HISTORICAL_EXCERPTS
            narrative = (
                f"Qualified Historical Excerpts: {len(verified_items)} excerpt(s) verified as authentic text from SEBI CSCRF circulars. "
                f"Provisions with UNRESOLVED status reflect historical baseline circular content; active legal currency "
                f"is not independently established beyond the 5-document pilot."
            )

        return SafetyGateResponse(
            query=query,
            disposition=disposition,
            verified_excerpts=verified_items,
            historical_superseded_excerpts=superseded_items,
            summary_narrative=narrative,
            technical_validation_passed=True,
            retrieval_candidate_count=candidate_count,
            abstention_reason=None,
            regulatory_warnings=unique_warnings,
        )
