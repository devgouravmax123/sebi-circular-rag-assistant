"""
End-to-End Regression Test Suite for Private Document Q&A Repair.

Verifies:
1. Supported natural-language questions across uploaded documents produce accurate, citation-grounded answers (FULLY_ANSWERED).
2. Unsupported questions over administrative documents (e.g. admit cards / hall tickets) produce clean Controlled Abstention (ABSTAINED) rather than provider crashes or fake answers.
3. Multi-PDF collections containing both administrative forms and substantive guidelines prioritize substantive guidelines for explanatory questions.
4. Paraphrased natural-language questions match via domain synonyms and morphological variants.
5. Groq provider parses markdown fences, preambles, reasoning <think> blocks, trailing commas, truncated JSON, and alternative keys without crashing.
6. Groq and Gemini providers handle empty/missing 'answer' fields on ABSTAINED responses gracefully without raising SynthesisMalformedOutputError or returning PROVIDER_FAILED.
"""
import json
from typing import List, Dict, Any, Optional
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
from app.schemas.retrieval import RetrievalResult, RetrievalResponse
from app.schemas.manifest import GroundingPolicyType
from app.schemas.grounding import RegulatoryStatus
from app.schemas.safety_gate import AnswerDisposition, ValidatedExcerptItem
from app.grounding.relevance import (
    filter_relevant_candidates,
    is_administrative_form_chunk,
    check_token_match,
    SYNONYM_EXPANSIONS,
    get_token_variants,
)
from app.synthesis.groq_provider import (
    GroqSynthesisProvider,
    _extract_structured_output,
)
from app.synthesis.gemini_provider import GeminiSynthesisProvider
from app.synthesis.service import SynthesisService
from app.synthesis.provider import SynthesisMalformedOutputError

DUMMY_API_KEY = "gsk_test_mock_repair_suite_key_12345"


# ---------------------------------------------------------------------------
# FIXTURES & DATA BUILDERS
# ---------------------------------------------------------------------------

@pytest.fixture
def admit_card_chunk() -> RetrievalResult:
    """Simulates an administrative examination admit card / hall ticket chunk."""
    return RetrievalResult(
        chunk_id="chunk_admit_card_001",
        document_id="doc_admit_card_2024",
        text=(
            "Central Board of Technical Education - Admit Card for Summer Internship Examination 2024. "
            "Candidate Name: Rahul Sharma. Roll Number: CBTE-2024-88492. "
            "Examination Centre: Centre Code 402, North Campus Hall B. "
            "Date of Examination: 15-July-2024. Reporting Time: 09:00 AM. "
            "Signature of Candidate: [Signed]. Invigilator Signature: [Verified]."
        ),
        page_start=1,
        page_end=1,
        rank=1,
        score=0.75,
        metadata={"dense_score": 0.48, "collection_id": "col_internship_test"},
    )


@pytest.fixture
def internship_guidelines_chunk() -> RetrievalResult:
    """Simulates an informative internship / industrial training guidelines chunk."""
    return RetrievalResult(
        chunk_id="chunk_guideline_002",
        document_id="doc_internship_handbook_2024",
        text=(
            "Industrial Training and Internship Programme Guidelines 2024: "
            "The six-month software engineering internship is designed to immerse students "
            "in production systems engineering, backend API development, and distributed data pipelines. "
            "Interns work directly alongside senior mentors to build resilient cloud services. "
            "Eligibility criteria: Final-year undergraduate students with a cumulative GPA of 7.5 or above "
            "who have completed core coursework in data structures and computer systems."
        ),
        page_start=1,
        page_end=1,
        rank=2,
        score=0.82,
        metadata={"dense_score": 0.62, "collection_id": "col_internship_test"},
    )


# ---------------------------------------------------------------------------
# 1. RELEVANCE FILTERING & VOCABULARY GAP TESTS
# ---------------------------------------------------------------------------

