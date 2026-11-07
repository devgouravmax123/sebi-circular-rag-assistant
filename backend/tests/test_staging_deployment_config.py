"""
Task 8.7: Comprehensive Automated Tests for Staging Deployment Configuration.

Validates:
1. Render Blueprint (render.yaml): services, root directories, build/start commands, health checks, no hardcoded secrets.
2. Vercel Configuration (vercel.json, frontend/vercel.json): framework, commands, output dir, SPA rewrites.
3. Backend API Role & URL Segregation: production guards against privileged owner, unpooled Neon endpoints, short secrets.
4. Worker Role & URL Segregation: fail-closed behavior when WORKER_DATABASE_URL is missing or misconfigured.
5. API Health & Readiness Probes: /health and /ready endpoints behave as expected without exposing secrets.
6. Frontend Asset Verification: production build contains no server-side secrets or credentials.
"""
import json
import os
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import Settings


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(autouse=True)
def clean_settings_environment():
    import app.main
    from app.core.config import get_settings
    orig_readiness = getattr(app.main, "_readiness_engine", None)
    yield
    if hasattr(app.main, "_readiness_engine"):
        app.main._readiness_engine = orig_readiness
    get_settings(force_reload=True)


# ---------------------------------------------------------------------------
# 1. Render Blueprint Configuration
# ---------------------------------------------------------------------------

def test_render_blueprint_validity_and_spec():
    render_yaml_path = REPO_ROOT / "render.yaml"
    assert render_yaml_path.exists(), "render.yaml must exist at repository root"

    content = render_yaml_path.read_text(encoding="utf-8")
    assert "services:" in content

    # Check for hardcoded secret values
    forbidden_tokens = ["postgres://", "postgresql://", "gsk_", "AIzaSy", "password", "secret123"]
    for token in forbidden_tokens:
        assert token not in content.lower(), f"Potential hardcoded secret or connection string found in render.yaml: {token}"

    # Basic structural check
    assert "name: sebi-rag-api-staging" in content
    assert "name: sebi-rag-worker-staging" in content
    assert "rootDir: backend" in content
    assert "startCommand: uvicorn app.main:app --host 0.0.0.0 --port $PORT" in content
    assert "startCommand: python -m app.ingestion.worker" in content
    assert "healthCheckPath: /health" in content
    assert "pip install -r requirements.txt" in content
    assert "sync: false" in content


# ---------------------------------------------------------------------------
# 2. Vercel Configuration
# ---------------------------------------------------------------------------

def test_vercel_json_root_and_frontend_specs():
    root_vercel = REPO_ROOT / "vercel.json"
    frontend_vercel = REPO_ROOT / "frontend" / "vercel.json"

    assert root_vercel.exists(), "vercel.json must exist at repository root"
    assert frontend_vercel.exists(), "frontend/vercel.json must exist"

    root_config = json.loads(root_vercel.read_text(encoding="utf-8"))
    assert root_config.get("framework") == "vite"
    assert root_config.get("rootDirectory") == "frontend"
    assert root_config.get("buildCommand") == "npm run build"
    assert root_config.get("outputDirectory") == "dist"
    assert len(root_config.get("rewrites", [])) > 0
    assert root_config["rewrites"][0]["destination"] == "/index.html"

    frontend_config = json.loads(frontend_vercel.read_text(encoding="utf-8"))
    assert frontend_config.get("framework") == "vite"
    assert frontend_config.get("buildCommand") == "npm run build"
    assert frontend_config.get("outputDirectory") == "dist"
    assert len(frontend_config.get("rewrites", [])) > 0
    assert frontend_config["rewrites"][0]["destination"] == "/index.html"


# ---------------------------------------------------------------------------
# 3. Backend API Production Guards & Role Segregation
# ---------------------------------------------------------------------------

def test_api_production_rejects_owner_role():
    with pytest.raises(ValueError, match="DATABASE_URL must not connect as migration owner 'neondb_owner'"):
        Settings(
            environment="production",
            database_url="postgresql://neondb_owner:secret@ep-test-pooler.ap-southeast-1.aws.neon.tech/neondb",
            admin_api_key="1234567890123456",
            auth_secret_key="12345678901234567890123456789012",
        )


def test_api_production_rejects_unpooled_neon_url():
    with pytest.raises(ValueError, match="Production Neon DATABASE_URL must connect via connection pooler"):
        Settings(
            environment="production",
            database_url="postgresql://sebi_api:secret@ep-test.ap-southeast-1.aws.neon.tech:5432/neondb",
            admin_api_key="1234567890123456",
            auth_secret_key="12345678901234567890123456789012",
        )


