"""
Collection-aware evidence scope evaluation for multi-collection document intelligence.

Provides deterministic detection of explicit scope mismatches between query requirements
and a selected collection's declared governing entity, regulator, and domain:
- Identifies explicitly requested governing authorities or frameworks in queries (e.g. SEBI CSCRF, CISA Playbooks, GDPR).
- Compares requested authorities against target collection metadata (collection_id, regulator, domain, name).
- Distinguishes:
  1. EXPLICIT_MISMATCH: The query explicitly demands rules/requirements from an authority/framework
     completely incompatible with the selected collection.
  2. COMPATIBLE: The query matches or is aligned with the collection's declared scope.
  3. AMBIGUOUS_OR_UNSPECIFIED: The query does not mandate a specific incompatible framework,
     allowing standard evidence-based retrieval and quotation verification to govern.
"""
from enum import Enum
import re
from typing import Optional, Set, Tuple, List
from pydantic import BaseModel


class ScopeMatchStatus(str, Enum):
    COMPATIBLE = "COMPATIBLE"
    EXPLICIT_MISMATCH = "EXPLICIT_MISMATCH"
    AMBIGUOUS_OR_UNSPECIFIED = "AMBIGUOUS_OR_UNSPECIFIED"


class ScopeEvaluationResult(BaseModel):
    status: ScopeMatchStatus
    requested_entity: Optional[str] = None
    target_collection_id: str
    target_regulator: Optional[str] = None
    reason: Optional[str] = None


# Known governing frameworks and their distinguishing identifying anchors
# Format: key -> (canonical_name, regex_patterns, matching_collection_ids, matching_regulators)
KNOWN_GOVERNING_FRAMEWORKS = {
    "SEBI_CSCRF": {
        "name": "SEBI Cybersecurity and Cyber Resilience Framework (CSCRF)",
        "patterns": [
            r"\bsebi\b",
            r"\bcscrf\b",
            r"\bsebi\s+cscrf\b",
            r"\bqualified\s+res?\b",
            r"\bmiis?\s+and\s+qualified\s+res?\b",
        ],
        "compatible_collections": {"sebi_cscrf"},
        "compatible_regulators": {"SEBI"},
    },
    "CISA_PLAYBOOKS": {
        "name": "CISA Cybersecurity Playbooks (EO 14028)",
        "patterns": [
            r"\bcisa\s+playbooks?\b",
            r"\bcisa\s+(?:guidance|incident|vulnerability|operational)\b",
            r"\bunder\s+cisa\b",
            r"\bby\s+cisa\b",
            r"\bnotify\s+cisa\b",
            r"\bcoordination\s+with\s+cisa\b",
            r"\be\.?o\.?\s*14028\b",
            r"\bexecutive\s+order\s+14028\b",
            r"\bfceb\b",
            r"\bnciss\b",
            r"\bcisa\b",
        ],
        "evidence_patterns": [
            r"\bcisa\s+playbooks?\b",
            r"\bcybersecurity\s+and\s+infrastructure\s+security\s+agency\b",
            r"\be\.?o\.?\s*14028\b",
            r"\bexecutive\s+order\s+14028\b",
            r"\bfceb\b",
            r"\bnciss\b",
            r"\bcisa\s+(?:guidance|incident|vulnerability|operational)\b",
        ],
        "compatible_collections": {"cisa_playbooks"},
        "compatible_regulators": {"CISA", "DHS"},
    },
    "GDPR": {
        "name": "EU General Data Protection Regulation (GDPR)",
        "patterns": [
            r"\bgdpr\b",
            r"\bgeneral\s+data\s+protection\s+regulation\b",
            r"\barticle\s+33\b",
        ],
        "compatible_collections": set(),
        "compatible_regulators": set(),
    },
    "ACME_FINOPS": {
        "name": "Acme Cloud FinOps and Infrastructure Policy",
        "patterns": [
            r"\bacme\b",
            r"\bfinops\b",
            r"\bdata\s+mesh\b",
        ],
        "compatible_collections": {"demo_finops", "general_synthetic"},
        "compatible_regulators": set(),
    },
}