def test_relevance_synonym_expansions_for_internship_and_training():
    """Confirms domain synonym expansions connect 'internship', 'training', 'trainee', etc."""
    assert "training" in SYNONYM_EXPANSIONS["internship"]
    assert "internship" in SYNONYM_EXPANSIONS["training"]
    assert "eligibility" in SYNONYM_EXPANSIONS["apply"]
    assert "requirements" in SYNONYM_EXPANSIONS["eligibility"]
    assert "allowance" in SYNONYM_EXPANSIONS["stipend"]


def test_token_variant_derivations_and_matches():
    """Confirms suffix derivations and bidirectional token matching for synonyms/variants."""
    assert "intern" in get_token_variants("internship")
    assert "enrol" in get_token_variants("enrolment")
    assert "train" in get_token_variants("trainee")
    # Verify check_token_match bridges user verbs to document nouns
    assert check_token_match("apply", "eligibility criteria for applicants") is True
    assert check_token_match("internship", "industrial training programme guidelines") is True


def test_administrative_form_detection(admit_card_chunk, internship_guidelines_chunk):
    """Confirms administrative form chunks are detected and distinguishable from guidelines."""
    assert is_administrative_form_chunk(admit_card_chunk.text) is True
    assert is_administrative_form_chunk(internship_guidelines_chunk.text) is False


def test_explanatory_query_prioritizes_guidelines_over_admit_card(
    admit_card_chunk, internship_guidelines_chunk
):
    """
    When a collection has both an admit card and an internship guideline,
    an explanatory question ('what internship is about') must prioritize
    the substantive guideline over the administrative admit card.
    """
    candidates = [admit_card_chunk, internship_guidelines_chunk]
    query = "what internship is about"

    relevant, intent, assessments = filter_relevant_candidates(
        query=query,
        candidates=candidates,
        policy_type=GroundingPolicyType.GENERAL_STATIC,
    )

    assert len(relevant) >= 1
    # The first candidate must be the guideline chunk, not the administrative admit card
    assert relevant[0].chunk_id == "chunk_guideline_002"
    assert "software engineering internship is designed" in relevant[0].text


def test_short_query_semantic_relevance_filtering():
    """
    Confirms 1-token and 2-token queries pass relevance filtering when supported
    by moderate semantic similarity and synonym matching.
    """
    candidate = RetrievalResult(
        chunk_id="chunk_training_003",
        document_id="doc_training",
        text="The software training programme requires practical participation in team code reviews.",
        page_start=1,
        page_end=1,
        rank=1,
        score=0.60,
        metadata={"dense_score": 0.54},
    )

    # 1-token query ('internship' after stop-word removal from 'what internship is about')
    relevant, _, _ = filter_relevant_candidates(
        query="what internship is about",
        candidates=[candidate],
        policy_type=GroundingPolicyType.GENERAL_STATIC,
    )
    assert len(relevant) == 1
    assert relevant[0].chunk_id == "chunk_training_003"


# ---------------------------------------------------------------------------
# 2. GROQ PROVIDER ROBUSTNESS & SAFE ABSTENTION TESTS
# ---------------------------------------------------------------------------