def test_api_production_accepts_pooled_sebi_api_url():
    settings = Settings(
        environment="production",
        database_url="postgresql://sebi_api:secret@ep-test-pooler.ap-southeast-1.aws.neon.tech/neondb",
        admin_api_key="1234567890123456",
        auth_secret_key="12345678901234567890123456789012",
        frontend_origin="https://sebi-rag-staging.vercel.app",
    )
    assert settings.sanitized_database_url is not None
    assert "secret" not in settings.sanitized_database_url
    assert "sebi_api" in settings.sanitized_database_url


def test_api_production_rejects_short_secrets():
    with pytest.raises(ValueError, match="ADMIN_API_KEY must be set and contain at least 16 characters"):
        Settings(
            environment="production",
            admin_api_key="short",
            auth_secret_key="12345678901234567890123456789012",
        )

    with pytest.raises(ValueError, match="AUTH_SECRET_KEY must be at least 32 characters in production"):
        s = Settings(
            environment="production",
            admin_api_key="1234567890123456",
            auth_secret_key="short_key",
        )
        _ = s.get_auth_secret_key


def test_cors_production_rejects_wildcard():
    settings = Settings(
        environment="production",
        frontend_origin="*",
        admin_api_key="1234567890123456",
        auth_secret_key="12345678901234567890123456789012",
    )
    with pytest.raises(ValueError, match="Wildcard CORS origin '\\*' is strictly prohibited in production"):
        _ = settings.allowed_cors_origins


def test_cors_accepts_multiple_staging_origins():
    settings = Settings(
        environment="production",
        frontend_origin="https://staging.vercel.app, https://preview.vercel.app",
        admin_api_key="1234567890123456",
        auth_secret_key="12345678901234567890123456789012",
    )
    origins = settings.allowed_cors_origins
    assert len(origins) == 2
    assert "https://staging.vercel.app" in origins
    assert "https://preview.vercel.app" in origins


# ---------------------------------------------------------------------------
# 4. Worker Production Guards & Role Segregation
# ---------------------------------------------------------------------------

def test_worker_production_rejects_owner_and_api_roles():
    with pytest.raises(ValueError, match="WORKER_DATABASE_URL cannot connect as 'neondb_owner'"):
        Settings(
            environment="production",
            worker_database_url="postgresql://neondb_owner:secret@ep-test.ap-southeast-1.aws.neon.tech:5432/neondb",
            admin_api_key="1234567890123456",
            auth_secret_key="12345678901234567890123456789012",
        )

    with pytest.raises(ValueError, match="WORKER_DATABASE_URL cannot connect as 'sebi_api'"):
        Settings(
            environment="production",
            worker_database_url="postgresql://sebi_api:secret@ep-test.ap-southeast-1.aws.neon.tech:5432/neondb",
            admin_api_key="1234567890123456",
            auth_secret_key="12345678901234567890123456789012",
        )


def test_worker_fails_closed_when_database_enabled_without_worker_url(monkeypatch):
    from app.ingestion.worker import get_repository_factory
    monkeypatch.setenv("DATABASE_URL", "postgresql://sebi_api:pw@ep-pooler.neon.tech:6543/neondb")
    monkeypatch.delenv("WORKER_DATABASE_URL", raising=False)

    with pytest.raises(ValueError, match="WORKER_DATABASE_URL must be explicitly configured"):
        get_repository_factory()


# ---------------------------------------------------------------------------
# 5. API Health & Readiness Probes
# ---------------------------------------------------------------------------

def test_api_health_endpoint():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_api_readiness_endpoint():
    client = TestClient(app)
    response = client.get("/ready")
    # Should be 200 when active snapshot and DB are reachable, or 503 with structured detail
    assert response.status_code in (200, 503)
    data = response.json()
    if response.status_code == 200:
        assert data.get("status") == "ready"
    else:
        assert "detail" in data
        assert data["detail"].get("status") == "not_ready"


# ---------------------------------------------------------------------------
# 6. Frontend Production Build & Secret Leak Prevention
# ---------------------------------------------------------------------------

def test_frontend_dist_contains_no_backend_secrets():
    dist_dir = REPO_ROOT / "frontend" / "dist"
    if not dist_dir.exists():
        pytest.skip("frontend/dist does not exist; run 'npm run build' first")

    # Read all assets in dist
    found_secrets = []
    sensitive_substrings = [
        "ADMIN_API_KEY",
        "AUTH_SECRET_KEY",
        "WORKER_DATABASE_URL",
        "DIRECT_DATABASE_URL",
        "GROQ_API_KEY",
        "GEMINI_API_KEY",
    ]

    for fpath in dist_dir.rglob("*"):
        if fpath.is_file() and fpath.suffix in (".js", ".html", ".css", ".map"):
            text = fpath.read_text(encoding="utf-8", errors="ignore")
            for sub in sensitive_substrings:
                if sub in text:
                    found_secrets.append((fpath.name, sub))

    assert len(found_secrets) == 0, f"Sensitive tokens found in built frontend bundle: {found_secrets}"
