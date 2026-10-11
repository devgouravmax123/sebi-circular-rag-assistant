"""
Deterministic relevance safeguard for SEBI CSCRF Regulatory Q&A Assistant.

Evaluates retrieval candidate passages against query intent and content anchors:
- Prevents out-of-scope or unrelated passages from being presented as responsive evidence.
- Distinguishes current-status vs. historical queries.
- Ensures empty, malformed, or missing candidate metadata fails closed.
- Emits explicit, testable relevance decision codes.
"""
from enum import Enum
import re
from typing import List, Dict, Any, Optional, Set, Tuple

from app.grounding.classifier import QueryIntent, classify_query_intent
from app.schemas.retrieval import RetrievalResult
from app.schemas.grounding import RegulatoryStatus


class RelevanceDecisionCode(str, Enum):
    RELEVANT_EVIDENCE = "RELEVANT_EVIDENCE"
    INSUFFICIENT_RELEVANCE = "INSUFFICIENT_RELEVANCE"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    CURRENT_STATUS_UNESTABLISHED = "CURRENT_STATUS_UNESTABLISHED"


class CandidateRelevanceAssessment:
    def __init__(
        self,
        chunk_id: str,
        is_relevant: bool,
        decision_code: RelevanceDecisionCode,
        reason: str,
    ):
        self.chunk_id = chunk_id
        self.is_relevant = is_relevant
        self.decision_code = decision_code
        self.reason = reason


def assess_candidate_relevance(
    query: str,
    intent: QueryIntent,
    candidate: RetrievalResult,
) -> CandidateRelevanceAssessment:
    """
    Evaluates relevance of an individual retrieval candidate passage against query intent and topic anchors.
    """
    text_lower = candidate.text.lower()
    q_lower = query.lower()

    # 1. Out of scope intent -> automatically rejected
    if intent == QueryIntent.OUT_OF_SCOPE_OR_UNESTABLISHED:
        return CandidateRelevanceAssessment(
            chunk_id=candidate.chunk_id,
            is_relevant=False,
            decision_code=RelevanceDecisionCode.OUT_OF_SCOPE,
            reason="Query is outside the demonstrated scope of the 5-document SEBI CSCRF corpus.",
        )

    # 2. Circular-specific change queries
    if intent == QueryIntent.CIRCULAR_SPECIFIC_CHANGE:
        # Check if candidate comes from or explicitly references the requested circular
        # Look for circular number tokens in query
        target_circulars = []
        if "096" in q_lower or "96" in q_lower:
            target_circulars.append("2025_096")
        if "045" in q_lower or "45" in q_lower:
            target_circulars.append("2025_045")
        if "060" in q_lower or "60" in q_lower:
            target_circulars.append("2025_060")
        if "119" in q_lower:
            target_circulars.append("2025_119")
        if "113" in q_lower:
            target_circulars.append("2024_113")

        if target_circulars:
            if not any(tc in candidate.document_id for tc in target_circulars):
                # Candidate is from another circular; check if it explicitly mentions the targeted circular in text
                mentions_target = any(
                    tc.replace("_", "/") in text_lower or tc.replace("_", "-") in text_lower
                    for tc in target_circulars
                )
                if not mentions_target:
                    return CandidateRelevanceAssessment(
                        chunk_id=candidate.chunk_id,
                        is_relevant=False,
                        decision_code=RelevanceDecisionCode.INSUFFICIENT_RELEVANCE,
                        reason=f"Candidate from '{candidate.document_id}' does not match requested circular target {target_circulars}.",
                    )

    # 3. Historical vs Current Deadline queries
    if intent == QueryIntent.HISTORICAL_DEADLINE:
        # Looking for original timeline or initial baseline dates
        # E.g. Paragraph 17 of CIR/2024/113 or CIR/2025/045 timeline background
        has_deadline_anchor = any(
            kw in text_lower
            for kw in [
                "timeline", "deadline", "implementation period", "paragraph 17",
                "april 01, 2025", "phases", "effective date", "extension"
            ]
        )
        if not has_deadline_anchor:
            return CandidateRelevanceAssessment(
                chunk_id=candidate.chunk_id,
                is_relevant=False,
                decision_code=RelevanceDecisionCode.INSUFFICIENT_RELEVANCE,
                reason="Passage does not contain historical compliance timeline or deadline anchors.",
            )

    if intent == QueryIntent.CURRENT_STATUS_OR_DEADLINE:
        # Check if candidate addresses timelines or deadlines specifically
        # Must not match general compliance/audit chunks like ISO 27001 recommendation or unrelated procedural chunks
        has_timeline_anchor = (
            re.search(r"\b(?:extend\s+the\s+compliance\s+timelines|compliance\s+timelines?|implementation\s+period|glide[\s-]path|paragraph\s+17|para\s+17|till\s+august\s+31,\s+2025|till\s+june\s+30,\s+2025|by\s+april\s+01,\s+2025|by\s+january\s+01,\s+2025)\b", text_lower)
            is not None
        )
        # Explicitly exclude chunks that are solely about ISO 27001 or general COTS
        is_iso_or_cots_only = ("iso 27001" in text_lower or "pr.ip.s16" in text_lower) and not ("paragraph 17" in text_lower or "compliance timelines" in text_lower or "extend" in text_lower)
        if not has_timeline_anchor or is_iso_or_cots_only:
            return CandidateRelevanceAssessment(
                chunk_id=candidate.chunk_id,
                is_relevant=False,
                decision_code=RelevanceDecisionCode.INSUFFICIENT_RELEVANCE,
                reason="Passage does not address regulatory deadlines or compliance timelines.",
            )

    # 4. Provision-specific queries (e.g. ISO 27001, CISO, RTO/RPO)
    if intent == QueryIntent.PROVISION_SPECIFIC_REQUIREMENT:
        # Check specific subject matter anchors
        if "iso 27001" in q_lower or "iso27001" in q_lower:
            # Reject passages that only cite ISO 27001 incidentally alongside general standards (e.g. ISO 27002 / CCI)
            is_incidental_standards_mention = bool(
                re.search(
                    r"\bstandards\s+such\s+as\s+iso\s*27001\b|\bfrom\s+standards\s+such\s+as\b",
                    text_lower,
                )
            )
            # Must discuss operative ISO 27001 certification, audit, mandate, recommendation, or clause anchors
            has_operative_iso_anchor = bool(
                re.search(
                    r"(?:\b(?:obtain|mandat\w*|certif\w*|require\w*|recommend\w*)\s+(?:[\w\s]{0,25}\s+)?iso\s*27001\b|"
                    r"\biso\s*27001\b\s*(?:certification|audit|standard|\(latest|\(page|\(not|\s*shall)\b|"
                    r"\bpr\.ip\.s16\b|\bsection\s+4\.2\b|\bclause\s+6\.11\b|\b4\.2\.?\s+iso\s+audit\b)",
                    text_lower,
                )
            )
            if not has_operative_iso_anchor or is_incidental_standards_mention:
                return CandidateRelevanceAssessment(
                    chunk_id=candidate.chunk_id,
                    is_relevant=False,
                    decision_code=RelevanceDecisionCode.INSUFFICIENT_RELEVANCE,
                    reason="Passage does not contain operative ISO 27001 requirements or anchors.",
                )

        if "ciso" in q_lower:
            has_ciso_anchor = bool(
                re.search(
                    r"\bciso\b|\bchief\s+information\s+security\s+officer\b|\bgv\.go\.s1\b",
                    text_lower,
                )
            )
            if not has_ciso_anchor:
                return CandidateRelevanceAssessment(
                    chunk_id=candidate.chunk_id,
                    is_relevant=False,
                    decision_code=RelevanceDecisionCode.INSUFFICIENT_RELEVANCE,
                    reason="Passage does not contain CISO requirements or anchors.",
                )

    return CandidateRelevanceAssessment(
        chunk_id=candidate.chunk_id,
        is_relevant=True,
        decision_code=RelevanceDecisionCode.RELEVANT_EVIDENCE,
        reason="Passage satisfies topic and intent relevance criteria.",
    )


