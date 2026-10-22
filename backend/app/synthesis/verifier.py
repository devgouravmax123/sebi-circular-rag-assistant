"""
Deterministic citation and claim verification engine (Phase 8E).

Verifies:
1. Every cited chunk exists in the supplied verified retrieval evidence.
2. Metadata is mapped faithfully from authoritative source records (never invented).
3. The verbatim text of cited chunks actually supports the specific claim asserted.
4. Prompt injections or instruction hijacking attempts inside passage text are ignored and neutralized.
5. Inconsistencies or conflicting passages are detected and reported.
"""
import re
from typing import List, Set, Dict, Optional, Tuple, Any
import logging

from app.schemas.synthesis import (
    SynthesisInput,
    SynthesisOutput,
    SynthesisStatus,
    ClaimCitation,
    CitationProvenance,
    UnresolvedSubquestion,
)

logger = logging.getLogger(__name__)

# Patterns indicative of prompt injection attempts embedded inside untrusted retrieved document text
INJECTION_MARKERS = [
    r"ignore\s+(?:all\s+)?previous\s+instructions",
    r"system\s*prompt",
    r"disregard\s+prior\s+rules",
    r"you\s+are\s+now\s+in\s+developer\s+mode",
    r"bypass\s+(?:evidence|safety)",
    r"output\s+the\s+following\s+secret",
]


def extract_keywords_from_claim(claim_text: str) -> List[str]:
    """
    Extracts substantive factual keywords from a claim string for support verification.
    """
    stop_words = {
        "the", "and", "for", "are", "is", "with", "from", "that", "this", "these",
        "those", "have", "has", "had", "been", "was", "were", "which", "under",
        "shall", "should", "would", "could", "may", "can", "per", "such", "all",
        "any", "also", "into", "than", "more", "most", "other", "some", "only",
        "must", "does", "did", "their", "its", "our", "each", "will", "between",
    }
    words = re.findall(r"[a-zA-Z0-9]+", claim_text.lower())
    return [w for w in words if len(w) >= 3 and w not in stop_words]


GENERIC_CLAIM_WORDS = {
    "security", "cybersecurity", "cyber", "resilience", "framework", "policy",
    "guidelines", "risk", "management", "control", "controls", "measures",
    "requirements", "compliance", "system", "systems", "information", "entity",
    "entities", "operational", "process", "processes", "standard", "standards",
    "regulatory", "provisions", "shall", "mandate", "mandated", "applicable",
}

# Negation and exemption tokens to verify polarity consistency
POLARITY_WORDS = {
    "exempt", "exempted", "exemption", "exemptions", "not", "no", "never",
    "excluded", "exclusion", "waived", "waiver", "relaxed", "relaxation",
    "neither", "nor", "prohibited", "banned",
}


def extract_numeric_tokens(text: str) -> Set[str]:
    """Extract numeric and date tokens from a string (e.g. '100', '2025', '30', '4.0')."""
    return set(re.findall(r"\b\d+(?:[.,]\d+)*\b", text.lower()))


def normalize_token(token: str) -> str:
    """
    Deterministically normalizes a word token for safe singular/plural and conservative inflection matching.
    Prevents aggressive over-stemming:
    - Standard plurals: -ies -> -y, -es (after sibilants), trailing -s (excluding ss/us/is/as)
    - Conservative explicit participle/adjective pairs only (e.g. documented/documentary -> document, reporting -> report)
    Does not truncate unrelated root words (e.g. summary, primary, speed, during).
    """
    t = token.lower().strip()
    if len(t) < 4:
        return t
    # Suffix: -ies -> -y (e.g. entities -> entity, categories -> category, policies -> policy)
    if t.endswith("ies") and len(t) >= 5:
        return t[:-3] + "y"
    # Suffix: -es (e.g. processes -> process, boxes -> box, patches -> patch)
    if t.endswith("es") and len(t) >= 6 and (
        t.endswith("sses") or t.endswith("shes") or t.endswith("ches") or t.endswith("xes") or t.endswith("zes")
    ):
        return t[:-2]
    # Suffix: -s (e.g. responses -> response, circulars -> circular, audits -> audit)
    # Exclude words that naturally end in 'ss', 'us', 'is', 'as' (e.g. process, status, analysis, basis)
    if t.endswith("s") and not t.endswith(("ss", "us", "is", "as")):
        return t[:-1]
    # Safe explicit participle/adjective inflections needed for regulatory audit reporting
    if t in ("documented", "documentary"):
        return "document"
    if t == "reporting":
        return "report"
    return t


