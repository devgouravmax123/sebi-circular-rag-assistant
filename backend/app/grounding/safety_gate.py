"""
Answer Safety Gate for Evidence Verification and Controlled Abstention.

Enforces server-side verification:
1. Strict retrieval-candidate binding (cited chunks must be in server-side retrieval set).
2. Verbatim quotation validation (candidate text must be an exact substring of chunk).
3. Pluggable relationship resolution (RegulatorySupersessionPolicy vs PassthroughPolicy).
4. Pluggable temporal applicability evaluation (RegulatoryTemporalPolicy vs PassthroughTemporalPolicy).
5. Controlled abstention on unanswerable queries or superseded evidence without current support.
"""
from typing import Dict, List, Optional, Set, Any, Union
from pathlib import Path
import json
import logging
import re

from app.schemas.document import ChunkRecord, DocumentRecord
from app.schemas.retrieval import RetrievalResult, RetrievalResponse
from app.schemas.citation import CitationItem, CitationStatus
from app.schemas.graph_collection import GraphEdgeCollection
from app.schemas.grounding import RegulatoryStatus
from app.schemas.safety_gate import (
    CandidateExcerptDraft,
    ExcerptDraftPayload,
    ValidatedExcerptItem,
    SafetyGateResponse,
    AnswerDisposition,
)
from app.grounding.validator import CitationValidator
from app.grounding.resolver import ConservativeGraphResolver
from app.schemas.manifest import GroundingPolicyDeclaration, GroundingPolicyType
from app.retrieval.service import RetrievalService
from app.schemas.cscrf_graph_data import create_verified_cscrf_graph
from app.grounding.relationship_policy import (
    resolve_relationship_and_temporal_policies,
    is_current_status_query,
    RelationshipPolicy,
    TemporalPolicy,
)

logger = logging.getLogger(__name__)

_OMITTED_POLICY = object()


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
        self.retrieval_service = retrieval_service
        self._graph = graph_collection
        self._resolver = resolver
        self.validator = validator if validator is not None else CitationValidator(
            processed_dir=self.processed_dir,
            graph_collection=self._graph,
        )

    @property
    def graph(self) -> GraphEdgeCollection:
        """Lazily load verified CSCRF graph if not provided."""
        if self._graph is None:
            self._graph = create_verified_cscrf_graph()
        return self._graph

    @property
    def resolver(self) -> ConservativeGraphResolver:
        """Lazily load conservative graph resolver if not provided."""
        if self._resolver is None:
            self._resolver = ConservativeGraphResolver(graph_collection=self.graph)
        return self._resolver

    def evaluate_draft(
        self,
        payload: Union[ExcerptDraftPayload, Dict[str, Any]],
        retrieval_candidates: Optional[Union[List[RetrievalResult], List[ChunkRecord], Set[str]]] = None,
        policy: Union[GroundingPolicyDeclaration, Any] = _OMITTED_POLICY,
        collection_id: Optional[str] = None,
    ) -> SafetyGateResponse:
        """
        Evaluates untrusted draft payload, performs retrieval-candidate binding,
        server-side quotation and status validation, and constructs a deterministic SafetyGateResponse.
        Enforces policy-aware dispatch based on the collection's GroundingPolicyDeclaration.
        Fails closed if policy is omitted or None.
        """
        if policy is _OMITTED_POLICY or policy is None:
            raise ValueError("grounding policy declaration is required and cannot be omitted or None")

        if not isinstance(policy, GroundingPolicyDeclaration):
            raise ValueError(f"Invalid policy declaration type: {type(policy).__name__}")

        # Resolve authoritative collection ID for policy binding
        cid = (
            collection_id.strip()
            if collection_id
            else ("sebi_cscrf" if policy.policy_type == GroundingPolicyType.SEBI_CSCRF_STRICT else "general_static")
        )

        rel_policy, temp_policy = resolve_relationship_and_temporal_policies(
            policy=policy,
            collection_id=cid,
            graph_collection=self._graph,
            resolver=self._resolver,
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
                disposition=AnswerDisposition.REJECTED_UNSAFE,
                verified_excerpts=[],
                historical_superseded_excerpts=[],
                summary_narrative="Draft payload rejected: query cannot be empty.",
                technical_validation_passed=False,
                retrieval_candidate_count=0,
                abstention_reason="Empty query in draft payload.",
                regulatory_warnings=[],
            )

        # Check for duplicate excerpt IDs
        seen_excerpt_ids: Set[str] = set()
        for draft in payload.excerpts:
            if draft.excerpt_id in seen_excerpt_ids:
                return SafetyGateResponse(
                    query=query,
                    disposition=AnswerDisposition.REJECTED_UNSAFE,
                    verified_excerpts=[],
                    historical_superseded_excerpts=[],
                    summary_narrative=f"Draft payload rejected: duplicate excerpt_id '{draft.excerpt_id}' detected.",
                    technical_validation_passed=False,
                    retrieval_candidate_count=0,
                    abstention_reason=f"Duplicate excerpt_id '{draft.excerpt_id}'.",
                    regulatory_warnings=[],
                )
            seen_excerpt_ids.add(draft.excerpt_id)

        # 2. Extract and index authorized candidate chunk IDs
        authorized_chunk_ids: Optional[Set[str]] = None
        candidate_count = 0
        if retrieval_candidates is not None:
            if isinstance(retrieval_candidates, set):
                authorized_chunk_ids = retrieval_candidates
            elif isinstance(retrieval_candidates, list):
                authorized_chunk_ids = {
                    c if isinstance(c, str) else c.chunk_id
                    for c in retrieval_candidates
                    if isinstance(c, str) or hasattr(c, "chunk_id")
                }
            candidate_count = len(authorized_chunk_ids) if authorized_chunk_ids else 0
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

        # If draft payload has zero excerpts: controlled abstention
        if not payload.excerpts:
            return SafetyGateResponse(
                query=query,
                disposition=AnswerDisposition.CONTROLLED_ABSTENTION,
                verified_excerpts=[],
                historical_superseded_excerpts=[],
                summary_narrative="Controlled Abstention: No candidate excerpts provided in draft payload.",
                technical_validation_passed=True,
                retrieval_candidate_count=candidate_count,
                abstention_reason="Model draft contained no candidate excerpts to verify.",
                regulatory_warnings=[],
            )

        # 3. Server-side excerpt validation
        verified_items: List[ValidatedExcerptItem] = []
        superseded_items: List[ValidatedExcerptItem] = []
        validation_errors: List[str] = []
        warnings: List[str] = []

        for draft in payload.excerpts:
            chunk_id = draft.source_chunk_id.strip()
            proposed_text = draft.proposed_excerpt_text

            # Check candidate authorization against server-side retrieval set
            if policy.enforce_retrieval_binding and authorized_chunk_ids is not None and chunk_id not in authorized_chunk_ids:
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
            if policy.enforce_verbatim_citation:
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

            # Policy dispatch: resolve regulatory status using pluggable RelationshipPolicy
            temp_result = RetrievalResult(
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                title=doc.title if doc else None,
                circular_number=getattr(doc, "circular_number", None) if doc else None,
                issue_date=getattr(doc, "issue_date", None) if doc else None,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                text=chunk.text,
                score=1.0,
                rank=1,
                is_ocr=chunk.is_ocr,
            )
            grounded_item = rel_policy.resolve_item(temp_result)

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

        # Delegate disposition evaluation to pluggable TemporalPolicy
        return temp_policy.evaluate_disposition(
            query=query,
            verified_items=verified_items,
            superseded_items=superseded_items,
            candidate_count=candidate_count,
            warnings=unique_warnings,
        )
