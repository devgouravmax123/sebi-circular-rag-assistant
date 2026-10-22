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
from typing import Optional, List, Dict, Any, cast
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
    _clean_prompt_passage_text,
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
        "usage": cast(Dict[str, Any], {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}),
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
    assert isinstance(max_tokens, int) and max_tokens == 768
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


# 15. Usage metrics logging on successful generation
def test_15_usage_metrics_logged_correctly(sample_synthesis_input, caplog):
    resp_with_usage = _make_groq_response_json()
    resp_with_usage["usage"] = {
        "prompt_tokens": 350,
        "completion_tokens": 80,
        "total_tokens": 430,
        "prompt_tokens_details": {"cached_tokens": 120},
    }

    def usage_handler(request: httpx.Request):
        return httpx.Response(200, json=resp_with_usage)

    client = httpx.Client(transport=httpx.MockTransport(usage_handler))
    provider = GroqSynthesisProvider(api_key=DUMMY_GROQ_KEY, timeout_seconds=10.0, http_client=client)

    with caplog.at_level(logging.INFO):
        output = provider.synthesize(sample_synthesis_input)

    assert output.status == SynthesisStatus.FULLY_ANSWERED
    log_messages = [rec.message for rec in caplog.records if "Groq API generation succeeded" in rec.message]
    assert len(log_messages) == 1
    msg = log_messages[0]
    assert "prompt_tokens=350" in msg
    assert "completion_tokens=80" in msg
    assert "total_tokens=430" in msg
    assert "cached_tokens=120" in msg


# 16. Missing or malformed usage metadata handled safely without breaking synthesis
def test_16_missing_or_malformed_usage_handled_safely(sample_synthesis_input, caplog):
    # Test with completely missing usage field
    resp_no_usage = _make_groq_response_json()
    del resp_no_usage["usage"]

    def no_usage_handler(request: httpx.Request):
        return httpx.Response(200, json=resp_no_usage)

    client = httpx.Client(transport=httpx.MockTransport(no_usage_handler))
    provider = GroqSynthesisProvider(api_key=DUMMY_GROQ_KEY, timeout_seconds=10.0, http_client=client)

    with caplog.at_level(logging.INFO):
        output = provider.synthesize(sample_synthesis_input)

    assert output.status == SynthesisStatus.FULLY_ANSWERED
    log_messages = [rec.message for rec in caplog.records if "Groq API generation succeeded" in rec.message]
    assert len(log_messages) == 1
    assert "prompt_tokens=N/A" in log_messages[0]

    # Test with malformed usage values (strings/objects)
    resp_malformed = _make_groq_response_json()
    resp_malformed["usage"] = {
        "prompt_tokens": "invalid_num",
        "completion_tokens": None,
        "total_tokens": {},
        "cached_tokens": "bad",
    }

    def malformed_usage_handler(request: httpx.Request):
        return httpx.Response(200, json=resp_malformed)

    client2 = httpx.Client(transport=httpx.MockTransport(malformed_usage_handler))
    provider2 = GroqSynthesisProvider(api_key=DUMMY_GROQ_KEY, timeout_seconds=10.0, http_client=client2)

    with caplog.at_level(logging.INFO):
        output2 = provider2.synthesize(sample_synthesis_input)

    assert output2.status == SynthesisStatus.FULLY_ANSWERED


