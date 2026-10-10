"""
Consolidated and verified evidence-grounded graph dataset for the SEBI CSCRF circular corpus.

Built strictly from verified textual evidence in docs/cscrf-relationship-audit.md.
All edges strictly preserve:
- Direction: Newer / Acting (`source_document_id`) -> RELATIONSHIP -> Older / Affected (`target_document_id`)
- Provision-level scoping for partial amendments
- Verbatim source quotations and page provenance
- Non-superseding informational REFERENCES
- Distinction between issue_date, effective_date, and compliance glide paths
"""
from typing import List, Dict, Any, Optional, Set
import json
from pathlib import Path

from app.schemas.graph import (
    GraphEdge,
    RelationshipType,
    ScopeType,
    ConfidenceLevel,
    ReviewStatus,
)
from app.schemas.graph_collection import GraphEdgeCollection


# Consolidated Verified Edges from docs/cscrf-relationship-audit.md
VERIFIED_CSCRF_EDGES_DATA: List[Dict[str, Any]] = [
    # -------------------------------------------------------------------------
    # Circular 2025/045 (Issued 2025-03-28)
    # -------------------------------------------------------------------------
    {
        "edge_id": "edge_cscrf_2025_045_amends_2024_113_timelines",
        "source_document_id": "sebi_cscrf_2025_045",
        "target_document_id": "sebi_cscrf_2024_113",
        "relationship": RelationshipType.AMENDS,
        "scope": ScopeType.PARTIAL,
        "affected_provisions": ["Paragraph 17", "Implementation Period"],
        "evidence_text": (
            "2. SEBI had received multiple requests for CSCRF compliance timelines extension "
            "to ensure ease of compliance for them. Therefore, it has been decided to extend "
            "the compliance timelines by three (3) months, i.e., till June 30, 2025 to all REs, "
            "except Market Infrastructure Institutions (MIIs), KYC Registration Agencies (KRAs), "
            "and Qualified Registrars to an Issue and Share Transfer Agents (QRTAs)."
        ),
        "evidence_page": 2,
        "confidence": ConfidenceLevel.HIGH,
        "review_status": ReviewStatus.VERIFIED,
        "effective_date": "2025-03-28",
    },
    {
        "edge_id": "edge_cscrf_2025_045_references_2024_113_preamble",
        "source_document_id": "sebi_cscrf_2025_045",
        "target_document_id": "sebi_cscrf_2024_113",
        "relationship": RelationshipType.REFERENCES,
        "scope": ScopeType.FULL,
        "affected_provisions": [],
        "evidence_text": (
            "1. Recognising the need for robust cybersecurity measures and protection of data "
            "and IT infrastructure, Securities and Exchange Board of India (SEBI) has issued "
            "‘Cybersecurity and Cyber Resilience Framework (CSCRF) for SEBI Regulated Entities (REs)’ "
            "vide circular SEBI/HO/ ITD-1/ITD_CSC_EXT/P/CIR/2024/113 dated August 20, 2024."
        ),
        "evidence_page": 2,
        "confidence": ConfidenceLevel.HIGH,
        "review_status": ReviewStatus.VERIFIED,
        "effective_date": None,
    },

    # -------------------------------------------------------------------------
    # Circular 2025/060 (Issued 2025-04-30)
    # -------------------------------------------------------------------------
    {
        "edge_id": "edge_cscrf_2025_060_amends_2024_113_stock_brokers_and_entities",
        "source_document_id": "sebi_cscrf_2025_060",
        "target_document_id": "sebi_cscrf_2024_113",
        "relationship": RelationshipType.AMENDS,
        "scope": ScopeType.PARTIAL,
        "affected_provisions": [
            "Section 2.1 (Stock Brokers / Table 1)",
            "Section 2.2 (Depository Participants / Table 2)",
            "Section 2.3 (Investment Advisers)",
            "Section 2.4 (Research Analysts)",
            "Section 2.5 (KYC Registration Agencies)",
            "Section 2.7 (AIFs and VCFs / Table 4)",
            "Annexure-J / Clause 5 (Cloud Hardware Security Module)",
        ],
        "evidence_text": (
            "2. Based on further discussions with REs, it has been decided to revise the thresholds "
            "and categorization of following REs as contained in this circular... "
            "2.1. Stock brokers... 2.2. Depository Participants (DPs)... 2.3. Investment Advisers (IAs)... "
            "2.4. Research Analysts (RAs)... 2.5. KYC Registration Agencies (KRAs)... "
            "2.7. Alternate Investment Funds (AIFs) and Venture Capital Funds (VCFs)... "
            "5. W.r.t. ‘Framework for Adoption of Cloud Services by SEBI Regulated Entities (REs)’ "
            "issued vide SEBI circular SEBI/HO/ITD/ITD_VAPT/P/CIR/2023/033 dated March 06, 2023 (CSCRF: Annexure-J), "
            "implementation of a dedicated Hardware Security Module (HSM) shall be made mandatory for MIIs and Qualified REs. "
            "However, mid-size REs, small-size REs, and self-certification REs shall be allowed to implement any alternative of HSM."
        ),
        "evidence_page": 2,
        "confidence": ConfidenceLevel.HIGH,
        "review_status": ReviewStatus.VERIFIED,
        "effective_date": "2025-04-30",
    },
    {
        "edge_id": "edge_cscrf_2025_060_references_2025_045_timeline",
        "source_document_id": "sebi_cscrf_2025_060",
        "target_document_id": "sebi_cscrf_2025_045",
        "relationship": RelationshipType.REFERENCES,
        "scope": ScopeType.FULL,
        "affected_provisions": [],
        "evidence_text": (
            "10. As already communicated vide SEBI circular SEBI/HO/ITD-1/ ITD_CSC_EXT/P/ CIR/2025/45 "
            "dated March 28, 2025, timeline for compliance is June 30, 2025, for REs covered in the said circular."
        ),
        "evidence_page": 7,
        "confidence": ConfidenceLevel.HIGH,
        "review_status": ReviewStatus.VERIFIED,
        "effective_date": None,
    },

    # -------------------------------------------------------------------------
    # Circular 2025/096 (Issued 2025-06-30)
    # -------------------------------------------------------------------------
    {
        "edge_id": "edge_cscrf_2025_096_amends_2025_045_timelines_august",
        "source_document_id": "sebi_cscrf_2025_096",
        "target_document_id": "sebi_cscrf_2025_045",
        "relationship": RelationshipType.AMENDS,
        "scope": ScopeType.PARTIAL,
        "affected_provisions": ["Paragraph 2", "Compliance Timelines"],
        "evidence_text": (
            "2. SEBI has received multiple requests for CSCRF compliance timelines extension "
            "to ensure ease of compliance for them. Therefore, it has been decided to extend "
            "the compliance timelines by two (2) months, i.e., till August 31, 2025 to all REs, "
            "except Market Infrastructure Institutions (MIIs), KYC Registration Agencies (KRAs), "
            "and Qualified Registrars to an Issue and Share Transfer Agents (QRTAs)."
        ),
        "evidence_page": 2,
        "confidence": ConfidenceLevel.HIGH,
        "review_status": ReviewStatus.VERIFIED,
        "effective_date": "2025-06-30",
    },
    {
        "edge_id": "edge_cscrf_2025_096_references_2024_113_preamble",
        "source_document_id": "sebi_cscrf_2025_096",
        "target_document_id": "sebi_cscrf_2024_113",
        "relationship": RelationshipType.REFERENCES,
        "scope": ScopeType.FULL,
        "affected_provisions": [],
        "evidence_text": (
            "1. Recognising the need for robust cybersecurity measures and protection of data "
            "and IT infrastructure, Securities and Exchange Board of India (SEBI) has issued "
            "‘Cybersecurity and Cyber Resilience Framework (CSCRF) for SEBI Regulated Entities (REs)’ "
            "vide circular SEBI/HO/ ITD-1/ITD_CSC_EXT/P/CIR/2024/113 dated August 20, 2024."
        ),
        "evidence_page": 2,
        "confidence": ConfidenceLevel.HIGH,
        "review_status": ReviewStatus.VERIFIED,
        "effective_date": None,
    },

    # -------------------------------------------------------------------------
    # Circular 2025/119 (Issued 2025-08-28)
    # -------------------------------------------------------------------------
    {
        "edge_id": "edge_cscrf_2025_119_amends_2024_113_technical_clarifications",
        "source_document_id": "sebi_cscrf_2025_119",
        "target_document_id": "sebi_cscrf_2024_113",
        "relationship": RelationshipType.AMENDS,
        "scope": ScopeType.PARTIAL,
        "affected_provisions": [
            "Part-A (Principles of Exclusivity & Equivalence)",
            "Definitions (Page 26, Item f / Clause 6.1)",
            "PR.AA.S4 & PR.AA.S5 (Zero-Trust Security / Clause 6.2)",
            "PR.AA.S16 (Mobile Application Security / Clause 6.3)",
            "DE.CM.S3 (Guideline 3.c - BAS/CART Security Solutions / Clause 6.5)",
            "RC.RP.S2 (Two-Hour RTO & 15-min RPO / Clause 6.10)",
            "Section 4.2 & PR.IP.S16 (ISO 27001 Recommendation / Clause 6.11)",
        ],
        "evidence_text": (
            "5.1. Following Principle of Exclusivity and Principle of Equivalence have been formulated... "
            "6.1. Clarification: Above-mentioned para (f) shall now be read as under: Any other system which is on the same network segment... "
            "6.2. Clarification: REs shall implement suggested strategies/ methodologies such as Zero-trust networks, segmentation... "
            "6.3. Clarification: Above-mentioned guidelines are recommendatory (not mandatory) in nature... "
            "6.5. Clarification: It is recommended that REs consider deploying a range of security solutions in consultation with their IT Committee... "
            "6.10. Clarification: Resumption within two hours (i.e. two-hour RTO)... RPO for critical systems shall be 15 minutes... "
            "6.11. Clarification: Qualified REs are encouraged and recommended (not mandatory) to obtain ISO 27001 certification."
        ),
        "evidence_page": 3,
        "confidence": ConfidenceLevel.HIGH,
        "review_status": ReviewStatus.VERIFIED,
        "effective_date": "2025-08-28",
    },
    {
        "edge_id": "edge_cscrf_2025_119_amends_2025_060_pm_mb_thresholds",
        "source_document_id": "sebi_cscrf_2025_119",
        "target_document_id": "sebi_cscrf_2025_060",
        "relationship": RelationshipType.AMENDS,
        "scope": ScopeType.PARTIAL,
        "affected_provisions": [
            "Section 7.1 (Table 1: Portfolio Managers)",
            "Section 7.2 (Table 2: Merchant Bankers)",
        ],
        "evidence_text": (
            "7. Based on further discussions, it has been decided to revise the thresholds "
            "and categorization of following REs as under: "
            "7.1. Portfolio Managers Table 1: Criteria and thresholds for Portfolio Managers categorization... "
            "7.2. Merchant Bankers (MBs) Table 2: Criteria and thresholds for MBs categorization"
        ),
        "evidence_page": 10,
        "confidence": ConfidenceLevel.HIGH,
        "review_status": ReviewStatus.VERIFIED,
        "effective_date": "2025-08-28",
    },
    {
        "edge_id": "edge_cscrf_2025_119_references_2025_060_market_soc",
        "source_document_id": "sebi_cscrf_2025_119",
        "target_document_id": "sebi_cscrf_2025_060",
        "relationship": RelationshipType.REFERENCES,
        "scope": ScopeType.PARTIAL,
        "affected_provisions": ["Clause 2.2", "Clause 2.6", "Clause 2.7", "Clause 3", "Clause 6.9"],
        "evidence_text": (
            "Clarification: small-size and self-certification with few exclusions as mentioned in "
            "(CSCRF Box Item 11, DE.CM.S3 Guidelines (c-d) – Page 118) and "
            "(Clause 2.2, 2.6, 2.7 and 3 of SEBI circular vide SEBI/HO/ ITD-1/ITD_CSC_EXT/P/CIR/2025/60 "
            "dated April 30, 2025) have been mandated to be on boarded on Market-SOC."
        ),
        "evidence_page": 8,
        "confidence": ConfidenceLevel.HIGH,
        "review_status": ReviewStatus.VERIFIED,
        "effective_date": None,
    },
]