def test_groq_abstention_with_empty_answer_field():
    """
    CRITICAL DEFECT A REGRESSION TEST:
    When Groq returns status='ABSTAINED' with an empty string for 'answer',
    the provider must NOT raise SynthesisMalformedOutputError or cause PROVIDER_FAILED.
    It must return SynthesisStatus.ABSTAINED with a clear controlled abstention narrative.
    """
    raw_response = {
        "id": "chatcmpl-abstain-1",
        "object": "chat.completion",
        "created": 1729000000,
        "model": "llama-3.3-70b-versatile",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps({
                        "status": "ABSTAINED",
                        "answer": "",
                        "claims": [],
                        "unresolved_subquestions": [
                            {
                                "subquestion": "What is the internship about?",
                                "reason": "The provided excerpt is an examination admit card containing roll numbers and test centers, not internship details.",
                            }
                        ],
                    }),
                },
                "finish_reason": "stop",
            }
        ],
    }

    def mock_handler(request: httpx.Request):
        return httpx.Response(200, json=raw_response)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    provider = GroqSynthesisProvider(api_key=DUMMY_API_KEY, http_client=client)

    synthesis_input = SynthesisInput(
        query="what internship is about",
        evidence_items=[
            SynthesisEvidenceItem(
                chunk_id="chunk_admit_card_001",
                document_id="doc_admit_card_2024",
                text="Admit card for Summer Internship Examination 2024. Roll: 88492.",
                page_start=1,
                page_end=1,
                regulatory_status=RegulatoryStatus.ACTIVE_CURRENT.value,
                status_reason="Active document",
            )
        ],
        regulatory_warnings=[],
        policy_type="general_static",
    )

    output = provider.synthesize(synthesis_input)
    assert output.status == SynthesisStatus.ABSTAINED
    assert "Controlled Abstention" in output.answer
    assert "examination admit card" in output.answer
    assert output.failure_reason is None


def test_groq_markdown_code_fence_and_preamble_parsing():
    """
    LLMs frequently output markdown fences (```json ... ```) with conversational preamble.
    The extractor must parse this cleanly without throwing SynthesisMalformedOutputError.
    """
    content = (
        "Here is the structured grounding evaluation based strictly on the excerpts:\n\n"
        "```json\n"
        "{\n"
        '  "status": "FULLY_ANSWERED",\n'
        '  "answer": "The internship is a six-month software engineering programme focused on backend API development.",\n'
        '  "claims": [\n'
        "    {\n"
        '      "claim_text": "The internship is a six-month software engineering programme.",\n'
        '      "supporting_chunk_ids": ["chunk_guideline_002"]\n'
        "    }\n"
        "  ],\n"
        '  "unresolved_subquestions": []\n'
        "}\n"
        "```\n\n"
        "Hope this helps!"
    )

    parsed = _extract_structured_output(content)
    assert parsed["status"] == "FULLY_ANSWERED"
    assert "six-month software engineering" in parsed["answer"]


def test_groq_reasoning_think_tags_stripped():
    """
    Reasoning models emit <think>...</think> blocks. These must be cleanly stripped
    before JSON decoding.
    """
    content = (
        "<think>\n"
        "The user asks what the internship is about.\n"
        "Excerpt chunk_guideline_002 describes it as a six-month software engineering programme.\n"
        "I will synthesize the answer and cite chunk_guideline_002.\n"
        "</think>\n"
        "{\n"
        '  "status": "FULLY_ANSWERED",\n'
        '  "answer": "The internship is designed to give students practical engineering experience.",\n'
        '  "claims": [\n'
        '    {"claim_text": "The internship provides practical engineering experience.", "supporting_chunk_ids": ["chunk_guideline_002"]}\n'
        "  ],\n"
        '  "unresolved_subquestions": []\n'
        "}"
    )

    parsed = _extract_structured_output(content)
    assert parsed["status"] == "FULLY_ANSWERED"
    assert "practical engineering experience" in parsed["answer"]


def test_groq_alternative_key_fallbacks():
    """
    When LLM outputs keys like 'direct_answer' or 'response' instead of 'answer',
    the parser must map them safely.
    """
    content = (
        "{\n"
        '  "status": "FULLY_ANSWERED",\n'
        '  "direct_answer": "Final-year students with a GPA of 7.5 or above can apply.",\n'
        '  "claim_citations": [\n'
        '    {"text": "Final-year students with a GPA of 7.5 or above can apply.", "chunk_ids": ["chunk_guideline_002"]}\n'
        "  ],\n"
        '  "unresolved": []\n'
        "}"
    )

    parsed = _extract_structured_output(content)
    assert "direct_answer" in parsed

    def mock_handler(request: httpx.Request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": content}}]
        })

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    provider = GroqSynthesisProvider(api_key=DUMMY_API_KEY, http_client=client)

    synthesis_input = SynthesisInput(
        query="who can apply",
        evidence_items=[
            SynthesisEvidenceItem(
                chunk_id="chunk_guideline_002",
                document_id="doc_guidelines",
                text="Eligibility criteria: Final-year undergraduate students with a cumulative GPA of 7.5 or above.",
                page_start=1,
                page_end=1,
                regulatory_status=RegulatoryStatus.ACTIVE_CURRENT.value,
                status_reason="Active guidelines",
            )
        ],
        regulatory_warnings=[],
        policy_type="general_static",
    )

    output = provider.synthesize(synthesis_input)
    assert output.status == SynthesisStatus.FULLY_ANSWERED
    assert "GPA of 7.5" in output.answer
    assert len(output.claims) == 1
    assert output.claims[0].supporting_chunk_ids == ["chunk_guideline_002"]


