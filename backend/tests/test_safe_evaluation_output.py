"""
Tests for Safe Evaluation Output Handling (Phase 2.2B.15).

Verifies:
1. A new output path is accepted.
2. An existing output path is rejected without changing its contents.
3. The default path is unique and does not target 'general_evaluation_results.json'.
4. Protected baseline paths are rejected.
5. Metric calculations and summary schema remain unchanged.
"""
import json
import pytest
import tempfile
from pathlib import Path
import sys

repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))
backend_path = repo_root / "backend"
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))

from evaluation.evaluate_general_rag import (
    resolve_output_path,
    PROTECTED_BASELINE_FILES,
    run_evaluation,
)


def test_1_new_output_path_accepted(tmp_path):
    """A valid, non-existent output path is accepted and resolved."""
    custom_out = tmp_path / "custom_eval_run.json"
    resolved = resolve_output_path(str(custom_out), repo_root=tmp_path)
    assert resolved == custom_out.resolve()
    assert not resolved.exists()


def test_2_existing_output_path_rejected(tmp_path):
    """An already existing output path must raise FileExistsError without altering content."""
    existing_out = tmp_path / "already_exists.json"
    sentinel_content = '{"status": "original_content"}'
    existing_out.write_text(sentinel_content, encoding="utf-8")

    with pytest.raises(FileExistsError) as exc_info:
        resolve_output_path(str(existing_out), repo_root=tmp_path)

    assert "already exists and will not be overwritten" in str(exc_info.value)
    # Ensure content was completely untouched
    assert existing_out.read_text(encoding="utf-8") == sentinel_content


def test_3_default_path_is_unique_and_avoids_static_file(tmp_path):
    """When output_path is None, a unique timestamped file is selected, avoiding the baseline name."""
    eval_data_dir = tmp_path / "evaluation" / "data"
    eval_data_dir.mkdir(parents=True)

    # Place the static results file in the temp directory to verify it is NOT targeted
    static_file = eval_data_dir / "general_evaluation_results.json"
    static_file.write_text('{"baseline": true}', encoding="utf-8")

    resolved_default_1 = resolve_output_path(None, repo_root=tmp_path)
    assert resolved_default_1.name != "general_evaluation_results.json"
    assert resolved_default_1.name.startswith("diagnostic_general_eval_")
    assert resolved_default_1.suffix == ".json"
    assert not resolved_default_1.exists()

    # If the candidate exists, it disambiguates with a counter
    resolved_default_1.write_text("{}", encoding="utf-8")
    resolved_default_2 = resolve_output_path(None, repo_root=tmp_path)
    assert resolved_default_2 != resolved_default_1
    assert not resolved_default_2.exists()


def test_4_protected_baseline_paths_rejected(tmp_path):
    """All protected baseline filenames must be rejected even if they don't yet exist in a directory."""
    for protected in PROTECTED_BASELINE_FILES:
        target = tmp_path / protected
        with pytest.raises(ValueError) as exc_info:
            resolve_output_path(str(target), repo_root=tmp_path)
        assert "protected baseline evaluation file" in str(exc_info.value)


def test_5_metric_calculations_and_schema_preserved(tmp_path):
    """
    Run evaluation targeting a safe isolated temp file to verify that metric calculations,
    summary structure, and schema remain 100% faithful and intact.
    """
    custom_target = tmp_path / "isolated_eval_test.json"
    assert not custom_target.exists()

    run_evaluation(output_path=str(custom_target))

    assert custom_target.exists()
    with open(custom_target, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Verify top-level schema keys
    expected_top_keys = {
        "dataset_version",
        "collection_id",
        "total_queries",
        "answerable_queries",
        "unanswerable_queries",
        "collection_isolation",
        "citation_validation",
        "lexical_support_heuristic",
        "per_query_results",
    }
    assert expected_top_keys.issubset(set(data.keys()))

    # Verify key metric calculations
    assert data["total_queries"] == 22
    assert data["answerable_queries"]["recall_rate_pct"] == "100.00% (17/17)"
    assert data["answerable_queries"]["evidence_presence_rate_pct"] == "100.00% (17/17)"
    assert data["collection_isolation"]["violations"] == 0
    assert data["collection_isolation"]["status"] == "ISOLATED"
    assert data["citation_validation"]["valid_rate_pct"] == "100.00% (18/18)"
    assert data["unanswerable_queries"]["safe_abstention_rate_pct"] == "100.00% (5/5)"
    assert len(data["per_query_results"]) == 22