def create_verified_cscrf_graph(known_document_ids: Optional[Set[str]] = None) -> GraphEdgeCollection:
    """Instantiate and populate the verified SEBI CSCRF GraphEdgeCollection."""
    collection = GraphEdgeCollection(known_document_ids=known_document_ids)
    for edge_dict in VERIFIED_CSCRF_EDGES_DATA:
        edge = GraphEdge(**edge_dict)
        collection.add_edge(edge)
    return collection


def export_graph_to_json(collection: GraphEdgeCollection, output_path: str | Path) -> None:
    """Deterministic JSON serialization of a GraphEdgeCollection."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Deterministically sort edges by edge_id
    sorted_edges = [
        collection.edges[k].model_dump(mode="json")
        for k in sorted(collection.edges.keys())
    ]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted_edges, f, indent=2, ensure_ascii=False)


def load_graph_from_json(input_path: str | Path, known_document_ids: Optional[Set[str]] = None) -> GraphEdgeCollection:
    """Load and validate a GraphEdgeCollection from a JSON file."""
    path = Path(input_path)
    if not path.exists():
        raise FileNotFoundError(f"Graph JSON file not found at: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    collection = GraphEdgeCollection(known_document_ids=known_document_ids)
    for item in data:
        edge = GraphEdge(**item)
        collection.add_edge(edge)
    return collection
