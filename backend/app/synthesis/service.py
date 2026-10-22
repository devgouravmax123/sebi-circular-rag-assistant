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


def _create_default_provider() -> SynthesisProvider:
    """Creates the default configured synthesis provider based on environment settings."""
    settings = get_settings()
    provider_name = (settings.synthesis_provider or "mock").strip().lower()

    if provider_name == "gemini":
        api_key = settings.gemini_api_key
        if not api_key or not api_key.strip():
            logger.error("SYNTHESIS_PROVIDER is set to 'gemini' but GEMINI_API_KEY is not configured.")
            raise ValueError(
                "Configuration error: SYNTHESIS_PROVIDER is set to 'gemini' but GEMINI_API_KEY is not configured in the backend environment."
            )
        from app.synthesis.gemini_provider import GeminiSynthesisProvider
        return GeminiSynthesisProvider(
            api_key=api_key,
            model=settings.gemini_model,
            timeout_seconds=settings.synthesis_timeout_seconds,
        )

    if provider_name == "groq":
        api_key = settings.groq_api_key
        if not api_key or not api_key.strip():
            logger.error("SYNTHESIS_PROVIDER is set to 'groq' but GROQ_API_KEY is not configured.")
            raise ValueError(
                "Configuration error: SYNTHESIS_PROVIDER is set to 'groq' but GROQ_API_KEY is not configured in the backend environment."
            )
        from app.synthesis.groq_provider import GroqSynthesisProvider
        return GroqSynthesisProvider(
            api_key=api_key,
            model=settings.groq_model,
            timeout_seconds=settings.synthesis_timeout_seconds,
        )

    return MockSynthesisProvider()


class SynthesisService:
    """
    High-level orchestration service for answer synthesis.
    """

    def __init__(self, provider: Optional[SynthesisProvider] = None):
        self._provider = provider or _create_default_provider()

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
        collection_id: Optional[str] = "sebi_cscrf",
        policy_type: Optional[str] = None,
    ) -> SynthesisOutput:
        """
        Executes bounded, grounded synthesis over verified regulatory excerpts.

        Rule:
        - If disposition is CONTROLLED_ABSTENTION or REJECTED_UNSAFE, or if zero excerpts exist,
          synthesis is NOT called; a safe abstention output is returned immediately.
        - policy_type must be explicitly provided and valid (e.g. sebi_cscrf_strict, general_static).
          Missing, empty, or unrecognized policy fails closed safely.
        """
        # 1. Safety Gate Pre-Check: Do NOT call provider if gate abstained or rejected
        if disposition in (AnswerDisposition.CONTROLLED_ABSTENTION, AnswerDisposition.REJECTED_UNSAFE) or not verified_excerpts:
            logger.info(
                "Synthesis skipped (pre-provider abstention): disposition=%s, verified_excerpts=%d",
                disposition.value if hasattr(disposition, "value") else str(disposition),
                len(verified_excerpts) if verified_excerpts else 0,
            )
            return SynthesisOutput(
                status=SynthesisStatus.ABSTAINED,
                answer="Controlled Abstention: No verified evidence available to synthesize an answer.",
                claims=[],
                unresolved_subquestions=[],
                failure_reason="Safety gate abstained or zero verified excerpts available.",
            )

        # 2. Policy-Resolution & Fail-Closed Validation
        if not policy_type or not policy_type.strip():
            logger.error("Synthesis rejected: missing or empty policy_type (fail-closed)")
            return SynthesisOutput(
                status=SynthesisStatus.ABSTAINED,
                answer="Controlled Abstention: Synthesis policy could not be resolved from collection manifest.",
                claims=[],
                unresolved_subquestions=[],
                failure_reason="Missing authoritative grounding policy_type.",
            )

        norm_policy = policy_type.strip().lower()
        if norm_policy not in ("sebi_cscrf_strict", "general_static"):
            logger.error("Synthesis rejected: unsupported policy_type '%s' (fail-closed)", policy_type)
            return SynthesisOutput(
                status=SynthesisStatus.ABSTAINED,
                answer="Controlled Abstention: Unrecognized grounding policy type for target collection.",
                claims=[],
                unresolved_subquestions=[],
                failure_reason=f"Unsupported grounding policy_type: '{policy_type}'.",
            )

        settings = get_settings()
        max_chunks = settings.max_synthesis_evidence_chunks

        # 3. Construct bounded SynthesisInput from verified excerpts
        evidence_items: List[SynthesisEvidenceItem] = []
        for exc in verified_excerpts[:max_chunks]:
            exc_coll_id = getattr(exc, "collection_id", None) or collection_id or "sebi_cscrf"
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
                    collection_id=exc_coll_id,
                )
            )

        provider_name = self._provider.__class__.__name__
        logger.info(
            "Starting answer synthesis: provider=%s, evidence_chunks=%d, collection=%s, policy=%s",
            provider_name,
            len(evidence_items),
            collection_id,
            norm_policy,
        )

        payload = SynthesisInput(
            query=query,
            evidence_items=evidence_items,
            regulatory_warnings=regulatory_warnings,
            domain=domain,
            collection_id=collection_id or "sebi_cscrf",
            policy_type=norm_policy,
        )

        # 3. Call synthesis provider with error handling
        try:
            raw_output = self._provider.synthesize(payload)
        except SynthesisTimeoutError as e:
            logger.warning(
                "Synthesis provider timed out: provider=%s, error=%s",
                provider_name,
                str(e),
            )
            return SynthesisOutput(
                status=SynthesisStatus.PROVIDER_FAILED,
                answer="Synthesis unavailable: The answer generation provider timed out.",
                claims=[],
                unresolved_subquestions=[],
                failure_reason="Synthesis provider execution timed out.",
            )
        except SynthesisUnavailableError as e:
            logger.warning(
                "Synthesis provider unavailable: provider=%s, error=%s",
                provider_name,
                str(e),
            )
            return SynthesisOutput(
                status=SynthesisStatus.PROVIDER_FAILED,
                answer="Synthesis unavailable: The regulatory synthesis service is currently offline.",
                claims=[],
                unresolved_subquestions=[],
                failure_reason="Synthesis provider backend unavailable.",
            )
        except SynthesisMalformedOutputError as e:
            logger.error(
                "Synthesis provider returned malformed output: provider=%s, error=%s",
                provider_name,
                str(e),
            )
            return SynthesisOutput(
                status=SynthesisStatus.PROVIDER_FAILED,
                answer="Synthesis error: The synthesis provider returned invalid output.",
                claims=[],
                unresolved_subquestions=[],
                failure_reason="Provider output failed structural validation.",
            )
        except Exception as e:
            logger.error(
                "Unexpected error in synthesis provider: provider=%s, error=%s",
                provider_name,
                str(e),
            )
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

        if validated_output.status == SynthesisStatus.UNSUPPORTED_CLAIMS_REJECTED:
            logger.warning(
                "Synthesis output rejected by citation/grounding validation: provider=%s, raw_status=%s, raw_claims=%d",
                provider_name,
                raw_output.status.value,
                len(raw_output.claims),
            )
        else:
            logger.info(
                "Synthesis completed successfully: provider=%s, status=%s, validated_claims=%d, unresolved_subquestions=%d",
                provider_name,
                validated_output.status.value,
                len(validated_output.claims),
                len(validated_output.unresolved_subquestions),
            )

        return validated_output


# Singleton getter for FastAPI dependency injection
_synthesis_service: Optional[SynthesisService] = None


def get_synthesis_service() -> SynthesisService:
    global _synthesis_service
    if _synthesis_service is None:
        _synthesis_service = SynthesisService()
    return _synthesis_service
