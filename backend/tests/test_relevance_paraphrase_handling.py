"""
Phase 2.2B.13: Regression and validation tests for Safe General RAG Relevance Matching.

Verifies:
A. Original conditional query:
   "How long do contractor credentials last, and what is required to extend them?"
   The security-policy evidence survives the relevance filter.
B. Expiry paraphrase:
   "When do contractor credentials expire?" survives.
C. Extension paraphrase:
   "What approval is required to extend contractor credentials?" survives.
D. Inflection handling:
   Appropriate forms of extend <-> extension and expire <-> expiry match,
   while unrelated words sharing short prefixes (external, extent, extensive, expert) do not match.
E. Unanswerable question:
   "What are the salary bands for Acme employees?" is rejected cleanly (fails closed).
F. General-purpose substantive terms:
   Words like "long" and "last" remain substantive query tokens and are not indiscriminately stripped.
G. SEBI regression compatibility:
   Relevance filter decisions on regulatory queries remain deterministic and safe.
"""
import pytest
from app.grounding.relevance import (
    extract_substantive_query_tokens,
    extract_specific_query_tokens,
    check_token_match,
    filter_relevant_candidates,
    normalize_inflection,
    STOP_WORDS,
    RelevanceDecisionCode,
)
from app.schemas.retrieval import RetrievalResult
from app.grounding.classifier import classify_query_intent, QueryIntent


@pytest.fixture
def contractor_credential_chunk():
    return RetrievalResult(
        chunk_id="acme_security_policy_p1_0",
        document_id="acme_security_policy",
        text=(
            "Third-party contractor credentials expire automatically after 30 days "
            "unless an extension is approved in writing by the Chief Information Security Officer (CISO)."
        ),
        page_start=1,
        page_end=2,
        score=0.032,
        rank=1,
    )


def test_a_original_conditional_query_survives_filter(contractor_credential_chunk):
    """
    Test A: The original live smoke-test query survives candidate relevance filtering.
    "How long do contractor credentials last, and what is required to extend them?"
    """
    query = "How long do contractor credentials last, and what is required to extend them?"
    sub_tokens = extract_substantive_query_tokens(query)
    spec_tokens = extract_specific_query_tokens(query)

    # Substantive tokens should be ['long', 'contractor', 'credentials', 'last', 'extend']
    assert "them" not in sub_tokens, "'them' should be treated as functional stop word"
    assert "long" in sub_tokens, "'long' must remain substantive"
    assert "last" in sub_tokens, "'last' must remain substantive"
    assert "extend" in sub_tokens
    assert len(sub_tokens) == 5

    # Check candidate relevance evaluation
    relevant_candidates, intent, assessments = filter_relevant_candidates(
        query, [contractor_credential_chunk]
    )

    assert len(relevant_candidates) == 1
    assert relevant_candidates[0].chunk_id == "acme_security_policy_p1_0"
    assert assessments[0].is_relevant is True
    assert assessments[0].decision_code == RelevanceDecisionCode.RELEVANT_EVIDENCE

    # Exact score verification: contractor, credentials, extend (via extension) = 3 / 5 = 60.0%
    matched = [t for t in sub_tokens if check_token_match(t, contractor_credential_chunk.text.lower())]
    assert set(matched) == {"contractor", "credentials", "extend"}
    coverage = len(matched) / len(sub_tokens)
    assert coverage == 0.60
    assert coverage >= 0.40


def test_b_expiry_paraphrase_survives(contractor_credential_chunk):
    """
    Test B: Paraphrased query focusing on expiry survives relevance filtering.
    "When do contractor credentials expire?"
    """
    query = "When do contractor credentials expire?"
    sub_tokens = extract_substantive_query_tokens(query)
    # Substantive: ['contractor', 'credentials', 'expire']
    assert set(sub_tokens) == {"contractor", "credentials", "expire"}

    relevant_candidates, intent, assessments = filter_relevant_candidates(
        query, [contractor_credential_chunk]
    )

    assert len(relevant_candidates) == 1
    assert assessments[0].is_relevant is True
    assert assessments[0].decision_code == RelevanceDecisionCode.RELEVANT_EVIDENCE


