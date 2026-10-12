"""
Provider-neutral LLM synthesis interface (Phase 8E).

Defines:
- SynthesisProvider: Abstract base class protocol for synthesis backends.
- Provider exceptions: SynthesisTimeoutError, SynthesisUnavailableError, SynthesisMalformedOutputError.
"""
from abc import ABC, abstractmethod
from typing import Optional
from app.schemas.synthesis import SynthesisInput, SynthesisOutput


class SynthesisError(Exception):
    """Base exception for answer synthesis operations."""
    pass


class SynthesisTimeoutError(SynthesisError):
    """Raised when synthesis provider exceeds the configured execution timeout."""
    pass


class SynthesisUnavailableError(SynthesisError):
    """Raised when synthesis provider is unconfigured, offline, or unreachable."""
    pass


class SynthesisMalformedOutputError(SynthesisError):
    """Raised when synthesis provider emits output that violates schema or structure."""
    pass


class SynthesisProvider(ABC):
    """
    Abstract interface for provider-neutral answer synthesis.
    All implementations must be stateless and adhere strictly to SynthesisInput -> SynthesisOutput.
    """

    @abstractmethod
    def synthesize(self, payload: SynthesisInput) -> SynthesisOutput:
        """
        Synthesizes an answer grounded strictly in supplied evidence.

        Args:
            payload: Typed input containing normalized query and verified evidence items.

        Returns:
            SynthesisOutput containing grounded answer, claim citations, and status.

        Raises:
            SynthesisTimeoutError: If execution exceeds timeout limit.
            SynthesisUnavailableError: If provider backend is not operational.
            SynthesisMalformedOutputError: If provider returns unparseable or schema-violating data.
        """
        pass
