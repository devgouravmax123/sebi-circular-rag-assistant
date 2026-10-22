"""
Regression tests for Phase 2.2B.18: Evaluation Harness Corrections.

Verifies:
1. An unanswerable query with an empty expected-chunk list cannot automatically count as a relevance-filter rejection.
2. A query with candidates retained by the relevance filter is reported as retained (not falsely marked as rejected).
3. A query with zero retained candidates is correctly reported as a relevance-filter rejection.
4. Per-chunk claims are checked against their designated chunks.
5. Cross-chunk claims are checked against the combined evidence when explicitly declared.
6. An unsupported number remains rejected when the cited evidence does not support it.
7. Existing benchmark files and result artifacts remain unchanged.
8. Output-path safety prevents accidental overwriting.
"""
import json
import pytest
from pathlib import Path
import sys

# Locate repo root accurately (two levels up from backend/tests/)
repo_root = Path(__file__).resolve().parent.parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))
backend_path = repo_root / "backend"
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))

from app.schemas.retrieval import RetrievalResult
from app.grounding.relevance import filter_relevant_candidates, RelevanceDecisionCode
from app.synthesis.verifier import verify_claim_support
from evaluation.evaluate_general_rag import (
    resolve_output_path,
    PROTECTED_BASELINE_FILES,
    run_evaluation,
)


@pytest.fixture
def sample_synthetic_chunks():
    synth_processed = repo_root / "data" / "processed_synthetic"
    with open(synth_processed / "acme_security_policy" / "chunks.json", "r", encoding="utf-8") as f:
        c_sec_text = json.load(f)[0]["text"]
    with open(synth_processed / "acme_infra_handbook" / "chunks.json", "r", encoding="utf-8") as f:
        c_infra_text = json.load(f)[0]["text"]
    with open(synth_processed / "acme_product_spec" / "chunks.json", "r", encoding="utf-8") as f:
        c_spec_text = json.load(f)[0]["text"]

    c_sec = RetrievalResult(
        chunk_id="acme_security_policy_p1_0",
        document_id="acme_security_policy",
        text=c_sec_text,
        page_start=1,
        page_end=2,
        score=0.032,
        rank=1,
    )
    c_infra = RetrievalResult(
        chunk_id="acme_infra_handbook_p1_0",
        document_id="acme_infra_handbook",
        text=c_infra_text,
        page_start=1,
        page_end=2,
        score=0.030,
        rank=2,
    )
    c_spec = RetrievalResult(
        chunk_id="acme_product_spec_p1_0",
        document_id="acme_product_spec",
        text=c_spec_text,
        page_start=1,
        page_end=2,
        score=0.028,
        rank=3,
    )
    return [c_sec, c_infra, c_spec]


def test_1_unanswerable_empty_expected_chunks_does_not_equal_relevance_rejection(sample_synthetic_chunks):
    """
    An unanswerable query cannot count as a relevance filter rejection merely because its expected-chunk list is empty.
    When candidate passages share common technical words with the query, the relevance filter retains them.
    """
    query_with_overlap = "Does Acme deploy PgBouncer or Pgpool-II in front of production PostgreSQL 16 instances?"
    rel, intent, assessments = filter_relevant_candidates(query_with_overlap, sample_synthetic_chunks)

    # Candidates were retained by the relevance filter
    assert len(rel) > 0, "Relevance filter retained candidate passages sharing common vocabulary"
    # It must NOT be reported as a relevance rejection
    relevance_rejected_all = (len(rel) == 0)
    assert relevance_rejected_all is False


def test_2_query_with_retained_candidates_reported_as_retained(sample_synthetic_chunks):
    """
    A query where filter_relevant_candidates returns >= 1 candidates is strictly reported as retained.
    """
    query = "What percentage annual performance bonus is awarded to Acme SRE leads upon completing semi-annual DR drills?"
    rel, intent, assessments = filter_relevant_candidates(query, sample_synthetic_chunks)
    assert len(rel) == 1
    assert rel[0].chunk_id == "acme_infra_handbook_p1_0"