def test_c_extension_paraphrase_survives(contractor_credential_chunk):
    """
    Test C: Paraphrased query focusing on approval and extension survives.
    "What approval is required to extend contractor credentials?"
    """
    query = "What approval is required to extend contractor credentials?"
    sub_tokens = extract_substantive_query_tokens(query)
    assert "extend" in sub_tokens
    assert "contractor" in sub_tokens
    assert "credentials" in sub_tokens

    relevant_candidates, intent, assessments = filter_relevant_candidates(
        query, [contractor_credential_chunk]
    )

    assert len(relevant_candidates) == 1
    assert assessments[0].is_relevant is True
    assert assessments[0].decision_code == RelevanceDecisionCode.RELEVANT_EVIDENCE


def test_d_inflection_handling_precision():
    """
    Test D: Bounded inflection normalization precision.
    Legitimate grammatical variants match, but unrelated words sharing short prefixes DO NOT.
    """
    # 1. 'extend' family maps to root 'extend'
    for form in ["extend", "extends", "extended", "extending", "extension", "extensions"]:
        assert normalize_inflection(form) == "extend", f"Failed for {form}"

    # 2. 'expire' family maps to root 'expire'
    for form in ["expire", "expires", "expired", "expiring", "expiry", "expiration", "expirations"]:
        assert normalize_inflection(form) == "expire", f"Failed for {form}"

    # 3. Text matching bidirectional tests
    assert check_token_match("extend", "request an extension of privileges") is True
    assert check_token_match("extension", "administrators may extend deadlines") is True
    assert check_token_match("expire", "check the expiry timestamp") is True
    assert check_token_match("expiry", "session keys expire within one hour") is True

    # 4. Prefix false-positive guards: unrelated words MUST NOT normalize or match
    unrelated_prefix_words = [
        "external", "extent", "extensive", "extinguish", "expert", "experiment",
        "explicit", "explore", "expansion", "export", "express"
    ]
    for w in unrelated_prefix_words:
        assert normalize_inflection(w) == w, f"Unrelated word {w} was over-normalized"
        assert check_token_match("extend", f"the {w} audit was performed") is False
        assert check_token_match(w, "they will extend the system") is False


def test_e_unanswerable_salary_bands_fails_closed(contractor_credential_chunk):
    """
    Test E: Unanswerable query is rejected with INSUFFICIENT_RELEVANCE.
    "What are the salary bands for Acme employees?"
    """
    query = "What are the salary bands for Acme employees?"
    sub_tokens = extract_substantive_query_tokens(query)
    assert "salary" in sub_tokens
    assert "bands" in sub_tokens
    assert "employees" in sub_tokens

    relevant_candidates, intent, assessments = filter_relevant_candidates(
        query, [contractor_credential_chunk]
    )

    assert len(relevant_candidates) == 0
    assert assessments[0].is_relevant is False
    assert assessments[0].decision_code == RelevanceDecisionCode.INSUFFICIENT_RELEVANCE


def test_f_general_purpose_substantive_terms_preserved():
    """
    Test F: Verify that 'long' and 'last' are NOT added to STOP_WORDS and remain
    substantive query tokens for general-purpose questions.
    """
    assert "long" not in STOP_WORDS
    assert "last" not in STOP_WORDS

    q_long = "What is the policy for long term archival storage?"
    tokens_long = extract_substantive_query_tokens(q_long)
    assert "long" in tokens_long

    q_last = "When was the last certified disaster recovery drill conducted?"
    tokens_last = extract_substantive_query_tokens(q_last)
    assert "last" in tokens_last


def test_g_sebi_regression_queries_unaffected():
    """
    Test G: Standard SEBI regulatory queries remain properly categorized and assessed.
    """
    # 1. Timeline extension query in SEBI CIR/2025/096
    sebi_chunk = RetrievalResult(
        chunk_id="sebi_cscrf_2025_096_p1_0",
        document_id="sebi_cscrf_2025_096",
        text="The compliance timelines for phase 1 implementation are extended till August 31, 2025.",
        page_start=1,
        page_end=1,
        score=0.035,
        rank=1,
    )
    q_sebi = "What compliance deadline extension was announced in circular 2025/096?"
    rel, intent, assessments = filter_relevant_candidates(q_sebi, [sebi_chunk])
    assert len(rel) == 1
    assert assessments[0].is_relevant is True
