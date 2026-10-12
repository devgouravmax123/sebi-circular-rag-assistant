"""
Conservative graph resolver for regulatory evidence grounding in SEBI CSCRF.

Resolves retrieved candidate passages against verified relationship graph edges:
- Preserves 100% of original retrieved passages and metadata.
- Distinguishes AMENDS, RESCINDS, and REFERENCES.
- Applies partial amendment scope strictly when deterministic provision mapping matches.
- Never infers supersession from REFERENCES edges.
- Avoids whole-document/chunk supersession when scope is partial.
- Leaves ambiguous, conflicting, or missing provision mappings as UNRESOLVED with explicit rationale.
- Assigns ACTIVE_CURRENT only when an explicit, verified relationship or baseline rule is satisfied.
"""
import re
from typing import List, Dict, Set, Optional, Tuple, Any
from app.schemas.retrieval import RetrievalResult, RetrievalResponse
from app.schemas.graph import GraphEdge, RelationshipType, ScopeType, ReviewStatus
from app.schemas.graph_collection import GraphEdgeCollection
from app.schemas.grounding import (
    GroundedEvidenceItem,
    GroundedEvidencePackage,
    RegulatoryStatus,
    ProvisionMapping,
)


# Canonical provision identifiers and deterministic detection patterns
# Uses structural anchors (e.g. section headers, clause prefixes, or specific operative phrases)
PROVISION_PATTERNS: Dict[str, Dict[str, Any]] = {
    # CIR/2024/113 Provisions
    "ISO_27001": {
        "canonical_id": "Section 4.3 / ISO 27001",
        "patterns": [
            r"(?:\b4\.2\.?\s+iso\s+audit|\bsection\s+4\.2\b.*iso\s+27001|\bpr\.ip\.s16\b\s*(?:1\.|:|\n)|\bshall\s+obtain\s+iso\s*27001\b|\biso\s*27001\s+certification\s+shall\s+be\s+mandatory\b|\biso\s*27001\s+certification\s+mandatory\b|\bencouraged\s+and\s+recommended\s*\(not\s+mandatory\)\s*to\s+obtain\s+iso\s*27001\b)",
        ],
        "graph_labels": ["Section 4.2 & PR.IP.S16 (ISO 27001 Recommendation / Clause 6.11)", "PR.IP.S16"],
    },
    "CISO_REPORTING": {
        "canonical_id": "Annexure-J / GV.GO.S1",
        "patterns": [
            r"(?:\bgv\.go\.s1\b|\breporting\s+of\s+the\s+ciso\s+of\s+the\s+mii\b|\bciso\s+shall\s+report\s+directly\s+to\b|\bciso\s+of\s+the\s+mii\s+and\s+qualified\s+res\s+shall\s+be\s+directly\s+to\s+the\s+md\b)",
        ],
        "graph_labels": ["Annexure-J / GV.GO.S1", "GV.GO.S1"],
    },
    "RTO_RPO": {
        "canonical_id": "Annexure-J / RC.RP.S2",
        "patterns": [
            r"(?:\brc\.rp\.s2\b|\btwo[\s-]hour\s+rto\b|\b15[\s-]minute\s+rpo\b|\bresumption\s+within\s+two\s+hours\b)",
        ],
        "graph_labels": ["RC.RP.S2 (Two-Hour RTO & 15-min RPO / Clause 6.10)", "RC.RP.S2"],
    },
    "CLOUD_HSM": {
        "canonical_id": "Annexure-J / Clause 5 (HSM)",
        "patterns": [
            r"(?:\bclause\s+5\b.*hardware\s+security\s+module|\bdedicated\s+hardware\s+security\s+module\s*\(hsm\)|\bdedicated\s+hsm\b|\balternative\s+of\s+hsm\b)",
        ],
        "graph_labels": ["Annexure-J / Clause 5 (Cloud Hardware Security Module)", "Clause 5"],
    },
    "VIRTUAL_PATCHING_LEGACY": {
        "canonical_id": "Annexure-J / PR.PT.S3",
        "patterns": [
            r"(?:\bpr\.pt\.s3\b|\bvirtual\s+patching\s+shall\s+be\s+implemented\s+for\s+legacy\s+systems\b|\bvirtual\s+patching\b.*legacy\s+systems\b.*6\s+months\b)",
        ],
        "graph_labels": ["PR.PT.S3", "Patch Management policy PR.MA.S3"],
    },
    "ZERO_TRUST": {
        "canonical_id": "Annexure-J / PR.AA.S4-S5",
        "patterns": [
            r"(?:\bpr\.aa\.s4\b|\bpr\.aa\.s5\b|\bzero[\s-]trust\s+networks\b|\bzero[\s-]trust\s+architecture\b)",
        ],
        "graph_labels": ["PR.AA.S4 & PR.AA.S5 (Zero-Trust Security / Clause 6.2)"],
    },
    "MOBILE_APP_SECURITY": {
        "canonical_id": "Annexure-J / PR.AA.S16",
        "patterns": [
            r"(?:\bpr\.aa\.s16\b|\bmobile\s+application\s+security\b|\bmobile\s+application\s+developed\s+by\s+res\b)",
        ],
        "graph_labels": ["PR.AA.S16 (Mobile Application Security / Clause 6.3)"],
    },
    "TIMELINE_COMPLIANCE": {
        "canonical_id": "Paragraph 17 / Timelines",
        "patterns": [
            r"(?:\bparagraph\s+17\b|\bpara\s+17\b|\bwhere\s+cscrf\s+is\s+being\s+issued\s+for\s+the\s+first\s+time\s+–\s+by\s+april\s+01,\s+2025\b|\bextend\s+the\s+compliance\s+timelines\s+by\s+three\s*\(3\)\s*months\b|\bextend\s+the\s+compliance\s+timelines\s+by\s+two\s*\(2\)\s*months\b|\btill\s+june\s+30,\s+2025\b|\btill\s+august\s+31,\s+2025\b)",
        ],
        "graph_labels": ["Paragraph 17", "Implementation Period", "Compliance Timelines", "Paragraph 2"],
    },

    # CIR/2025/060 Entity Provisions
    "STOCK_BROKERS_CAT": {
        "canonical_id": "CIR/2025/060 Section 2.1 (Stock Brokers)",
        "patterns": [
            r"(?:\bsection\s+2\.1\b.*stock\s+brokers|\btable\s+1:\s*criteria\s+and\s+thresholds\s+for\s+stock\s+brokers\b)",
        ],
        "graph_labels": ["Section 2.1 (Stock Brokers / Table 1)"],
    },
    "IA_RA_EXEMPTION": {
        "canonical_id": "CIR/2025/060 Section 2.3-2.4 (IAs / RAs)",
        "patterns": [
            r"(?:\bsection\s+2\.3\b.*investment\s+advisers|\bsection\s+2\.4\b.*research\s+analysts|\btable\s+3:\s*criteria\s+and\s+thresholds\s+for\s+ias\b)",
        ],
        "graph_labels": ["Section 2.3 (Investment Advisers)", "Section 2.4 (Research Analysts)"],
    },
    "KRA_RECATEGORIZATION": {
        "canonical_id": "CIR/2025/060 Section 2.5 (KRAs)",
        "patterns": [
            r"(?:\bsection\s+2\.5\b.*kyc\s+registration\s+agencies|\bkras\s+shall\s+be\s+re-categorised\s+as\s+qualified\s+res\b)",
        ],
        "graph_labels": ["Section 2.5 (KYC Registration Agencies)"],
    },
    "PORTFOLIO_MANAGERS_CAT": {
        "canonical_id": "Portfolio Managers Categorization",
        "patterns": [
            r"(?:\bsection\s+7\.1\b|\btable\s+1:\s*criteria\s+and\s+thresholds\s+for\s+portfolio\s+managers\b|\btable\s+3:\s*criteria\s+and\s+thresholds\s+for\s+portfolio\s+managers\b)",
        ],
        "graph_labels": ["Section 7.1 (Table 1: Portfolio Managers)", "Table 3: Criteria and thresholds for Portfolio Managers"],
    },
    "MERCHANT_BANKERS_CAT": {
        "canonical_id": "Merchant Bankers Categorization",
        "patterns": [
            r"(?:\bsection\s+7\.2\b|\btable\s+2:\s*criteria\s+and\s+thresholds\s+for\s+mbs\b|\btable\s+5:\s*criteria\s+and\s+thresholds\s+for\s+mbs\b)",
        ],
        "graph_labels": ["Section 7.2 (Table 2: Merchant Bankers)", "Table 5: Criteria and thresholds for MBs"],
    },
}