# 17. Daily quota exhaustion (TPD/RPD) fails fast on attempt 1 without retry
def test_17_daily_quota_exhaustion_fails_fast_without_retry(sample_synthesis_input):
    quota_error_messages = [
        "Rate limit reached for model `llama-3.3-70b-versatile` on tokens per day (TPD): Limit 500000, Used 501000",
        "Requests per day (RPD) limit exceeded. Please wait until tomorrow.",
        "Daily quota exceeded for organization.",
    ]

    for err_text in quota_error_messages:
        call_count = 0

        def quota_handler(request: httpx.Request):
            nonlocal call_count
            call_count += 1
            return httpx.Response(
                429,
                json={"error": {"code": "rate_limit_exceeded", "message": err_text}},
            )

        client = httpx.Client(transport=httpx.MockTransport(quota_handler))
        provider = GroqSynthesisProvider(api_key=DUMMY_GROQ_KEY, timeout_seconds=10.0, http_client=client)

        with patch("time.sleep") as mock_sleep:
            with pytest.raises(SynthesisUnavailableError) as exc:
                provider.synthesize(sample_synthesis_input)

        # Must fail fast on attempt 1 without retrying
        assert call_count == 1, f"Expected 1 call for daily quota message '{err_text}', got {call_count}"
        assert mock_sleep.call_count == 0
        assert "quota exhausted or rate limit exceeded" in str(exc.value).lower()


# 18. Transient 429 (RPM/TPM) still retries with backoff
def test_18_transient_rate_limit_still_retries(sample_synthesis_input):
    call_count = 0
    valid_resp = _make_groq_response_json()

    def transient_handler(request: httpx.Request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(
                429,
                headers={"retry-after": "1.0"},
                json={"error": {"code": "rate_limit_exceeded", "message": "Rate limit reached for requests per minute (RPM)"}},
            )
        return httpx.Response(200, json=valid_resp)

    client = httpx.Client(transport=httpx.MockTransport(transient_handler))
    provider = GroqSynthesisProvider(api_key=DUMMY_GROQ_KEY, timeout_seconds=10.0, http_client=client)

    with patch("time.sleep") as mock_sleep:
        output = provider.synthesize(sample_synthesis_input)

    assert call_count == 2
    assert mock_sleep.call_count == 1
    assert output.status == SynthesisStatus.FULLY_ANSWERED


# 19. Title deduplicated for multiple excerpts from same document_id
def test_19_title_deduplicated_for_same_document():
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=_make_groq_response_json())))
    provider = GroqSynthesisProvider(api_key=DUMMY_GROQ_KEY, timeout_seconds=10.0, http_client=client)

    input_payload = SynthesisInput(
        query="What are the audit requirements under CSCRF?",
        evidence_items=[
            SynthesisEvidenceItem(
                chunk_id="cscrf_p144_0",
                document_id="sebi_cscrf_2024_113",
                circular_number="SEBI/HO/ITD/ITD_PoD-1/P/CIR/2024/113",
                title="Master Circular on Cybersecurity and Cyber Resilience Framework (CSCRF)",
                page_start=144,
                regulatory_status=RegulatoryStatus.ACTIVE_CURRENT.value,
                status_reason="Active",
                text="Excerpt 1: The audit scope shall cover IT infrastructure and systems.",
            ),
            SynthesisEvidenceItem(
                chunk_id="cscrf_p145_1",
                document_id="sebi_cscrf_2024_113",
                circular_number="SEBI/HO/ITD/ITD_PoD-1/P/CIR/2024/113",
                title="Master Circular on Cybersecurity and Cyber Resilience Framework (CSCRF)",
                page_start=145,
                regulatory_status=RegulatoryStatus.ACTIVE_CURRENT.value,
                status_reason="Active",
                text="Excerpt 2: The audit report must specify deadline of corrective actions within 30 days.",
            ),
        ],
        regulatory_warnings=["Warning: historical circulars referenced."],
    )

    prompt = provider._build_user_prompt(input_payload)

    # Title appears exactly once across both excerpts
    assert prompt.count("Title: Master Circular on Cybersecurity and Cyber Resilience Framework (CSCRF)") == 1

    # Both chunk IDs, sources, and page provenance are fully preserved
    assert "--- EXCERPT #1 [Chunk ID: cscrf_p144_0] ---" in prompt
    assert "Source Circular: SEBI/HO/ITD/ITD_PoD-1/P/CIR/2024/113 | Page 144 | Status: ACTIVE_CURRENT" in prompt
    assert "Excerpt 1: The audit scope shall cover IT infrastructure and systems." in prompt

    assert "--- EXCERPT #2 [Chunk ID: cscrf_p145_1] ---" in prompt
    assert "Source Circular: SEBI/HO/ITD/ITD_PoD-1/P/CIR/2024/113 | Page 145 | Status: ACTIVE_CURRENT" in prompt
    assert "Excerpt 2: The audit report must specify deadline of corrective actions within 30 days." in prompt

    # Warning and task instructions remain intact
    assert "APPLICABLE REGULATORY CAVEATS / SCOPE LIMITATIONS:" in prompt
    assert "- Warning: historical circulars referenced." in prompt
    assert "TASK: Provide a synthesized response adhering strictly to the above facts and rules." in prompt


