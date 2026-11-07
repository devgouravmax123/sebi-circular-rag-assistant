"""
Pluggable Relationship and Temporal Policy Architecture (Phase 3.3 / ADR-017).

Decouples regulatory supersession and temporal applicability from core claim grounding and citation verification:
1. RelationshipPolicy (ABC) with:
   - RegulatorySupersessionPolicy: Evaluates AMENDS, RESCINDS, and REFERENCES edges via ConservativeGraphResolver. Strictly collection-bound.
   - PassthroughPolicy: Pure static document passthrough without graph loading, provision parsing, or supersession traversal.
2. TemporalPolicy (ABC) with:
   - RegulatoryTemporalPolicy: Evaluates current-status queries and supersession disposition rules for regulatory collections.
   - PassthroughTemporalPolicy: Neutral disposition evaluation without regulatory abstention or supersession filtering.
3. Authoritative Policy Factory:
   - resolve_relationship_and_temporal_policies(policy, collection_id, graph_collection=None, resolver=None)
"""
from abc import ABC, abstractmethod
from typing import List, Optional, Set, Tuple, Union, Dict, Any
import logging

from app.schemas.manifest import GroundingPolicyDeclaration, GroundingPolicyType
from app.schemas.retrieval import RetrievalResult
from app.schemas.grounding import GroundedEvidenceItem, RegulatoryStatus
from app.schemas.graph_collection import GraphEdgeCollection
from app.schemas.safety_gate import ValidatedExcerptItem, AnswerDisposition, SafetyGateResponse
from app.grounding.resolver import ConservativeGraphResolver
from app.schemas.cscrf_graph_data import create_verified_cscrf_graph

logger = logging.getLogger(__name__)


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


# -----------------------------------------------------------------------------
# 1. Relationship Policy Interfaces & Implementations
# -----------------------------------------------------------------------------

class RelationshipPolicy(ABC):
    """Abstract base class for collection-scoped relationship and supersession resolution."""

    @property
    @abstractmethod
    def policy_name(self) -> str:
        """Human-readable policy name."""
        pass

    @property
    @abstractmethod
    def is_graph_active(self) -> bool:
        """Whether this policy actively resolves regulatory graph edges."""
        pass

    @abstractmethod
    def resolve_item(self, item: RetrievalResult) -> GroundedEvidenceItem:
        """Resolve regulatory status, applicable edges, and status reason for a retrieved passage item."""
        pass


class RegulatorySupersessionPolicy(RelationshipPolicy):
    """
    Evaluates regulatory relationship graph edges (AMENDS, RESCINDS, REFERENCES)
    using the ConservativeGraphResolver. Strictly bound to authorized regulatory collections.
    """

    def __init__(
        self,
        collection_id: str,
        graph_collection: Optional[GraphEdgeCollection] = None,
        resolver: Optional[ConservativeGraphResolver] = None,
    ):
        self.collection_id = collection_id.strip().lower()
        if self.collection_id != "sebi_cscrf":
            raise ValueError(
                f"RegulatorySupersessionPolicy is not authorized for collection '{collection_id}'. "
                f"Currently only 'sebi_cscrf' has an authorized regulatory graph dataset."
            )

        if resolver is not None:
            self._resolver = resolver
            self._graph = resolver.graph
        else:
            self._graph = graph_collection if graph_collection is not None else create_verified_cscrf_graph()
            self._resolver = ConservativeGraphResolver(graph_collection=self._graph)

    @property
    def policy_name(self) -> str:
        return "RegulatorySupersessionPolicy"

    @property
    def is_graph_active(self) -> bool:
        return True

    @property
    def graph(self) -> GraphEdgeCollection:
        return self._graph

    @property
    def resolver(self) -> ConservativeGraphResolver:
        return self._resolver

    def resolve_item(self, item: RetrievalResult) -> GroundedEvidenceItem:
        return self._resolver.resolve_item(item)


class PassthroughPolicy(RelationshipPolicy):
    """
    Passthrough relationship policy for general, static document collections (e.g. cisa_playbooks).
    Does not instantiate, load, query, or traverse any regulatory relationship graph.
    Assigns RegulatoryStatus.INFORMATIONAL without legal currency assertions.
    """

    def __init__(self, collection_id: Optional[str] = None):
        self.collection_id = collection_id.strip().lower() if collection_id else None

    @property
    def policy_name(self) -> str:
        return "PassthroughPolicy"

    @property
    def is_graph_active(self) -> bool:
        return False

    def resolve_item(self, item: RetrievalResult) -> GroundedEvidenceItem:
        return GroundedEvidenceItem(
            chunk_id=item.chunk_id,
            document_id=item.document_id,
            title=item.title,
            circular_number=getattr(item, "circular_number", None),
            issue_date=getattr(item, "issue_date", None),
            page_start=item.page_start,
            page_end=item.page_end,
            text=item.text,
            retrieval_score=item.score,
            retrieval_rank=item.rank if item.rank and item.rank >= 1 else 1,
            is_ocr=item.is_ocr,
            regulatory_status=RegulatoryStatus.INFORMATIONAL,
            status_reason="Static document excerpt verified verbatim from registered collection; textual provenance established without legal currency evaluation.",
            applicable_edge_ids=[],
            acting_document_ids=[],
            detected_provisions=[],
        )