def detect_provisions_in_text(text: str) -> List[ProvisionMapping]:
    """
    Deterministic regex pattern matching to detect canonical regulatory provisions in chunk text.
    """
    text_lower = text.lower()
    matched_provisions: List[ProvisionMapping] = []

    for key, info in PROVISION_PATTERNS.items():
        for pat in info["patterns"]:
            match = re.search(pat, text_lower)
            if match:
                matched_provisions.append(
                    ProvisionMapping(
                        provision_id=info["canonical_id"],
                        provision_title=key,
                        matched_term=match.group(0),
                    )
                )
                break  # Avoid duplicate entries for same provision

    return matched_provisions


def check_edge_affects_provision(edge: GraphEdge, provisions: List[ProvisionMapping]) -> Tuple[bool, Optional[str]]:
    """
    Check if a graph edge's affected_provisions explicitly overlaps with any detected provision.
    """
    if edge.scope == ScopeType.FULL:
        return True, "Full document scope"

    if not provisions:
        return False, None

    # Check each detected provision against edge's affected provisions list
    for prov in provisions:
        prov_id_lower = prov.provision_id.lower()
        for aff in edge.affected_provisions:
            aff_lower = aff.lower()
            # Match canonical ID or substring in graph affected provisions
            if aff_lower in prov_id_lower or prov_id_lower in aff_lower:
                return True, aff
            # Check pattern registry graph labels
            for key, info in PROVISION_PATTERNS.items():
                if info["canonical_id"] == prov.provision_id:
                    for gl in info["graph_labels"]:
                        if gl.lower() in aff_lower or aff_lower in gl.lower():
                            return True, aff

    return False, None


