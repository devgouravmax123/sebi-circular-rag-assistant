"""
Deterministic mock LLM synthesis provider for offline tests and evaluation (Phase 8E).

Behavior:
1. Operates purely offline without network calls or API costs.
2. Extracts sentences from verified evidence passages that address query concepts.
3. Attaches claim-level citations to specific source chunk IDs.
4. Identifies multi-part questions and emits UnresolvedSubquestion disclosures when evidence only covers a subset.
5. Injects simulated timeouts or failures when configured for testing.
6. Neutralizes untrusted prompt-injection strings inside document passages.
"""
import re
from typing import List, Optional, Dict, Any

from app.schemas.synthesis import (
    SynthesisInput,
    SynthesisOutput,
    SynthesisStatus,
    ClaimCitation,
    UnresolvedSubquestion,
)
from app.synthesis.provider import (
    SynthesisProvider,
    SynthesisTimeoutError,
    SynthesisUnavailableError,
    SynthesisMalformedOutputError,
)
from app.synthesis.verifier import extract_keywords_from_claim


class MockSynthesisProvider(SynthesisProvider):
    """
    Deterministic mock synthesis provider.
    """

    def __init__(
        self,
        simulate_timeout: bool = False,
        simulate_unavailable: bool = False,
        simulate_malformed: bool = False,
        simulate_unsupported_claim: bool = False,
        simulate_nonexistent_citation: bool = False,
    ):
        self.simulate_timeout = simulate_timeout
        self.simulate_unavailable = simulate_unavailable
        self.simulate_malformed = simulate_malformed
        self.simulate_unsupported_claim = simulate_unsupported_claim
        self.simulate_nonexistent_citation = simulate_nonexistent_citation

    def synthesize(self, payload: SynthesisInput) -> SynthesisOutput:
        # 1. Error simulation checks for tests
        if self.simulate_timeout:
            raise SynthesisTimeoutError("Mock provider exceeded execution timeout.")
        if self.simulate_unavailable:
            raise SynthesisUnavailableError("Mock provider is offline or unconfigured.")
        if self.simulate_malformed:
            raise SynthesisMalformedOutputError("Mock provider returned unparseable output.")

        # 2. Check for empty evidence -> safe abstention
        if not payload.evidence_items:
            return SynthesisOutput(
                status=SynthesisStatus.ABSTAINED,
                answer="Controlled Abstention: No verified evidence provided for synthesis.",
                claims=[],
                unresolved_subquestions=[],
                failure_reason="Zero evidence items supplied to synthesis provider.",
            )

        query = payload.query.strip()
        q_lower = query.lower()

        # 3. Handle multi-part question detection
        # Multi-part questions typically combine two distinct inquiries (e.g. "and what", "also", "?")
        is_multipart = bool(
            re.search(r"\band\s+what\b|\band\s+how\b|\band\s+when\b|\band\s+which\b|\?.*\?", q_lower)
        )

        claims: List[ClaimCitation] = []
        answer_paragraphs: List[str] = []

        # 4. Form claims from top evidence items
        raw_tokens = extract_keywords_from_claim(query)
        q_tokens = [w for w in raw_tokens if w not in {"what", "which", "where", "when", "how", "define", "meaning", "explain"}]
        preamble_markers = [
            r"^circular\b",
            r"^sebi/ho/",
            r"^to\s+all\s+(?:registered|regulated|stock|depositories|clearing)",
            r"dear\s+sir\s*/\s*madam",
            r"^\s*sub\s*:",
        ]

        for ev in payload.evidence_items[:3]:
            # Clean passage text: strip prompt injection attempts or raw formatting
            clean_text = re.sub(
                r"ignore\s+(?:all\s+)?previous\s+instructions[^\.\n]*", "", ev.text, flags=re.IGNORECASE
            ).strip()

            # Extract sentences
            candidate_sentences = [
                s.strip() for s in re.split(r"(?<=[.!?])\s+", clean_text) if len(s.strip()) > 20
            ]
            if not candidate_sentences:
                candidate_sentences = [clean_text[:200].strip()]

            # Score each sentence against substantive query tokens and penalize non-ASCII / metadata preambles
            def _score_and_filter(s: str) -> int:
                # Discard sentences that are mostly non-ASCII (e.g. Hindi headers decoded in latin-1)
                ascii_chars = sum(1 for c in s if ord(c) < 128)
                if len(s) > 0 and (ascii_chars / len(s)) < 0.70:
                    return 0

                s_lower = s.lower().strip()
                # Check for administrative circular preambles
                if any(re.search(marker, s_lower) for marker in preamble_markers):
                    return 0

                return sum(1 for t in q_tokens if t in s_lower)

            scored = [(s, _score_and_filter(s)) for s in candidate_sentences]
            positive_scored = [pair for pair in scored if pair[1] > 0]

            if positive_scored:
                best_sentence = max(positive_scored, key=lambda x: x[1])[0]
                claims.append(
                    ClaimCitation(
                        claim_text=best_sentence,
                        supporting_chunk_ids=[ev.chunk_id],
                    )
                )
                answer_paragraphs.append(best_sentence)


        # 5. Simulated test condition injections
        if self.simulate_unsupported_claim:
            # Insert a claim not supported by the chunk
            claims.append(
                ClaimCitation(
                    claim_text="The Reserve Bank of India mandates a 100% Liquidity Coverage Ratio for all commercial banks.",
                    supporting_chunk_ids=[payload.evidence_items[0].chunk_id],
                )
            )
            answer_paragraphs.append(
                "The Reserve Bank of India mandates a 100% Liquidity Coverage Ratio for all commercial banks."
            )

        if self.simulate_nonexistent_citation:
            # Insert a citation referencing a nonexistent chunk ID
            claims.append(
                ClaimCitation(
                    claim_text="Nonexistent reference claim.",
                    supporting_chunk_ids=["chunk_fabricated_99999"],
                )
            )

        # 6. Topical coverage and answer sufficiency assessment
        unresolved: List[UnresolvedSubquestion] = []
        status = SynthesisStatus.FULLY_ANSWERED

        # (a) Check if the synthesized claims substantively cover the key concepts of the query
        # Prevent irrelevant evidence from receiving FULLY_ANSWERED status merely because citations pass mechanical checks.
        query_substantive = extract_keywords_from_claim(query)
        # Exclude pure question meta-words from sufficiency calculation
        query_substantive = [
            kw for kw in query_substantive
            if kw not in {"what", "which", "where", "when", "how", "define", "meaning", "explain", "describe", "overview"}
        ]
        if query_substantive and answer_paragraphs:
            combined_claims_text = " ".join(answer_paragraphs).lower()
            matched_query_kw = [kw for kw in query_substantive if kw in combined_claims_text]
            coverage = len(matched_query_kw) / len(query_substantive)
            if coverage < 0.40:
                missing_kw = [kw for kw in query_substantive if kw not in matched_query_kw]
                unresolved.append(
                    UnresolvedSubquestion(
                        subquestion=f"Sufficiency check for query subject: '{query}'",
                        reason=(
                            f"Retrieved evidence does not substantively define or explain key query concepts "
                            f"(coverage {coverage:.0%}, missing {missing_kw[:3]})."
                        ),
                    )
                )
                status = SynthesisStatus.PARTIALLY_ANSWERED

        # (b) Multi-part coverage assessment
        if is_multipart:
            # Identify secondary question clause
            parts = re.split(r"\band\s+(?:what|how|when|which)\b", query, flags=re.IGNORECASE)
            if len(parts) > 1:
                sub_q = parts[1].strip().rstrip("?")
                # Check if evidence mentions keywords of the secondary subquestion
                sub_keywords = extract_keywords_from_claim(sub_q)
                evidence_text = " ".join(e.text.lower() for e in payload.evidence_items)
                has_sub_support = any(kw in evidence_text for kw in sub_keywords) if sub_keywords else False

                if not has_sub_support:
                    unresolved.append(
                        UnresolvedSubquestion(
                            subquestion=f"What {sub_q}?",
                            reason="Supplied regulatory passages do not contain specific provisions addressing this secondary detail.",
                        )
                    )
                    status = SynthesisStatus.PARTIALLY_ANSWERED

        # 7. Disclose mock/offline mode explicitly and handle zero qualifying sentences
        if not claims or not answer_paragraphs:
            return SynthesisOutput(
                status=SynthesisStatus.PARTIALLY_ANSWERED,
                answer=(
                    "[MOCK SYNTHESIS - OFFLINE TEST MODE]\n\n"
                    "The retrieved passages do not contain substantive provisions directly addressing "
                    f"the key terms of query '{query}'. No speculative or preamble assertions have been fabricated."
                ),
                claims=[],
                unresolved_subquestions=[
                    UnresolvedSubquestion(
                        subquestion=f"Regulatory answer for: '{query}'",
                        reason="Passages in retrieved evidence contain only non-matching sections, administrative metadata, or circular headers.",
                    )
                ],
                failure_reason="Mock offline mode: No evidence sentences matched query concepts.",
            )

        full_answer = (
            "[MOCK SYNTHESIS - OFFLINE TEST MODE: The following sentences are extracted verbatim excerpts "
            "for deterministic test verification only]\n\n" + "\n\n".join(answer_paragraphs)
        )

        return SynthesisOutput(
            status=status,
            answer=full_answer,
            claims=claims,
            unresolved_subquestions=unresolved,
            failure_reason="Mock offline mode: Output consists of deterministic excerpt extractions, not live LLM regulatory synthesis.",
        )