def evaluate_query_collection_scope(
    query: str,
    collection_id: str,
    collection_name: Optional[str] = None,
    regulator: Optional[str] = None,
    domain: Optional[str] = None,
    candidate_excerpts_text: Optional[str] = None,
) -> ScopeEvaluationResult:
    """
    Evaluates whether a query explicitly demands requirements from an authority or framework
    that is fundamentally incompatible with the target collection.

    Guiding Principles:
    1. Explicit Incompatible Subject: If the query explicitly targets a known governing authority
       (e.g., 'under SEBI CSCRF', 'mandated by SEBI') when querying a collection dedicated to an
       entirely different authority (e.g., 'cisa_playbooks'), return EXPLICIT_MISMATCH.
    2. Preservation of Cross-Entity Factual Questions: If the candidate evidence itself explicitly
       discusses or mentions the queried entity/standard (e.g. CISA playbook discussing NIST or OMB guidelines),
       it is NOT an explicit mismatch.
    3. Ambiguous / Generic Queries: General questions (e.g. 'What is incident response?', 'How are credentials managed?')
       that do not specify an incompatible governing body return AMBIGUOUS_OR_UNSPECIFIED, allowing
       evidence-based retrieval to function normally.
    """
    q_lower = query.lower()
    norm_cid = (collection_id or "").strip()
    norm_regulator = (regulator or "").strip().upper() if regulator else ""

    # Detect if any known governing framework is explicitly requested as the governing authority in the query
    detected_frameworks: List[str] = []
    for fw_key, fw_meta in KNOWN_GOVERNING_FRAMEWORKS.items():
        for pat in fw_meta["patterns"]:
            if re.search(pat, q_lower):
                detected_frameworks.append(fw_key)
                break

    # If no specific external framework was explicitly named, scope is ambiguous/unspecified
    if not detected_frameworks:
        return ScopeEvaluationResult(
            status=ScopeMatchStatus.AMBIGUOUS_OR_UNSPECIFIED,
            target_collection_id=norm_cid,
            target_regulator=regulator,
            reason="Query does not specify a distinct governing framework or authority.",
        )

    # Check detected frameworks against target collection compatibility
    for fw_key in detected_frameworks:
        fw_meta = KNOWN_GOVERNING_FRAMEWORKS[fw_key]
        comp_colls = fw_meta["compatible_collections"]
        comp_regs = fw_meta["compatible_regulators"]

        # Check if the target collection matches this framework
        is_compatible_target = (norm_cid in comp_colls) or (norm_regulator in comp_regs)

        if not is_compatible_target:
            # Check if candidate evidence substantively discusses the requested framework subject.
            # Merely matching an acronym (e.g. CISA as Certified Information Systems Auditor) is NOT
            # sufficient to bypass an explicit framework requirement; the evidence must contain
            # substantive topic overlap with the query.
            if candidate_excerpts_text:
                cand_lower = candidate_excerpts_text.lower()
                evidence_pats = fw_meta.get("evidence_patterns") or fw_meta["patterns"]
                evidence_has_anchor = any(
                    re.search(pat, cand_lower) for pat in evidence_pats
                )
                if evidence_has_anchor:
                    # Check that the candidate text also substantively matches other query topic tokens,
                    # not just an isolated accidental acronym mention.
                    from app.grounding.relevance import extract_substantive_query_tokens, check_token_match
                    topic_tokens = [
                        t for t in extract_substantive_query_tokens(query)
                        if not any(re.search(pat, t) for pat in fw_meta["patterns"])
                    ]
                    if topic_tokens:
                        matched_topic = [t for t in topic_tokens if check_token_match(t, cand_lower)]
                        # If the evidence text contains substantive topic representation, permit evidence answering
                        if len(matched_topic) / len(topic_tokens) >= 0.40:
                            continue
                    else:
                        continue

            # Bidirectional framework exclusivity:
            # - SEBI_CSCRF requested against non-SEBI collections (e.g. cisa_playbooks) -> EXPLICIT_MISMATCH
            # - CISA_PLAYBOOKS requested against non-CISA collections (e.g. sebi_cscrf) -> EXPLICIT_MISMATCH
            # - GDPR / ACME_FINOPS requested against collections outside their declared scope -> EXPLICIT_MISMATCH
            # - General synthetic collections preserve evidence-grounded answering for supported factual queries
            is_exclusive_framework = (
                (fw_key == "SEBI_CSCRF" and norm_cid != "sebi_cscrf" and norm_regulator != "SEBI")
                or (fw_key == "CISA_PLAYBOOKS" and norm_cid != "cisa_playbooks" and norm_regulator not in ("CISA", "DHS"))
                or (fw_key == "GDPR" and norm_cid in ("sebi_cscrf", "cisa_playbooks", "demo_finops"))
                or (fw_key == "ACME_FINOPS" and norm_cid in ("sebi_cscrf", "cisa_playbooks"))
            )

            if is_exclusive_framework:
                framework_name = fw_meta["name"]
                target_desc = collection_name or norm_cid
                return ScopeEvaluationResult(
                    status=ScopeMatchStatus.EXPLICIT_MISMATCH,
                    requested_entity=fw_key,
                    target_collection_id=norm_cid,
                    target_regulator=regulator,
                    reason=(
                        f"Query explicitly requests requirements under {framework_name}, "
                        f"which is outside the evidence scope of {target_desc}."
                    ),
                )

    return ScopeEvaluationResult(
        status=ScopeMatchStatus.COMPATIBLE,
        target_collection_id=norm_cid,
        target_regulator=regulator,
        reason="Query is compatible with target collection scope.",
    )