class ConservativeGraphResolver:
    """
    Conservative Graph Resolver evaluating retrieval candidate passages against verified regulatory edges.
    """

    def __init__(self, graph_collection: GraphEdgeCollection):
        self.graph = graph_collection

    def resolve_item(self, item: RetrievalResult) -> GroundedEvidenceItem:
        """
        Resolve regulatory status of a single retrieved passage item.
        """
        doc_id = item.document_id
        text = item.text

        # 1. Detect provisions deterministically
        detected_provisions = detect_provisions_in_text(text)

        # 2. Inspect incoming edges (newer circulars acting on this doc)
        incoming_edges = self.graph.get_incoming_edges(doc_id)

        # Filter verified edges - only VERIFIED edges ever influence regulatory status
        verified_edges = [e for e in incoming_edges if e.review_status == ReviewStatus.VERIFIED]

        # Categorize edges by relationship type
        amending_edges = [
            e for e in verified_edges
            if e.relationship in (
                RelationshipType.AMENDS,
                RelationshipType.SUPERSEDES,
                RelationshipType.REPLACES,
                RelationshipType.RESCINDS,
            )
        ]
        informational_edges = [
            e for e in verified_edges
            if e.relationship in (RelationshipType.REFERENCES, RelationshipType.CLARIFIES)
        ]

        # Default state
        status = RegulatoryStatus.UNRESOLVED
        status_reason = "No applicable amendment or supersession graph edge identified for this document."
        applicable_edge_ids: List[str] = []
        acting_doc_ids: List[str] = []

        # Check full-scope supersession / rescission
        full_amendments = [e for e in amending_edges if e.scope == ScopeType.FULL]
        if full_amendments:
            acting_sources = {e.source_document_id for e in full_amendments}
            acting_relationships = {e.relationship for e in full_amendments}
            # If multiple full-scope edges exist from conflicting source documents or conflicting relationship types, mark UNRESOLVED
            if len(acting_sources) > 1 or len(acting_relationships) > 1:
                status = RegulatoryStatus.UNRESOLVED
                status_reason = (
                    f"Conflicting verified full-scope relationship edges detected for '{doc_id}' from multiple source circulars "
                    f"({list(acting_sources)}); unresolved outcome applied conservatively."
                )
                applicable_edge_ids.extend([e.edge_id for e in full_amendments])
                acting_doc_ids.extend(list(acting_sources))
            else:
                acting_edge = full_amendments[0]
                status = RegulatoryStatus.SUPERSEDED
                status_reason = (
                    f"Entire document '{doc_id}' is superseded/rescinded by '{acting_edge.source_document_id}' "
                    f"via verified {acting_edge.relationship.value} edge '{acting_edge.edge_id}'."
                )
                applicable_edge_ids.append(acting_edge.edge_id)
                acting_doc_ids.append(acting_edge.source_document_id)

            return GroundedEvidenceItem(
                chunk_id=item.chunk_id,
                document_id=item.document_id,
                title=item.title,
                circular_number=item.circular_number,
                issue_date=item.issue_date,
                page_start=item.page_start,
                page_end=item.page_end,
                text=item.text,
                retrieval_score=item.score,
                retrieval_rank=item.rank,
                is_ocr=item.is_ocr,
                regulatory_status=status,
                status_reason=status_reason,
                mapped_provisions=detected_provisions,
                applicable_edge_ids=applicable_edge_ids,
                acting_document_ids=acting_doc_ids,
                metadata=item.metadata,
            )

        # Check partial amendments
        partial_amendments = [e for e in amending_edges if e.scope == ScopeType.PARTIAL]
        matched_amendment: Optional[Tuple[GraphEdge, str]] = None

        for edge in partial_amendments:
            is_affected, matched_aff_prov = check_edge_affects_provision(edge, detected_provisions)
            if is_affected:
                matched_amendment = (edge, matched_aff_prov or "")
                break

        if matched_amendment:
            acting_edge, aff_prov = matched_amendment
            status = RegulatoryStatus.PARTIALLY_AMENDED
            status_reason = (
                f"Provision mapped to '{aff_prov}' is partially amended by '{acting_edge.source_document_id}' "
                f"via verified {acting_edge.relationship.value} edge '{acting_edge.edge_id}'."
            )
            applicable_edge_ids.append(acting_edge.edge_id)
            acting_doc_ids.append(acting_edge.source_document_id)

        elif informational_edges and not amending_edges:
            # Referenced / Clarified informational context
            acting_edge = informational_edges[0]
            status = RegulatoryStatus.INFORMATIONAL
            status_reason = (
                f"Document '{doc_id}' is referenced/clarified by '{acting_edge.source_document_id}' "
                f"via non-superseding {acting_edge.relationship.value} edge '{acting_edge.edge_id}'."
            )
            applicable_edge_ids.append(acting_edge.edge_id)
            acting_doc_ids.append(acting_edge.source_document_id)

        else:
            # Conservative default: Do NOT infer ACTIVE_CURRENT merely because circular is newest or has no amendment in 5-doc pilot corpus
            if detected_provisions:
                status = RegulatoryStatus.UNRESOLVED
                status_reason = (
                    f"Passage mapped to provisions ({[p.provision_id for p in detected_provisions]}) in '{doc_id}'. "
                    f"No verified amendment edge applies in the 5-document pilot corpus; "
                    f"current legal validity is not independently established beyond the corpus."
                )
            else:
                status = RegulatoryStatus.UNRESOLVED
                status_reason = (
                    f"Passage in '{doc_id}' could not be unambiguously mapped to a specific verified provision, "
                    f"and no full-scope supersession or reference edge applies."
                )

        return GroundedEvidenceItem(
            chunk_id=item.chunk_id,
            document_id=item.document_id,
            title=item.title,
            circular_number=item.circular_number,
            issue_date=item.issue_date,
            page_start=item.page_start,
            page_end=item.page_end,
            text=item.text,
            retrieval_score=item.score,
            retrieval_rank=item.rank,
            is_ocr=item.is_ocr,
            regulatory_status=status,
            status_reason=status_reason,
            mapped_provisions=detected_provisions,
            applicable_edge_ids=applicable_edge_ids,
            acting_document_ids=acting_doc_ids,
            metadata=item.metadata,
        )

    def resolve_package(self, response: RetrievalResponse) -> GroundedEvidencePackage:
        """
        Resolve a full RetrievalResponse collection of passages into a GroundedEvidencePackage.
        """
        resolved_items = [self.resolve_item(r) for r in response.results]

        active_count = sum(1 for item in resolved_items if item.regulatory_status == RegulatoryStatus.ACTIVE_CURRENT)
        partially_amended_count = sum(1 for item in resolved_items if item.regulatory_status == RegulatoryStatus.PARTIALLY_AMENDED)
        superseded_count = sum(1 for item in resolved_items if item.regulatory_status == RegulatoryStatus.SUPERSEDED)
        unresolved_count = sum(1 for item in resolved_items if item.regulatory_status == RegulatoryStatus.UNRESOLVED)
        informational_count = sum(1 for item in resolved_items if item.regulatory_status == RegulatoryStatus.INFORMATIONAL)

        return GroundedEvidencePackage(
            query=response.query,
            items=resolved_items,
            total_retrieved=len(resolved_items),
            active_count=active_count,
            partially_amended_count=partially_amended_count,
            superseded_count=superseded_count,
            unresolved_count=unresolved_count,
            informational_count=informational_count,
        )