def test_groq_trailing_commas_and_truncated_recovery():
    """Verifies that trailing commas and unclosed JSON objects are repaired."""
    # Trailing comma in JSON object
    trailing_content = '{"status": "ABSTAINED", "answer": "Insufficient evidence.", "claims": [],}'
    parsed = _extract_structured_output(trailing_content)
    assert parsed["status"] == "ABSTAINED"

    # Truncated JSON closing
    truncated_content = '{"status": "ABSTAINED", "answer": "No details found.", "claims": [], "unresolved_subquestions": ['
    parsed_trunc = _extract_structured_output(truncated_content)
    assert parsed_trunc["status"] == "ABSTAINED"
    assert parsed_trunc["answer"] == "No details found."


# ---------------------------------------------------------------------------
# 3. GEMINI PROVIDER ROBUSTNESS & SAFE ABSTENTION TESTS
# ---------------------------------------------------------------------------

def test_gemini_abstention_with_empty_answer_field():
    """
    Confirms Gemini provider also handles status='ABSTAINED' with empty 'answer'
    gracefully without raising SynthesisMalformedOutputError.
    """
    raw_response = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": json.dumps({
                                "status": "ABSTAINED",
                                "answer": "",
                                "claims": [],
                                "unresolved_subquestions": [
                                    {
                                        "subquestion": "Internship scope",
                                        "reason": "The document is an admit card.",
                                    }
                                ],
                            })
                        }
                    ]
                },
                "finishReason": "STOP",
            }
        ]
    }

    def mock_handler(request: httpx.Request):
        return httpx.Response(200, json=raw_response)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    provider = GeminiSynthesisProvider(api_key="mock_gemini_key", http_client=client)

    synthesis_input = SynthesisInput(
        query="what internship is about",
        evidence_items=[
            SynthesisEvidenceItem(
                chunk_id="chunk_admit_card_001",
                document_id="doc_admit_card_2024",
                text="Admit card for Summer Internship Examination 2024.",
                page_start=1,
                page_end=1,
                regulatory_status=RegulatoryStatus.ACTIVE_CURRENT.value,
                status_reason="Active admit card",
            )
        ],
        regulatory_warnings=[],
        policy_type="general_static",
    )

    output = provider.synthesize(synthesis_input)
    assert output.status == SynthesisStatus.ABSTAINED
    assert "Controlled Abstention" in output.answer
    assert "The document is an admit card" in output.answer


# ---------------------------------------------------------------------------
# 4. END-TO-END SYNTHESIS SERVICE INTEGRATION
# ---------------------------------------------------------------------------

