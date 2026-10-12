"""
Synthesis orchestrator service for the SEBI Regulatory Intelligence Platform (Phase 8E).

Coordinates:
1. Converting verified SafetyGate / Retrieval candidates into bounded SynthesisInput.
2. Invoking the configured SynthesisProvider with execution bounds and safe timeout handling.
3. Validating and sanitizing claim-level citations through the verifier engine.
4. Failing safely on provider errors without exposing raw internal errors or leaking credentials.
"""
import logging
from typing import Optional, List, Dict, Any

from app.core.config import get_settings
from app.schemas.synthesis import (
    SynthesisInput,
    SynthesisOutput,
    SynthesisStatus,
    SynthesisEvidenceItem,
)
from app.schemas.safety_gate import ValidatedExcerptItem, AnswerDisposition
from app.synthesis.provider import (
    SynthesisProvider,
    SynthesisTimeoutError,
    SynthesisUnavailableError,
    SynthesisMalformedOutputError,
)
from app.synthesis.mock_provider import MockSynthesisProvider
from app.synthesis.verifier import sanitize_and_validate_synthesis_output

logger = logging.getLogger(__name__)


class SynthesisService:
    """
    High-level orchestration service for answer synthesis.
    """

    def __init__(self, provider: Optional[SynthesisProvider] = None):
        self._provider = provider or MockSynthesisProvider()

    def set_provider(self, provider: SynthesisProvider) -> None:
        """Dynamically configure the synthesis provider (useful for tests and dependency injection)."""
        self._provider = provider

    def synthesize_answer(
        self,
        query: str,
        verified_excerpts: List[ValidatedExcerptItem],
        disposition: AnswerDisposition,
        regulatory_warnings: List[str],
        domain: Optional[str] = None,
    ) -> SynthesisOutput:
        """
        Executes bounded, grounded synthesis over verified regulatory excerpts.

        Rule:
        - If disposition is CONTROLLED_ABSTENTION or REJECTED_UNSAFE, or if zero excerpts exist,
          synthesis is NOT called; a safe abstention output is returned immediately.
        """
        # 1. Safety Gate Pre-Check: Do NOT call provider if gate abstained or rejected
        if disposition in (AnswerDisposition.CONTROLLED_ABSTENTION, AnswerDisposition.REJECTED_UNSAFE) or not verified_excerpts:
            return SynthesisOutput(
                status=SynthesisStatus.ABSTAINED,
                answer="Controlled Abstention: No verified evidence available to synthesize an answer.",
                claims=[],
                unresolved_subquestions=[],
                failure_reason="Safety gate abstained or zero verified excerpts available.",
            )

        settings = get_settings()
        max_chunks = settings.max_synthesis_evidence_chunks

        # 2. Construct bounded SynthesisInput from verified excerpts
        evidence_items: List[SynthesisEvidenceItem] = []
        for exc in verified_excerpts[:max_chunks]:
            evidence_items.append(
                SynthesisEvidenceItem(
                    chunk_id=exc.source_chunk_id,
                    document_id=exc.document_id,
                    text=exc.excerpt_text,
                    page_start=exc.page_start,
                    page_end=exc.page_end,
                    circular_number=exc.circular_number,
                    title=exc.title,
                    issue_date=exc.issue_date,
                    regulatory_status=exc.regulatory_status.value,
                    status_reason=exc.status_reason,
                    mapped_provisions=[],
                    applicable_edge_ids=exc.applicable_edge_ids,
                )
            )

        payload = SynthesisInput(
            query=query,
            evidence_items=evidence_items,
            regulatory_warnings=regulatory_warnings,
            domain=domain,
        )

        # 3. Call synthesis provider with error handling
        try:
            raw_output = self._provider.synthesize(payload)
        except SynthesisTimeoutError as e:
            logger.warning(f"Synthesis provider timed out for query: {str(e)}")
            return SynthesisOutput(
                status=SynthesisStatus.PROVIDER_FAILED,
                answer="Synthesis unavailable: The answer generation provider timed out.",
                claims=[],
                unresolved_subquestions=[],
                failure_reason="Synthesis provider execution timed out.",
            )
        except SynthesisUnavailableError as e:
            logger.warning(f"Synthesis provider unavailable for query: {str(e)}")
            return SynthesisOutput(
                status=SynthesisStatus.PROVIDER_FAILED,
                answer="Synthesis unavailable: The regulatory synthesis service is currently offline.",
                claims=[],
                unresolved_subquestions=[],
                failure_reason="Synthesis provider backend unavailable.",
            )
        except SynthesisMalformedOutputError as e:
            logger.error(f"Synthesis provider returned malformed output: {str(e)}")
            return SynthesisOutput(
                status=SynthesisStatus.PROVIDER_FAILED,
                answer="Synthesis error: The synthesis provider returned invalid output.",
                claims=[],
                unresolved_subquestions=[],
                failure_reason="Provider output failed structural validation.",
            )
        except Exception as e:
            logger.error(f"Unexpected error in synthesis provider: {str(e)}")
            return SynthesisOutput(
                status=SynthesisStatus.PROVIDER_FAILED,
                answer="Synthesis unavailable: An internal error occurred during answer generation.",
                claims=[],
                unresolved_subquestions=[],
                failure_reason="Internal synthesis execution failure.",
            )

        # 4. Mechanical citation support verification and metadata sanitization
        validated_output = sanitize_and_validate_synthesis_output(
            output=raw_output,
            input_payload=payload,
        )

        return validated_output


# Singleton getter for FastAPI dependency injection
_synthesis_service: Optional[SynthesisService] = None


def get_synthesis_service() -> SynthesisService:
    global _synthesis_service
    if _synthesis_service is None:
        _synthesis_service = SynthesisService()
    return _synthesis_service
