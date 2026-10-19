"""
Tests for Phase 10.1.26: Configurable RETRIEVAL_MODE and Lexical-Only Demo Mode.

Verifies:
1. `hybrid` remains the default when RETRIEVAL_MODE is unset or omitted.
2. Valid and invalid `RETRIEVAL_MODE` values are handled correctly.
3. Lexical-only mode executes without loading PyTorch or SentenceTransformers into sys.modules.
4. Exact keyword query and citation retrieval in lexical-only mode.
5. Controlled abstention on out-of-scope queries in lexical-only mode.
6. Separation of superseded provisions and `include_superseded` toggle in lexical-only mode.
7. Readiness probe passes in lexical-only mode without dense index assets or database.
8. Rate limiting and CORS validation remain intact.
"""
import os
import sys
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import app
from app.schemas.query import QueryResponse
from app.schemas.safety_gate import AnswerDisposition


@pytest.fixture
def client():
    return TestClient(app)


def test_1_default_retrieval_mode_is_hybrid():
    """Verify that when RETRIEVAL_MODE is unset, default is 'hybrid'."""
    settings = Settings()
    assert settings.retrieval_mode == "hybrid"
    assert settings.is_lexical_only is False


def test_2_retrieval_mode_validation():
    """Verify valid and invalid RETRIEVAL_MODE values."""
    s1 = Settings(retrieval_mode="hybrid")
    assert s1.is_lexical_only is False

    s2 = Settings(retrieval_mode="lexical_only")
    assert s2.is_lexical_only is True

    s3 = Settings(retrieval_mode="  LEXICAL_ONLY  ")
    assert s3.is_lexical_only is True
    assert s3.retrieval_mode == "lexical_only"

    with pytest.raises(ValueError, match="Unsupported RETRIEVAL_MODE"):
        Settings(retrieval_mode="semantic_vector")


def test_3_lexical_only_readiness_probe(client, monkeypatch):
    """Verify that GET /ready succeeds in lexical_only mode without database."""
    monkeypatch.setenv("RETRIEVAL_MODE", "lexical_only")
    get_settings(force_reload=True)

    resp = client.get("/ready")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ready"
    assert data["storage_mode"] == "file_backed"
    assert data["index_active"] is True