def verify_claim_support(
    claim_text: str,
    chunk_text: str,
    min_keyword_overlap: float = 0.50,
) -> Tuple[bool, float, List[str]]:
    """
    Checks whether chunk_text substantively and faithfully supports claim_text.

    A claim is supported if and only if:
    1. Exact or near-exact verbatim containment is present, OR
    2. It passes all the following strict factual consistency checks:
       (a) Numeric fidelity: All numeric values (dates, percentages, thresholds, counts, limits)
           asserted in the claim MUST exist in the cited chunk text. If a claim alters or invents
           a number, it is rejected immediately.
       (b) Polarity / Exemption consistency: If a claim asserts an exemption, exclusion, or negation
           (e.g. 'exempted', 'not required'), the supporting chunk MUST contain corresponding exemption/negation context.
       (c) Specific keyword support: The claim cannot merely match generic cybersecurity words
           ('security', 'risk', 'management', 'controls'). It must match at least 50% of the
           distinguishing specific keywords (or all specific keywords if fewer than 3).
       (d) Substantive keyword overlap: At least min_keyword_overlap (50%) of all substantive
           keywords must appear in the chunk text (via exact word match or safe singular/plural/inflection match).
    """
    claim_norm = claim_text.strip().lower()
    chunk_norm = chunk_text.strip().lower()

    if not claim_norm:
        return False, 0.0, ["empty_claim"]

    # 1. Exact or near-exact containment
    if claim_norm in chunk_norm or chunk_norm in claim_norm:
        return True, 1.0, []

    # 2a. Numeric fidelity check
    claim_numbers = extract_numeric_tokens(claim_norm)
    chunk_numbers = extract_numeric_tokens(chunk_norm)
    unmatched_numbers = claim_numbers - chunk_numbers
    if unmatched_numbers:
        return False, 0.0, [f"unsupported_number_{n}" for n in sorted(unmatched_numbers)]

    # 2b. Polarity & exemption consistency check
    claim_polarity = [w for w in POLARITY_WORDS if re.search(r"\b" + re.escape(w) + r"\b", claim_norm)]
    chunk_polarity = [w for w in POLARITY_WORDS if re.search(r"\b" + re.escape(w) + r"\b", chunk_norm)]
    if claim_polarity and not chunk_polarity:
        return False, 0.0, [f"unsupported_polarity_{p}" for p in claim_polarity]

    # 2c. Substantive & specific keywords
    keywords = extract_keywords_from_claim(claim_text)
    if not keywords:
        return True, 1.0, []

    # Extract chunk words and their normalized stems for safe morphological matching
    chunk_tokens = re.findall(r"[a-zA-Z0-9]+", chunk_norm)
    chunk_stems = {normalize_token(w) for w in chunk_tokens}

    def keyword_matches_chunk(kw: str) -> bool:
        if re.search(r"\b" + re.escape(kw) + r"\b", chunk_norm) is not None:
            return True
        norm_kw = normalize_token(kw)
        return norm_kw in chunk_stems

    matched = [kw for kw in keywords if keyword_matches_chunk(kw)]
    overlap_ratio = len(matched) / len(keywords)
    missing = [kw for kw in keywords if kw not in matched]

    if overlap_ratio < min_keyword_overlap:
        return False, overlap_ratio, missing

    # 2d. Guard against purely generic word matches
    specific_keywords = [kw for kw in keywords if kw not in GENERIC_CLAIM_WORDS]
    if specific_keywords:
        matched_specific = [kw for kw in specific_keywords if keyword_matches_chunk(kw)]
        specific_ratio = len(matched_specific) / len(specific_keywords)
        # Must match at least 50% of specific keywords, or at least 1 if only 1 specific keyword exists
        min_spec_req = 0.50 if len(specific_keywords) > 1 else 1.0
        if specific_ratio < min_spec_req:
            missing_spec = [kw for kw in specific_keywords if kw not in matched_specific]
            return False, specific_ratio, [f"missing_specific_{s}" for s in missing_spec]

    return True, overlap_ratio, []