# 20. Distinct documents each retain their own title and source identity
def test_20_distinct_documents_retain_separate_titles():
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=_make_groq_response_json())))
    provider = GroqSynthesisProvider(api_key=DUMMY_GROQ_KEY, timeout_seconds=10.0, http_client=client)

    input_payload = SynthesisInput(
        query="How does the amending circular change the original requirements?",
        evidence_items=[
            SynthesisEvidenceItem(
                chunk_id="cscrf_2024_p1",
                document_id="doc_cscrf_2024",
                circular_number="SEBI/CIR/2024/113",
                title="Master Circular on CSCRF",
                page_start=1,
                regulatory_status="SUPERSEDED",
                status_reason="Superseded in part",
                text="Original requirement: submit report within 60 days.",
            ),
            SynthesisEvidenceItem(
                chunk_id="cscrf_amend_2025_p1",
                document_id="doc_cscrf_amend_2025",
                circular_number="SEBI/CIR/2025/45",
                title="Amendment to Master Circular on CSCRF",
                page_start=1,
                regulatory_status="ACTIVE_CURRENT",
                status_reason="Active amendment",
                text="Amended requirement: deadline extended to 90 days.",
            ),
            SynthesisEvidenceItem(
                chunk_id="cscrf_amend_2025_p2",
                document_id="doc_cscrf_amend_2025",
                circular_number="SEBI/CIR/2025/45",
                title="Amendment to Master Circular on CSCRF",
                page_start=2,
                regulatory_status="ACTIVE_CURRENT",
                status_reason="Active amendment",
                text="Second page of amendment with additional guidelines.",
            ),
        ],
    )

    prompt = provider._build_user_prompt(input_payload)

    # Each distinct document shows its title once
    assert prompt.count("Title: Master Circular on CSCRF") == 1
    assert prompt.count("Title: Amendment to Master Circular on CSCRF") == 1

    # Statuses correctly preserved on respective excerpts
    assert "Source Circular: SEBI/CIR/2024/113 | Page 1 | Status: SUPERSEDED" in prompt
    assert "Source Circular: SEBI/CIR/2025/45 | Page 1 | Status: ACTIVE_CURRENT" in prompt
    assert "Source Circular: SEBI/CIR/2025/45 | Page 2 | Status: ACTIVE_CURRENT" in prompt


# 21. Preservation of complex table content, numbers, dates, and deadlines byte-for-byte
def test_21_table_numbers_deadlines_preserved_byte_for_byte():
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=_make_groq_response_json())))
    provider = GroqSynthesisProvider(api_key=DUMMY_GROQ_KEY, timeout_seconds=10.0, http_client=client)

    verbatim_table_text = (
        "| S.No | Standard | Criticality | Deadline | Management Response |\n"
        "| 1    | GV.OC.S1 | CRITICAL    | 15 days  | Action plan approved |\n"
        "| 2    | GV.OC.S2 | HIGH        | 30 days  | Vendor notified      |\n"
        "Total compliance score: 98.5% as of 2024-11-01."
    )

    input_payload = SynthesisInput(
        query="What are the compliance deadlines?",
        evidence_items=[
            SynthesisEvidenceItem(
                chunk_id="table_chunk_101",
                document_id="doc_audit_table",
                circular_number="CIR/2024/TABLE",
                title="Audit Report Formats",
                page_start=12,
                regulatory_status="ACTIVE_CURRENT",
                status_reason="Active",
                text=verbatim_table_text,
            ),
        ],
    )

    prompt = provider._build_user_prompt(input_payload)
    # The table text must be present byte-for-byte
    assert verbatim_table_text in prompt