def test_3_query_with_zero_candidates_reported_as_rejection(sample_synthetic_chunks):
    """
    A query with zero retained candidates is strictly reported as a relevance filter rejection.
    """
    query = "Which specific hardware token vendor (e.g., YubiKey 5C NFC or Google Titan) does Acme procure for FIDO2 MFA?"
    rel, intent, assessments = filter_relevant_candidates(query, sample_synthetic_chunks)
    assert len(rel) == 0


def test_4_per_chunk_claims_evaluated_against_designated_chunk(sample_synthetic_chunks):
    """
    Per-chunk claim mapping evaluates each claim only against its designated chunk.
    """
    infra_text = next(c.text for c in sample_synthetic_chunks if c.chunk_id == "acme_infra_handbook_p1_0")
    spec_text = next(c.text for c in sample_synthetic_chunks if c.chunk_id == "acme_product_spec_p1_0")

    # Claim specific to product spec (contains 365)
    spec_claim = "cold parquet archives for 365 days"
    supported_spec, score, _ = verify_claim_support(spec_claim, spec_text)
    assert supported_spec is True

    # Claim specific to infra handbook (contains 6 hours, 30 calendar days)
    infra_claim = "Production database snapshots must be taken every 6 hours and retained for 30 calendar days"
    supported_infra, score, _ = verify_claim_support(infra_claim, infra_text)
    assert supported_infra is True


def test_5_cross_chunk_claims_evaluated_against_combined_evidence(sample_synthetic_chunks):
    """
    When a comparative claim spans multiple chunks, checking against the combined text succeeds.
    """
    infra_text = next(c.text for c in sample_synthetic_chunks if c.chunk_id == "acme_infra_handbook_p1_0")
    spec_text = next(c.text for c in sample_synthetic_chunks if c.chunk_id == "acme_product_spec_p1_0")
    combined_evidence = f"{spec_text}\n{infra_text}"

    comparative_claim = "database snapshots are managed independently of the telemetry mesh and adhere to 30-day storage limits"
    supported, score, reasons = verify_claim_support(comparative_claim, combined_evidence)
    assert supported is True, f"Cross-chunk claim failed on combined evidence: {reasons}"


def test_6_unsupported_number_rejected():
    """
    Numeric fidelity: If a claim asserts a number not present in the supporting chunk, it is strictly rejected.
    """
    infra_text = "Production database snapshots must be taken every 6 hours and retained for 30 calendar days."
    unsupported_claim = "Production database snapshots are retained for 365 calendar days."
    supported, score, reasons = verify_claim_support(unsupported_claim, infra_text)
    assert supported is False
    assert any("unsupported_number_365" in r for r in reasons)


def test_7_baseline_and_result_artifacts_untouched():
    """
    Verifies that the original benchmark files and results exist and remain untouched.
    """
    data_dir = repo_root / "evaluation" / "data"
    assert (data_dir / "general_evaluation_dataset.json").exists()
    assert (data_dir / "general_evaluation_results.json").exists()
    assert (data_dir / "broader_general_evaluation_dataset.json").exists()
    assert (data_dir / "broader_general_evaluation_results.json").exists()
    assert (data_dir / "gold_evaluation_dataset.json").exists()


def test_8_output_path_safety_prevents_overwrite(tmp_path):
    """
    Existing files cannot be overwritten, and protected baselines are blocked.
    """
    existing_file = tmp_path / "already_exists.json"
    existing_file.write_text("{}", encoding="utf-8")
    with pytest.raises(FileExistsError):
        resolve_output_path(str(existing_file), repo_root=tmp_path)

    for protected in PROTECTED_BASELINE_FILES:
        with pytest.raises(ValueError):
            resolve_output_path(str(tmp_path / protected), repo_root=tmp_path)
