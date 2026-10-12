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
        q_tokens = [w.lower() for w in re.findall(r"[a-zA-Z0-9]+", query) if len(w) >= 3]

        for ev in payload.evidence_items[:3]:
            # Clean passage text: strip prompt injection attempts or raw formatting
            clean_text = re.sub(
                r"ignore\s+(?:all\s+)?previous\s+instructions[^\.\n]*", "", ev.text, flags=re.IGNORECASE
            ).strip()

            # Extract sentences
            sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", clean_text) if len(s.strip()) > 20]
            if not sentences:
                sentences = [clean_text[:200].strip()]

            # Select sentence that shares the highest token overlap with query
            def _score_sentence(s: str) -> int:
                s_lower = s.lower()
                return sum(1 for t in q_tokens if t in s_lower)

            best_sentence = max(sentences, key=_score_sentence)
            # If best sentence has zero overlap with query, default to first sentence
            claim_sentence = best_sentence if _score_sentence(best_sentence) > 0 else sentences[0]

            claims.append(
                ClaimCitation(
                    claim_text=claim_sentence,
                    supporting_chunk_ids=[ev.chunk_id],
                )
            )
            answer_paragraphs.append(claim_sentence)


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

        # 6. Multi-part coverage assessment
        unresolved: List[UnresolvedSubquestion] = []
        status = SynthesisStatus.FULLY_ANSWERED

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

        full_answer = "\n\n".join(answer_paragraphs)

        return SynthesisOutput(
            status=status,
            answer=full_answer,
            claims=claims,
            unresolved_subquestions=unresolved,
            failure_reason=None,
        )