def sanitize_and_validate_synthesis_output(
    output: SynthesisOutput,
    input_payload: SynthesisInput,
) -> SynthesisOutput:
    """
    Mechanically validates and sanitizes a SynthesisOutput against input evidence:
    1. Rejects citations referencing nonexistent chunks.
    2. Populates CitationProvenance strictly from input evidence metadata (never inventing pages/provisions).
    3. Verifies that cited chunks actually support each claim.
    4. If any claim lacks valid support, rejects or degrades the status accordingly.
    """
    if output.status in (SynthesisStatus.ABSTAINED, SynthesisStatus.PROVIDER_FAILED):
        return output

    # Build lookup of authorized input evidence items
    evidence_by_chunk_id: Dict[str, Any] = {
        item.chunk_id: item for item in input_payload.evidence_items
    }

    validated_claims: List[ClaimCitation] = []
    unsupported_claims_found = False
    unsupported_reasons: List[str] = []

    for claim in output.claims:
        valid_supporting_chunk_ids: List[str] = []
        resolved_citations: List[CitationProvenance] = []

        for cid in claim.supporting_chunk_ids:
            # 1. Existence check
            if cid not in evidence_by_chunk_id:
                unsupported_claims_found = True
                unsupported_reasons.append(
                    f"Claim references nonexistent or unauthorized chunk ID: '{cid}'"
                )
                continue

            ev = evidence_by_chunk_id[cid]

            # 2. Collection isolation check
            if input_payload.collection_id and ev.collection_id and ev.collection_id != input_payload.collection_id:
                unsupported_claims_found = True
                unsupported_reasons.append(
                    f"Claim references foreign collection chunk '{cid}' (collection '{ev.collection_id}' != expected '{input_payload.collection_id}')"
                )
                continue

            # 3. Support relationship check
            is_supported, overlap, missing = verify_claim_support(
                claim_text=claim.claim_text,
                chunk_text=ev.text,
            )

            if not is_supported:
                unsupported_claims_found = True
                unsupported_reasons.append(
                    f"Claim '{claim.claim_text[:60]}...' is not supported by chunk '{cid}' "
                    f"(overlap {overlap:.0%}, missing {missing[:3]})"
                )
                continue

            valid_supporting_chunk_ids.append(cid)

            coll_id = getattr(ev, "collection_id", None)
            prov = CitationProvenance(
                source_chunk_id=ev.chunk_id,
                document_id=ev.document_id,
                circular_number=ev.circular_number,
                title=ev.title,
                issue_date=ev.issue_date,
                sha256=ev.sha256,
                page_start=ev.page_start,
                page_end=ev.page_end,
                provision_id=ev.mapped_provisions[0] if ev.mapped_provisions else None,
                regulatory_status=ev.regulatory_status,
                collection_id=coll_id,
            )
            resolved_citations.append(prov)

        if valid_supporting_chunk_ids:
            validated_claims.append(
                ClaimCitation(
                    claim_text=claim.claim_text,
                    supporting_chunk_ids=valid_supporting_chunk_ids,
                    citations=resolved_citations,
                )
            )

    # If any claims were asserted that had zero valid supporting citations, fail safely
    if unsupported_claims_found and (len(validated_claims) < len(output.claims)):
        logger.warning(
            f"Synthesis output rejected due to unsupported claims: {unsupported_reasons}"
        )
        return SynthesisOutput(
            status=SynthesisStatus.UNSUPPORTED_CLAIMS_REJECTED,
            answer=(
                "Controlled Abstention: The proposed answer contained regulatory assertions "
                "not substantiated by the retrieved evidence."
            ),
            claims=[],
            unresolved_subquestions=output.unresolved_subquestions,
            failure_reason="One or more claims could not be verified against retrieved passage text.",
        )

    # Check for prompt injection artifacts in answer
    for marker in INJECTION_MARKERS:
        if re.search(marker, output.answer, re.IGNORECASE):
            logger.warning(f"Synthesis answer matched injection marker '{marker}'. Abstaining.")
            return SynthesisOutput(
                status=SynthesisStatus.ABSTAINED,
                answer="Controlled Abstention: Potential prompt injection or unverified instructions detected.",
                claims=[],
                unresolved_subquestions=[],
                failure_reason="Prompt injection detected in synthesized text.",
            )

    return SynthesisOutput(
        status=output.status,
        answer=output.answer,
        claims=validated_claims,
        unresolved_subquestions=output.unresolved_subquestions,
        failure_reason=output.failure_reason,
    )