# 22. Missing metadata (no title, no circular number, unlisted page) handled safely
def test_22_missing_metadata_handled_gracefully():
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=_make_groq_response_json())))
    provider = GroqSynthesisProvider(api_key=DUMMY_GROQ_KEY, timeout_seconds=10.0, http_client=client)

    input_payload = SynthesisInput(
        query="Query with sparse metadata",
        evidence_items=[
            SynthesisEvidenceItem(
                chunk_id="sparse_chunk_1",
                document_id="doc_sparse",
                circular_number=None,
                title=None,
                page_start=None,
                regulatory_status="UNRESOLVED",
                status_reason="Unresolved status",
                text="Verbatim text with minimal metadata.",
            ),
            SynthesisEvidenceItem(
                chunk_id="sparse_chunk_2",
                document_id="",
                circular_number=None,
                title="",
                page_start=None,
                regulatory_status="UNRESOLVED",
                status_reason="Unresolved status",
                text="Second sparse excerpt text.",
            ),
        ],
    )

    prompt = provider._build_user_prompt(input_payload)
    assert "--- EXCERPT #1 [Chunk ID: sparse_chunk_1] ---" in prompt
    assert "Source Circular: doc_sparse | Page unlisted | Status: UNRESOLVED" in prompt
    assert "Verbatim text with minimal metadata." in prompt

    assert "--- EXCERPT #2 [Chunk ID: sparse_chunk_2] ---" in prompt
    assert "Source Circular:  | Page unlisted | Status: UNRESOLVED" in prompt
    assert "Second sparse excerpt text." in prompt
    assert "Title:" not in prompt


# 23. Direct unit test of _clean_prompt_passage_text for Annexure-B and main body headers
def test_23_clean_prompt_passage_text_removes_running_headers():
    # Annexure-B running header
    sample_annexure_b = (
        "3.3. Any other specific item(s) CSCRF Annexure-B Page 146 of 205 Version 1.0 "
        "4. Methodology/ Audit approach (audit subject identification, pre-audit planning)"
    )
    cleaned_annexure_b = _clean_prompt_passage_text(sample_annexure_b)
    assert "CSCRF Annexure-B Page 146 of 205 Version 1.0" not in cleaned_annexure_b
    assert (
        cleaned_annexure_b
        == "3.3. Any other specific item(s) 4. Methodology/ Audit approach (audit subject identification, pre-audit planning)"
    )

    # Main body running header (without Annexure tag)
    sample_main_body = (
        "Perform Hotwash CSCRF Page 152 of 205 Version 1.0 Part IV: CSCRF Annexures and References"
    )
    cleaned_main_body = _clean_prompt_passage_text(sample_main_body)
    assert "CSCRF Page 152 of 205 Version 1.0" not in cleaned_main_body
    assert cleaned_main_body == "Perform Hotwash Part IV: CSCRF Annexures and References"

    # Multiple headers in a single passage
    sample_multi = (
        "Start text. CSCRF Annexure-A Page 133 of 205 Version 1.0 Middle text. "
        "CSCRF Annexure-A Page 134 of 205 Version 1.0 End text."
    )
    cleaned_multi = _clean_prompt_passage_text(sample_multi)
    assert cleaned_multi == "Start text. Middle text. End text."