# -----------------------------------------------------------------------------
# 2. Temporal Policy Interfaces & Implementations
# -----------------------------------------------------------------------------

class TemporalPolicy(ABC):
    """Abstract base class for collection-scoped temporal evaluation and disposition determination."""

    @property
    @abstractmethod
    def policy_name(self) -> str:
        pass

    @abstractmethod
    def evaluate_disposition(
        self,
        query: str,
        verified_items: List[ValidatedExcerptItem],
        superseded_items: List[ValidatedExcerptItem],
        candidate_count: int,
        warnings: List[str],
        corpus_label: Optional[str] = None,
    ) -> SafetyGateResponse:
        """Determines final SafetyGateResponse disposition, narrative, and abstention reasons."""
        pass


class RegulatoryTemporalPolicy(TemporalPolicy):
    """
    Evaluates regulatory temporal intent (e.g., current compliance deadlines / active obligations)
    against verified and superseded excerpt partitions for regulatory collections.
    """

    def __init__(self, collection_id: str = "sebi_cscrf"):
        self.collection_id = collection_id.strip().lower()

    @property
    def policy_name(self) -> str:
        return "RegulatoryTemporalPolicy"

    def evaluate_disposition(
        self,
        query: str,
        verified_items: List[ValidatedExcerptItem],
        superseded_items: List[ValidatedExcerptItem],
        candidate_count: int,
        warnings: List[str],
        corpus_label: Optional[str] = None,
    ) -> SafetyGateResponse:
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
                    regulatory_warnings=warnings,
                )
            else:
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
                    regulatory_warnings=warnings,
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
            regulatory_warnings=warnings,
        )


class PassthroughTemporalPolicy(TemporalPolicy):
    """
    Passthrough temporal policy for general, static document collections.
    Does not evaluate regulatory currency, active in-force deadlines, or supersession.
    Current-status keywords in queries never trigger regulatory supersession abstention.
    """

    def __init__(self, collection_id: Optional[str] = None):
        self.collection_id = collection_id.strip().lower() if collection_id else None

    @property
    def policy_name(self) -> str:
        return "PassthroughTemporalPolicy"

    def evaluate_disposition(
        self,
        query: str,
        verified_items: List[ValidatedExcerptItem],
        superseded_items: List[ValidatedExcerptItem],
        candidate_count: int,
        warnings: List[str],
        corpus_label: Optional[str] = None,
    ) -> SafetyGateResponse:
        label = corpus_label or "registered document collection"
        return SafetyGateResponse(
            query=query,
            disposition=AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS,
            verified_excerpts=verified_items,
            historical_superseded_excerpts=[],
            summary_narrative=f"Verified Excerpts: {len(verified_items)} excerpt(s) verified verbatim from {label}.",
            technical_validation_passed=True,
            retrieval_candidate_count=candidate_count,
            abstention_reason=None,
            regulatory_warnings=warnings,
        )


# -----------------------------------------------------------------------------
# 3. Authoritative Policy Factory
# -----------------------------------------------------------------------------

def resolve_relationship_and_temporal_policies(
    policy: GroundingPolicyDeclaration,
    collection_id: str,
    graph_collection: Optional[GraphEdgeCollection] = None,
    resolver: Optional[ConservativeGraphResolver] = None,
) -> Tuple[RelationshipPolicy, TemporalPolicy]:
    """
    Server-side authoritative factory resolving RelationshipPolicy and TemporalPolicy.
    Fails closed on missing, unsupported, or contradictory policy declarations.
    """
    if policy is None or not isinstance(policy, GroundingPolicyDeclaration):
        raise ValueError(
            "grounding policy declaration is required and must be an instance of GroundingPolicyDeclaration"
        )

    cid = (collection_id or "").strip().lower()
    if not cid:
        raise ValueError("collection_id must be a non-empty string for policy resolution")

    if policy.policy_type == GroundingPolicyType.SEBI_CSCRF_STRICT:
        if not policy.enforce_regulatory_graph:
            raise ValueError(
                f"Contradictory policy configuration: policy_type '{policy.policy_type.value}' "
                f"requires enforce_regulatory_graph=True"
            )
        rel_policy = RegulatorySupersessionPolicy(
            collection_id=cid,
            graph_collection=graph_collection,
            resolver=resolver,
        )
        temp_policy = RegulatoryTemporalPolicy(collection_id=cid)
        return rel_policy, temp_policy

    elif policy.policy_type == GroundingPolicyType.GENERAL_STATIC:
        if policy.enforce_regulatory_graph:
            raise ValueError(
                f"Contradictory policy configuration: policy_type '{policy.policy_type.value}' "
                f"requires enforce_regulatory_graph=False"
            )
        rel_policy = PassthroughPolicy(collection_id=cid)
        temp_policy = PassthroughTemporalPolicy(collection_id=cid)
        return rel_policy, temp_policy

    else:
        raise ValueError(
            f"Unsupported or unrecognized grounding policy_type: '{getattr(policy, 'policy_type', policy)}'. "
            f"Must be one of {[p.value for p in GroundingPolicyType]}"
        )