def filter_relevant_candidates(
    query: str,
    candidates: List[RetrievalResult],
) -> Tuple[List[RetrievalResult], QueryIntent, List[CandidateRelevanceAssessment]]:
    """
    Evaluates and filters a list of retrieval candidates against query intent and relevance safeguards.
    """
    intent = classify_query_intent(query)
    assessments: List[CandidateRelevanceAssessment] = []
    relevant_candidates: List[RetrievalResult] = []

    for c in candidates:
        assessment = assess_candidate_relevance(query, intent, c)
        assessments.append(assessment)
        if assessment.is_relevant:
            relevant_candidates.append(c)

    # For CURRENT_STATUS_OR_DEADLINE, deterministic intent-specific candidate-selection ordering:
    # Prioritize newer timeline modification circulars (e.g. CIR/2025/096, CIR/2025/045)
    # followed by baseline CIR/2024/113 timeline provisions.
    if intent == QueryIntent.CURRENT_STATUS_OR_DEADLINE and relevant_candidates:
        def deadline_priority_key(item: RetrievalResult) -> Tuple[int, float]:
            doc_id = item.document_id
            # High priority for CIR/2025/096 (latest extension in pilot)
            if "2025_096" in doc_id:
                return (0, -item.score)
            # Next for CIR/2025/045 (intermediate extension in pilot)
            elif "2025_045" in doc_id:
                return (1, -item.score)
            # Baseline CIR/2024/113 timeline
            elif "2024_113" in doc_id:
                return (2, -item.score)
            return (3, -item.score)

        relevant_candidates.sort(key=deadline_priority_key)

    # For PROVISION_SPECIFIC_REQUIREMENT regarding ISO 27001 for Qualified REs:
    # Prioritize verified clarification circulars (CIR/2025/119 Clause 6.11)
    # directly modifying Qualified RE certification status over partially amended baseline passages.
    q_lower = query.lower()
    if (
        intent == QueryIntent.PROVISION_SPECIFIC_REQUIREMENT
        and ("iso 27001" in q_lower or "iso27001" in q_lower)
        and relevant_candidates
    ):
        def iso_priority_key(item: RetrievalResult) -> Tuple[int, float]:
            doc_id = item.document_id
            # Priority 0: CIR/2025/119 (Clause 6.11 specific clarification for Qualified REs)
            if "2025_119" in doc_id:
                return (0, -item.score)
            # Priority 1: Baseline CIR/2024/113
            elif "2024_113" in doc_id:
                return (1, -item.score)
            return (2, -item.score)

        relevant_candidates.sort(key=iso_priority_key)

    return relevant_candidates, intent, assessments
