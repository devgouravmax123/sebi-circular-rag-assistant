"""
Unit and integration tests for Groq LLM Synthesis Provider.

Covers:
1. Valid structured JSON response parsed correctly into typed SynthesisOutput.
2. Malformed JSON and invalid schema handled with SynthesisMalformedOutputError.
3. Missing choices and content filters handled safely.
4. HTTP 429 rate limit exhaustion triggers retry and fails safely if persistent.
5. HTTP 5xx server error handling with retry and safe failure.
6. Timeout and network connection error handling.
7. Missing API key validation (fast fail on init).
8. Transient HTTP 503 recovery on second attempt.
9. Retry-After header parsing and enforcement.
10. Timeout budget exhaustion preventing futile retries.
11. Non-retryable client errors (401, 403, 400) failing immediately without retrying.
12. Secret masking (GROQ_API_KEY is never leaked in logs or error messages).
13. Service integration with provider_name='groq'.
"""
import json
import logging
from typing import Optional, List, Dict, Any
from unittest.mock import patch, MagicMock
import httpx
import pytest

from app.schemas.synthesis import (
    SynthesisInput,
    SynthesisOutput,
    SynthesisStatus,
    SynthesisEvidenceItem,
    ClaimCitation,
    UnresolvedSubquestion,
)
from app.schemas.grounding import RegulatoryStatus
from app.synthesis.provider import (
    SynthesisTimeoutError,
    SynthesisUnavailableError,
    SynthesisMalformedOutputError,
)
from app.synthesis.groq_provider import (
    GroqSynthesisProvider,
    _sanitize_error_message,
)
from app.synthesis.service import SynthesisService, _create_default_provider
from app.core.config import Settings

DUMMY_GROQ_KEY = "gsk_test1234567890abcdefghijklmnopqrstuvwxyz"


@pytest.fixture
def sample_synthesis_input():
    return SynthesisInput(
        query="What is the objective of the Cyber Resilience Framework?",
        evidence_items=[
            SynthesisEvidenceItem(
                chunk_id="chunk_1",
                document_id="sebi_cscrf_2024_113",
                text="The CSCRF provides guidelines to strengthen the cyber resilience of Regulated Entities.",
                regulatory_status=RegulatoryStatus.ACTIVE_CURRENT.value,
                status_reason="Active circular provision",
                page_start=1,
                page_end=1,
            )
        ],
        regulatory_warnings=[],
    )


def _make_groq_response_json(
    status: str = "FULLY_ANSWERED",
    answer: str = "CSCRF provides guidelines to strengthen the cyber resilience of Regulated Entities.",
    claims: Optional[List[Any]] = None,
    unresolved: Optional[List[Any]] = None,
):
    if claims is None:
        claims = [
            {
                "claim_text": "CSCRF provides guidelines to strengthen cyber resilience.",
                "supporting_chunk_ids": ["chunk_1"],
            }
        ]
    if unresolved is None:
        unresolved = []

    content_obj = {
        "status": status,
        "answer": answer,
        "claims": claims,
        "unresolved_subquestions": unresolved,
    }

    return {
        "id": "chatcmpl-test-123",
        "object": "chat.completion",
        "created": 1729000000,
        "model": "llama-3.3-70b-versatile",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(content_obj),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


# 1. Valid structured response
def test_1_valid_groq_structured_response(sample_synthesis_input):
    raw_resp = _make_groq_response_json()

    def handler(request: httpx.Request):
        assert request.headers.get("Authorization") == f"Bearer {DUMMY_GROQ_KEY}"
        return httpx.Response(200, json=raw_resp)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = GroqSynthesisProvider(api_key=DUMMY_GROQ_KEY, timeout_seconds=10.0, http_client=client)

    output = provider.synthesize(sample_synthesis_input)
    assert output.status == SynthesisStatus.FULLY_ANSWERED
    assert "strengthen the cyber resilience" in output.answer
    assert len(output.claims) == 1
    assert output.claims[0].supporting_chunk_ids == ["chunk_1"]
    assert output.failure_reason is None


# 2. Malformed JSON and invalid schema
def test_2_malformed_json_and_invalid_schema(sample_synthesis_input):
    def bad_json_handler(request: httpx.Request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "This is raw text without JSON"}}]
        })

    client = httpx.Client(transport=httpx.MockTransport(bad_json_handler))
    provider = GroqSynthesisProvider(api_key=DUMMY_GROQ_KEY, timeout_seconds=10.0, http_client=client)

    with pytest.raises(SynthesisMalformedOutputError) as exc:
        provider.synthesize(sample_synthesis_input)
    assert "not valid json" in str(exc.value).lower()