def test_e2e_supported_question_synthesis_flow():
    """
    Full pipeline test: User asks 'what is the internship about' with a valid guideline excerpt.
    Result must be FULLY_ANSWERED with citations, verified claims, and no error messages.
    """
    def mock_llm_handler(request: httpx.Request):
        return httpx.Response(200, json={
            "choices": [
                {
                    "message": {
                        "content": json.dumps({
                            "status": "FULLY_ANSWERED",
                            "answer": "The six-month software engineering internship is designed to immerse students in backend API development and distributed data pipelines.",
                            "claims": [
                                {
                                    "claim_text": "The six-month software engineering internship is designed to immerse students in backend API development.",
                                    "supporting_chunk_ids": ["chunk_guideline_002"],
                                }
                            ],
                            "unresolved_subquestions": [],
                        })
                    }
                }
            ]
        })

    client = httpx.Client(transport=httpx.MockTransport(mock_llm_handler))
    groq_provider = GroqSynthesisProvider(api_key=DUMMY_API_KEY, http_client=client)
    service = SynthesisService(provider=groq_provider)

    verified_excerpt = ValidatedExcerptItem(
        excerpt_id="excerpt_1",
        source_chunk_id="chunk_guideline_002",
        document_id="doc_internship_handbook_2024",
        excerpt_text="The six-month software engineering internship is designed to immerse students in backend API development and distributed data pipelines.",
        regulatory_status=RegulatoryStatus.ACTIVE_CURRENT,
        status_reason="Active handbook provision",
        circular_number=None,
        title="Internship Guidelines 2024",
        issue_date="2024-01-15",
        page_start=1,
        page_end=1,
        collection_id="col_internship_test",
    )

    result = service.synthesize_answer(
        query="what is the internship about",
        verified_excerpts=[verified_excerpt],
        disposition=AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS,
        regulatory_warnings=[],
        collection_id="col_internship_test",
        policy_type="general_static",
    )

    assert result.status == SynthesisStatus.FULLY_ANSWERED
    assert "software engineering internship" in result.answer
    assert len(result.claims) == 1
    assert result.claims[0].supporting_chunk_ids == ["chunk_guideline_002"]
    assert result.failure_reason is None


def test_e2e_unsupported_question_synthesis_flow():
    """
    Full pipeline test: User asks 'what internship is about' over an admit card.
    Groq abstains with empty answer string.
    Result must be ABSTAINED (Controlled Abstention), NOT PROVIDER_FAILED or 'invalid output'.
    """
    def mock_abstain_handler(request: httpx.Request):
        return httpx.Response(200, json={
            "choices": [
                {
                    "message": {
                        "content": json.dumps({
                            "status": "ABSTAINED",
                            "answer": "",
                            "claims": [],
                            "unresolved_subquestions": [
                                {
                                    "subquestion": "internship description",
                                    "reason": "The provided excerpt is an examination admit card containing candidate name, roll number, and exam center.",
                                }
                            ],
                        })
                    }
                }
            ]
        })

    client = httpx.Client(transport=httpx.MockTransport(mock_abstain_handler))
    groq_provider = GroqSynthesisProvider(api_key=DUMMY_API_KEY, http_client=client)
    service = SynthesisService(provider=groq_provider)

    admit_excerpt = ValidatedExcerptItem(
        excerpt_id="excerpt_1",
        source_chunk_id="chunk_admit_card_001",
        document_id="doc_admit_card_2024",
        excerpt_text="Candidate Name: Rahul Sharma. Roll Number: CBTE-2024-88492. Examination Centre: Hall B.",
        regulatory_status=RegulatoryStatus.ACTIVE_CURRENT,
        status_reason="Active document",
        circular_number=None,
        title="Admit Card 2024",
        issue_date="2024-06-01",
        page_start=1,
        page_end=1,
        collection_id="col_internship_test",
    )

    result = service.synthesize_answer(
        query="what internship is about",
        verified_excerpts=[admit_excerpt],
        disposition=AnswerDisposition.VERIFIED_HISTORICAL_EXCERPTS,
        regulatory_warnings=[],
        collection_id="col_internship_test",
        policy_type="general_static",
    )

    # Must be ABSTAINED, NOT PROVIDER_FAILED
    assert result.status == SynthesisStatus.ABSTAINED
    assert result.status != SynthesisStatus.PROVIDER_FAILED
    assert "Controlled Abstention" in result.answer
    assert "examination admit card" in result.answer
    assert result.failure_reason is None