# 24. Clean text preservation: non-matching text, substantive titles, and clause numbers survive
def test_24_substantive_titles_clauses_deadlines_survive_cleaning():
    text_with_substantive_data = (
        "Annexure-B: Cyber Audit Report Format\n"
        "6. Control-wise Compliance status of SEBI CSCRF:\n"
        "Clause 9.1: Audit report shall provide terms of reference (ToR).\n"
        "Clause 9.2: Audit report shall include open observations.\n"
        "Standard GV.OC.S1: Criticality HIGH, Deadline of corrective action(s) is 15 days.\n"
        "Standard GV.OC.S2: Deadline 30 days. Management response required.\n"
        "Compliance score: 98.5%."
    )
    cleaned = _clean_prompt_passage_text(text_with_substantive_data)
    # Entire substantive text must survive identically
    assert cleaned == text_with_substantive_data
    assert "Annexure-B: Cyber Audit Report Format" in cleaned
    assert "9.1" in cleaned
    assert "9.2" in cleaned
    assert "GV.OC.S1" in cleaned
    assert "15 days" in cleaned
    assert "30 days" in cleaned
    assert "98.5%" in cleaned


# 25. Table content in Annexure-K survives intact
def test_25_annexure_k_table_content_survives():
    raw_annexure_k_with_header = (
        "18% CSCRF Annexure-K Page 166 of 205 Version 1.0 S No Measure ID Goal/Objective Measure Measure Type "
        "Formula Target Implementation Evidence Weightage Self-assessment score Auditor comments w.r.t. cyber audit (for MIIs) "
        "2. VAPT report and its closure report. 3. Time taken to close the identified vulnerabilities."
    )
    cleaned = _clean_prompt_passage_text(raw_annexure_k_with_header)
    assert "CSCRF Annexure-K Page 166 of 205 Version 1.0" not in cleaned
    assert "18% S No Measure ID Goal/Objective Measure" in cleaned
    assert "VAPT report and its closure report." in cleaned
    assert "Auditor comments w.r.t. cyber audit (for MIIs)" in cleaned


# 26. Original SynthesisEvidenceItem.text is never mutated by prompt building
def test_26_original_evidence_text_immutable():
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=_make_groq_response_json())))
    provider = GroqSynthesisProvider(api_key=DUMMY_GROQ_KEY, timeout_seconds=10.0, http_client=client)

    original_verbatim_text = (
        "3.3. Any other specific item(s) CSCRF Annexure-B Page 146 of 205 Version 1.0 4. Methodology/ Audit approach"
    )
    evidence_item = SynthesisEvidenceItem(
        chunk_id="chunk_audit_146",
        document_id="sebi_cscrf_2024_113",
        circular_number="SEBI/HO/ITD/ITD_PoD-1/P/CIR/2024/113",
        title="Master Circular on CSCRF",
        page_start=146,
        regulatory_status="ACTIVE_CURRENT",
        status_reason="Active",
        text=original_verbatim_text,
    )
    payload = SynthesisInput(
        query="What is the audit methodology?",
        evidence_items=[evidence_item],
    )

    prompt = provider._build_user_prompt(payload)

    # 1. Prompt has the cleaned copy
    assert "CSCRF Annexure-B Page 146 of 205 Version 1.0" not in prompt
    assert "3.3. Any other specific item(s) 4. Methodology/ Audit approach" in prompt

    # 2. Original item.text in memory remains 100% byte-for-byte identical
    assert evidence_item.text == original_verbatim_text
    assert "CSCRF Annexure-B Page 146 of 205 Version 1.0" in evidence_item.text


# 27. Similar but non-matching text is preserved
def test_27_similar_non_matching_text_preserved():
    # Different version format, missing "of", or substantive regulatory references to CSCRF
    non_matching_cases = [
        "Refer to CSCRF Annexure-B for format details.",
        "CSCRF Version 1.0 was published in 2024.",
        "See Page 146 of CSCRF guidelines.",
        "CSCRF Annexure-B Page 146 Version 1.0",  # missing "of <total>"
    ]
    for case in non_matching_cases:
        assert _clean_prompt_passage_text(case) == case