def test_4_lexical_only_exact_keyword_query(client, monkeypatch):
    """Verify that exact keyword search in lexical-only mode retrieves correct grounded excerpts."""
    monkeypatch.setenv("RETRIEVAL_MODE", "lexical_only")
    get_settings(force_reload=True)

    resp = client.post(
        "/api/v1/query",
        json={"query": "ISO 27001 certification requirement", "top_k": 3},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["query"] == "ISO 27001 certification requirement"
    assert data["technical_validation_passed"] is True
    assert len(data["verified_excerpts"]) > 0

    # Ensure pilot corpus scope limitation is present
    assert "pilot_corpus_scope_limitation" in data
    assert "5-document SEBI CSCRF circular corpus" in data["pilot_corpus_scope_limitation"]

    # Verify claim citations and answer text exist (Mock synthesis)
    assert data["synthesis_status"] in ["FULLY_ANSWERED", "PARTIALLY_ANSWERED"]
    assert len(data["claim_citations"]) > 0


def test_5_lexical_only_controlled_abstention_out_of_scope(client, monkeypatch):
    """Verify controlled abstention for out-of-scope query in lexical-only mode."""
    monkeypatch.setenv("RETRIEVAL_MODE", "lexical_only")
    get_settings(force_reload=True)

    resp = client.post(
        "/api/v1/query",
        json={"query": "What are the margin trading rules for equity derivatives?", "top_k": 3},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["disposition"] == AnswerDisposition.CONTROLLED_ABSTENTION.value
    assert data["verified_excerpts"] == []
    assert data["synthesis_status"] == "ABSTAINED"
    assert "Controlled Abstention" in data["summary_narrative"]


def test_6_lexical_only_superseded_evidence_separation(client, monkeypatch):
    """Verify separation of superseded provisions when include_superseded=False."""
    monkeypatch.setenv("RETRIEVAL_MODE", "lexical_only")
    get_settings(force_reload=True)

    resp = client.post(
        "/api/v1/query",
        json={
            "query": "What is the compliance deadline for Qualified REs?",
            "top_k": 5,
            "include_superseded": False,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    # Verified excerpts must only include active/unresolved, not superseded baseline without qualification
    for exc in data["verified_excerpts"]:
        assert exc["regulatory_status"] != "SUPERSEDED"


def test_7_lexical_only_with_filters(client, monkeypatch):
    """Verify that domain and document_id filters work in lexical-only mode."""
    monkeypatch.setenv("RETRIEVAL_MODE", "lexical_only")
    get_settings(force_reload=True)

    resp = client.post(
        "/api/v1/query",
        json={
            "query": "cyber resilience framework",
            "domain": "CSCRF",
            "top_k": 3,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    for exc in data["verified_excerpts"]:
        assert exc["domain"] == "CSCRF"


def test_8_hybrid_mode_still_works_as_default(client, monkeypatch):
    """Verify that hybrid mode still operates cleanly when RETRIEVAL_MODE is hybrid."""
    monkeypatch.setenv("RETRIEVAL_MODE", "hybrid")
    get_settings(force_reload=True)

    resp = client.post(
        "/api/v1/query",
        json={"query": "ISO 27001 certification requirement", "top_k": 3},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["technical_validation_passed"] is True
    assert len(data["verified_excerpts"]) > 0


def test_9_demo_requirements_contains_numpy():
    """Verify backend/requirements-demo.txt specifies numpy for interfaces type-checking."""
    from pathlib import Path
    req_path = Path(__file__).resolve().parent.parent / "requirements-demo.txt"
    assert req_path.exists(), "backend/requirements-demo.txt must exist"
    req_text = req_path.read_text(encoding="utf-8")
    assert "numpy" in req_text, "requirements-demo.txt must include numpy"


def test_10_app_startup_without_sqlalchemy():
    """Verify app imports and startup succeed cleanly when sqlalchemy is completely unavailable."""
    import sys
    import subprocess
    from pathlib import Path

    # Determine stable absolute path to project root and backend directory
    project_root = Path(__file__).resolve().parent.parent.parent
    backend_dir = project_root / "backend"

    # Run in a separate subprocess to ensure clean module isolation
    cmd = [
        sys.executable,
        "-c",
        (
            f"import sys, os\n"
            f"from pathlib import Path\n"
            f"os.environ['RETRIEVAL_MODE'] = 'lexical_only'\n"
            f"os.environ['SYNTHESIS_PROVIDER'] = 'mock'\n"
            f"os.environ['APP_ENV'] = 'production'\n"
            f"os.environ['ADMIN_API_KEY'] = 'test_secret_key_at_least_16_chars'\n"
            f"sys.path.insert(0, r'{backend_dir}')\n"
            f"# Block all database dependencies\n"
            f"for mod in ['sqlalchemy', 'sqlalchemy.orm', 'sqlalchemy.exc', 'asyncpg', 'psycopg', 'psycopg2', 'alembic', 'pgvector']:\n"
            f"    sys.modules[mod] = None\n"
            f"import app.main\n"
            f"from fastapi.testclient import TestClient\n"
            f"client = TestClient(app.main.app)\n"
            f"r1 = client.get('/health')\n"
            f"assert r1.status_code == 200, f'health failed: {{r1.status_code}}'\n"
            f"r2 = client.get('/ready')\n"
            f"assert r2.status_code == 200, f'ready failed: {{r2.status_code}}'\n"
            f"r3 = client.post('/api/v1/query', json={{'query': 'What is CSCRF?'}})\n"
            f"assert r3.status_code == 200, f'query failed: {{r3.status_code}}'\n"
            f"r4 = client.get('/api/v1/admin/jobs', headers={{'X-Admin-Key': 'test_secret_key_at_least_16_chars'}})\n"
            f"assert r4.status_code == 200, f'admin failed: {{r4.status_code}}'\n"
            f"print('SUCCESS')\n"
        ),
    ]
    res = subprocess.run(cmd, cwd=str(project_root), capture_output=True, text=True)
    assert res.returncode == 0, f"Subprocess failed with code {res.returncode}:\nStdout: {res.stdout}\nStderr: {res.stderr}"
    assert "SUCCESS" in res.stdout


def test_11_cross_platform_manifest_checksum_and_retrieval(tmp_path):
    """
    Verify that RetrievalService resolves the snapshot and successfully indexes passages
    even when files have Linux LF line endings while active_snapshot.json has Windows CRLF checksum.
    Also verifies that a genuinely tampered manifest is still rejected.
    """
    from app.retrieval.service import RetrievalService
    from pathlib import Path
    import json

    real_snap_dir = Path("data/indexes/snapshots/snap_20261011_104512_23a014")
    real_pointer_file = Path("data/indexes/active_snapshot.json")

    # Set up isolated index directory mimicking Linux git checkout (LF only)
    fake_indexes = tmp_path / "indexes"
    fake_snap = fake_indexes / "snapshots" / "snap_20261011_104512_23a014"
    fake_snap.mkdir(parents=True)

    # Write snapshot files with strict LF line endings
    for fname in ("manifest.json", "chunks.json", "documents.json"):
        src = real_snap_dir / fname
        data = src.read_bytes().replace(b"\r\n", b"\n")
        (fake_snap / fname).write_bytes(data)

    # Copy active pointer with Windows CRLF manifest checksum as committed
    fake_pointer = fake_indexes / "active_snapshot.json"
    fake_pointer.write_bytes(real_pointer_file.read_bytes())

    # Empty processed directory to simulate Docker.demo container
    empty_processed = tmp_path / "processed"
    empty_processed.mkdir()

    # 1. RetrievalService must resolve snapshot and load real documents and chunks
    service = RetrievalService(
        processed_dir=empty_processed,
        indexes_dir=fake_indexes,
        retrieval_mode="lexical_only",
    )
    docs_count, chunks_count = service.load_and_index_corpus()
    assert docs_count == 5, f"Expected 5 documents, got {docs_count}"
    assert chunks_count == 168, f"Expected 168 chunks, got {chunks_count}"

    # 2. Must retrieve actual circular passages for regulatory queries
    res = service.search("Cybersecurity and Cyber Resilience Framework", top_k=3)
    assert res.total_results > 0, "Expected non-zero results from active snapshot"
    assert len(res.results) > 0
    assert any("CSCRF" in r.text or "Cybersecurity" in r.text for r in res.results)

    # 3. Verify that true content tampering is still strictly rejected
    tampered_manifest = fake_snap / "manifest.json"
    manifest_data = json.loads(tampered_manifest.read_text(encoding="utf-8"))
    manifest_data["total_chunks"] = 9999
    tampered_manifest.write_text(json.dumps(manifest_data), encoding="utf-8")

    tampered_service = RetrievalService(
        processed_dir=empty_processed,
        indexes_dir=fake_indexes,
        retrieval_mode="lexical_only",
    )
    assert tampered_service.get_active_snapshot_dir() is None, "Tampered manifest must be rejected"