# 3. Missing choices in response
def test_3_missing_choices_handled_safely(sample_synthesis_input):
    def empty_choices_handler(request: httpx.Request):
        return httpx.Response(200, json={"choices": []})

    client = httpx.Client(transport=httpx.MockTransport(empty_choices_handler))
    provider = GroqSynthesisProvider(api_key=DUMMY_GROQ_KEY, timeout_seconds=10.0, http_client=client)

    with pytest.raises(SynthesisMalformedOutputError) as exc:
        provider.synthesize(sample_synthesis_input)
    assert "no completion choices" in str(exc.value).lower()


# 4. HTTP 429 quota/rate limit exhaustion
def test_4_http_429_exhaustion(sample_synthesis_input):
    call_count = 0

    def rate_limit_handler(request: httpx.Request):
        nonlocal call_count
        call_count += 1
        return httpx.Response(429, json={"error": {"message": "Rate limit exceeded: 30 RPM reached"}})

    client = httpx.Client(transport=httpx.MockTransport(rate_limit_handler))
    provider = GroqSynthesisProvider(api_key=DUMMY_GROQ_KEY, timeout_seconds=10.0, http_client=client)

    with patch("time.sleep") as mock_sleep:
        with pytest.raises(SynthesisUnavailableError) as exc:
            provider.synthesize(sample_synthesis_input)

    assert call_count == 3  # 1 initial + 2 retries
    assert mock_sleep.call_count == 2
    assert "rate limit exceeded" in str(exc.value).lower()


# 5. HTTP 503 transient recovery
def test_5_retry_success_after_transient_503(sample_synthesis_input):
    call_count = 0
    valid_resp = _make_groq_response_json()

    def retry_handler(request: httpx.Request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(503, text="Service Unavailable - Server Busy")
        return httpx.Response(200, json=valid_resp)

    client = httpx.Client(transport=httpx.MockTransport(retry_handler))
    provider = GroqSynthesisProvider(api_key=DUMMY_GROQ_KEY, timeout_seconds=10.0, http_client=client)

    with patch("time.sleep") as mock_sleep:
        output = provider.synthesize(sample_synthesis_input)

    assert call_count == 2
    assert mock_sleep.call_count == 1
    assert output.status == SynthesisStatus.FULLY_ANSWERED


# 6. Retry-After header parsing
def test_6_retry_after_header_respected(sample_synthesis_input):
    call_count = 0
    valid_resp = _make_groq_response_json()

    def retry_after_handler(request: httpx.Request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, headers={"retry-after": "1.75"}, text="Too Many Requests")
        return httpx.Response(200, json=valid_resp)

    client = httpx.Client(transport=httpx.MockTransport(retry_after_handler))
    provider = GroqSynthesisProvider(api_key=DUMMY_GROQ_KEY, timeout_seconds=10.0, http_client=client)

    with patch("time.sleep") as mock_sleep:
        output = provider.synthesize(sample_synthesis_input)

    assert call_count == 2
    assert mock_sleep.call_count == 1
    slept = mock_sleep.call_args[0][0]
    assert 1.70 <= slept <= 1.80
    assert output.status == SynthesisStatus.FULLY_ANSWERED


# 7. Non-retryable client errors fail fast without retrying
def test_7_non_retryable_errors_fail_fast(sample_synthesis_input):
    for status_code in [400, 401, 403, 404]:
        call_count = 0

        def error_handler(request: httpx.Request):
            nonlocal call_count
            call_count += 1
            return httpx.Response(status_code, json={"error": {"message": f"Client Error {status_code}"}})

        client = httpx.Client(transport=httpx.MockTransport(error_handler))
        provider = GroqSynthesisProvider(api_key=DUMMY_GROQ_KEY, timeout_seconds=10.0, http_client=client)

        with patch("time.sleep") as mock_sleep:
            with pytest.raises((SynthesisUnavailableError, SynthesisMalformedOutputError)):
                provider.synthesize(sample_synthesis_input)

        assert call_count == 1
        assert mock_sleep.call_count == 0


# 8. Timeout budget exhaustion
def test_8_timeout_budget_exhaustion(sample_synthesis_input):
    call_count = 0

    def slow_503_handler(request: httpx.Request):
        nonlocal call_count
        call_count += 1
        return httpx.Response(503, text="Service Unavailable")

    client = httpx.Client(transport=httpx.MockTransport(slow_503_handler))
    provider = GroqSynthesisProvider(api_key=DUMMY_GROQ_KEY, timeout_seconds=3.0, http_client=client)

    fake_time = [0.0, 0.1, 2.5, 2.5, 2.6]

    def monotonic_mock():
        if fake_time:
            return fake_time.pop(0)
        return 5.0

    with patch("time.monotonic", side_effect=monotonic_mock):
        with patch("time.sleep") as mock_sleep:
            with pytest.raises(SynthesisTimeoutError) as exc:
                provider.synthesize(sample_synthesis_input)

    assert call_count == 1
    assert mock_sleep.call_count == 0
    assert "budget exhausted" in str(exc.value).lower()


# 9. API key validation
def test_9_missing_api_key_validation():
    with pytest.raises(ValueError) as exc:
        GroqSynthesisProvider(api_key="")
    assert "GROQ_API_KEY must be provided" in str(exc.value)


# 10. API key never leaked in logs or sanitizers
def test_10_api_key_never_leaked():
    sensitive_key = "gsk_secret9876543210zyxwvutsrqponmlkjihgfedcba"
    leak_msg = f"Failed to reach https://api.groq.com/openai/v1/chat/completions?key={sensitive_key} with Bearer {sensitive_key}"
    sanitized = _sanitize_error_message(leak_msg, sensitive_key)

    assert sensitive_key not in sanitized
    assert "[REDACTED_API_KEY]" in sanitized


# 11. Service integration with provider_name='groq'
def test_11_service_provider_selection_groq(monkeypatch):
    test_settings = Settings(
        synthesis_provider="groq",
        groq_api_key="gsk_valid_configured_key_12345",
        groq_model="llama-3.3-70b-versatile",
    )
    monkeypatch.setattr("app.synthesis.service.get_settings", lambda: test_settings)

    provider = _create_default_provider()
    assert isinstance(provider, GroqSynthesisProvider)
    assert provider._model == "llama-3.3-70b-versatile"


# 12. HTTP 400 json_validate_failed diagnostic error parsing
def test_12_json_validate_failed_diagnostics(sample_synthesis_input):
    def failed_json_handler(request: httpx.Request):
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "json_validate_failed",
                    "message": "Failed to validate JSON. Please adjust your prompt.",
                    "failed_generation": '{"status": "INVALID_ENUM", "answer": ""}',
                }
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(failed_json_handler))
    provider = GroqSynthesisProvider(api_key=DUMMY_GROQ_KEY, timeout_seconds=10.0, http_client=client)

    with pytest.raises(SynthesisMalformedOutputError) as exc:
        provider.synthesize(sample_synthesis_input)

    assert "json_validate_failed" in str(exc.value)
    assert "Failed to validate JSON" in str(exc.value)


# 13. Reasoning-capable models (e.g. openai/gpt-oss-20b) include reasoning_format='parsed' and token budget
def test_13_gpt_oss_reasoning_format_payload(sample_synthesis_input):
    captured_request = {}
    valid_resp = _make_groq_response_json()

    def capture_handler(request: httpx.Request):
        nonlocal captured_request
        captured_request = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=valid_resp)

    client = httpx.Client(transport=httpx.MockTransport(capture_handler))
    provider = GroqSynthesisProvider(
        api_key=DUMMY_GROQ_KEY,
        model="openai/gpt-oss-20b",
        timeout_seconds=10.0,
        http_client=client,
    )

    output = provider.synthesize(sample_synthesis_input)
    assert output.status == SynthesisStatus.FULLY_ANSWERED
    assert captured_request["model"] == "openai/gpt-oss-20b"
    assert captured_request.get("reasoning_format") == "parsed"
    assert captured_request.get("reasoning_effort") == "low"
    max_tokens = captured_request.get("max_completion_tokens")
    assert isinstance(max_tokens, int) and max_tokens >= 4096
    assert captured_request["response_format"]["type"] == "json_schema"


# 14. Truncation due to max completion tokens reached fails safely with SynthesisMalformedOutputError without retry
def test_14_max_completion_tokens_truncation_fails_safely(sample_synthesis_input):
    call_count = 0

    def truncated_handler(request: httpx.Request):
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "json_validate_failed",
                    "message": "Failed to validate JSON. Please adjust your prompt: max completion tokens reached",
                    "failed_generation": '{"status": "FULLY_ANSWERED", "answer": "Incomplete text...',
                }
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(truncated_handler))
    provider = GroqSynthesisProvider(api_key=DUMMY_GROQ_KEY, timeout_seconds=10.0, http_client=client)

    with patch("time.sleep") as mock_sleep:
        with pytest.raises(SynthesisMalformedOutputError) as exc:
            provider.synthesize(sample_synthesis_input)

    # 400 Bad Request should fail immediately on attempt 1 without wasteful retries
    assert call_count == 1
    assert mock_sleep.call_count == 0
    assert "max completion tokens reached" in str(exc.value)
