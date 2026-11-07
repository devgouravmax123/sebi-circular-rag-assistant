"""
Application configuration management for the SEBI Regulatory Intelligence Platform.
Supports environment-based configuration, secure defaults, and database URL parsing
without exposing secrets in exceptions or logs.
"""
import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

# Automatically load backend/.env using absolute path relative to this file
# backend/app/core/config.py -> parent(core) -> parent(app) -> parent(backend)
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
_ENV_FILE = _BACKEND_DIR / ".env"
if _ENV_FILE.is_file():
    load_dotenv(dotenv_path=_ENV_FILE)


class Settings(BaseModel):
    """
    Core application settings.
    Defaults to file-backed local mode when DATABASE_URL is not set.
    """
    app_name: str = "SEBI Regulatory Intelligence Platform"
    environment: str = Field(default_factory=lambda: os.getenv("APP_ENV", "development"))
    database_url: Optional[str] = Field(
        default_factory=lambda: os.getenv("DATABASE_URL"),
        validate_default=True,
    )
    direct_database_url: Optional[str] = Field(
        default_factory=lambda: os.getenv("DIRECT_DATABASE_URL") or os.getenv("MIGRATION_DATABASE_URL"),
        validate_default=True,
    )
    worker_database_url: Optional[str] = Field(
        default_factory=lambda: os.getenv("WORKER_DATABASE_URL"),
        validate_default=True,
    )
    frontend_origin: str = Field(default_factory=lambda: os.getenv("FRONTEND_ORIGIN", "http://localhost:5173"))
    processed_dir: str = Field(default_factory=lambda: os.getenv("PROCESSED_DIR", "data/processed"))
    indexes_dir: str = Field(default_factory=lambda: os.getenv("INDEXES_DIR", "data/indexes"))
    raw_dir: str = Field(default_factory=lambda: os.getenv("RAW_DIR", "data/raw"))
    backups_dir: str = Field(default_factory=lambda: os.getenv("BACKUPS_DIR", "data/backups"))
    uploads_dir: str = Field(default_factory=lambda: os.getenv("UPLOADS_DIR", "data/uploads"))
    max_upload_bytes: int = Field(default_factory=lambda: int(os.getenv("MAX_UPLOAD_BYTES", str(30 * 1024 * 1024))))
    storage_provider: str = Field(default_factory=lambda: os.getenv("STORAGE_PROVIDER", "local"))
    # Milestone 11: Durable Object Storage (Cloudflare R2 / AWS S3)
    s3_endpoint_url: Optional[str] = Field(default_factory=lambda: os.getenv("S3_ENDPOINT_URL"))
    s3_bucket_name: Optional[str] = Field(default_factory=lambda: os.getenv("S3_BUCKET_NAME"))
    s3_access_key_id: Optional[str] = Field(default_factory=lambda: os.getenv("S3_ACCESS_KEY_ID"), repr=False)
    s3_secret_access_key: Optional[str] = Field(default_factory=lambda: os.getenv("S3_SECRET_ACCESS_KEY"), repr=False)
    s3_region_name: str = Field(default_factory=lambda: os.getenv("S3_REGION_NAME", "auto"))
    admin_api_key: Optional[str] = Field(default_factory=lambda: os.getenv("ADMIN_API_KEY"))
    # Database connection pool settings (for hosted PostgreSQL)
    db_pool_size: int = Field(default_factory=lambda: int(os.getenv("DB_POOL_SIZE", "5")))
    db_max_overflow: int = Field(default_factory=lambda: int(os.getenv("DB_MAX_OVERFLOW", "10")))
    db_pool_timeout: float = Field(default_factory=lambda: float(os.getenv("DB_POOL_TIMEOUT", "30.0")))
    db_pool_recycle: int = Field(default_factory=lambda: int(os.getenv("DB_POOL_RECYCLE", "300")))
    # Phase 8E: Synthesis Provider Configuration (mock, gemini, groq)
    synthesis_provider: str = Field(default_factory=lambda: os.getenv("SYNTHESIS_PROVIDER", "mock"))
    synthesis_timeout_seconds: float = Field(default_factory=lambda: float(os.getenv("SYNTHESIS_TIMEOUT_SECONDS", "5.0")))
    max_synthesis_evidence_chunks: int = Field(default_factory=lambda: int(os.getenv("MAX_SYNTHESIS_EVIDENCE_CHUNKS", "5")))
    gemini_api_key: Optional[str] = Field(default_factory=lambda: os.getenv("GEMINI_API_KEY"), repr=False)
    gemini_model: str = Field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-2.0-flash"))
    groq_api_key: Optional[str] = Field(default_factory=lambda: os.getenv("GROQ_API_KEY"), repr=False)
    groq_model: str = Field(default_factory=lambda: os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"))
    # Phase 10.1.20: Public Rate Limiting Configuration
    rate_limit_query_per_minute: int = Field(default_factory=lambda: int(os.getenv("RATE_LIMIT_QUERY_PER_MINUTE", "20")))
    rate_limit_query_burst: int = Field(default_factory=lambda: int(os.getenv("RATE_LIMIT_QUERY_BURST", "10")))
    # Phase 10.1.26: Configurable Retrieval Mode (hybrid vs lexical_only)
    retrieval_mode: str = Field(default_factory=lambda: os.getenv("RETRIEVAL_MODE", "hybrid"))
    # Phase 4 Reliability Hardening: LRU collection retrieval state cache capacity
    max_cached_collections: int = Field(default_factory=lambda: int(os.getenv("MAX_CACHED_COLLECTIONS", "5")))
    # Milestone 8: Authentication Configuration
    auth_secret_key: Optional[str] = Field(default_factory=lambda: os.getenv("AUTH_SECRET_KEY"), repr=False)
    auth_token_expire_minutes: int = Field(default_factory=lambda: int(os.getenv("AUTH_TOKEN_EXPIRE_MINUTES", "60")))
    auth_algorithm: str = Field(default_factory=lambda: os.getenv("AUTH_ALGORITHM", "HS256"))


    @property
    def is_lexical_only(self) -> bool:
        """Returns True if RETRIEVAL_MODE is lexical_only."""
        return self.retrieval_mode.strip().lower() == "lexical_only"

    @property
    def is_production(self) -> bool:
        """Returns True if APP_ENV is production."""
        return self.environment.lower() == "production"

    @property
    def is_admin_auth_enabled(self) -> bool:
        """Returns True when ADMIN_API_KEY is configured."""
        return bool(self.admin_api_key and self.admin_api_key.strip())

    @property
    def get_auth_secret_key(self) -> str:
        """
        Retrieves auth signing secret key.
        In development/test: falls back to a deterministic development key if unset.
        In production: strictly fails safe if missing or shorter than 32 characters.
        """
        key = self.auth_secret_key
        if not key or not key.strip():
            if self.is_production:
                raise ValueError("Security violation: AUTH_SECRET_KEY is mandatory and cannot be empty in production.")
            return "dev_auth_secret_key_minimum_32_chars_long_for_hs256_signing"
        if self.is_production and len(key.strip()) < 32:
            raise ValueError("Security violation: AUTH_SECRET_KEY must be at least 32 characters in production.")
        return key.strip()

    @property
    def is_database_enabled(self) -> bool:
        """Returns True only when a non-empty, non-whitespace DATABASE_URL is configured."""
        return bool(self.database_url and self.database_url.strip())

    @property
    def is_direct_database_enabled(self) -> bool:
        """Returns True only when a non-empty, non-whitespace DIRECT_DATABASE_URL is configured."""
        return bool(self.direct_database_url and self.direct_database_url.strip())

    @property
    def is_worker_database_enabled(self) -> bool:
        """Returns True only when a non-empty, non-whitespace WORKER_DATABASE_URL is configured."""
        return bool(self.worker_database_url and self.worker_database_url.strip())

    @property
    def allowed_cors_origins(self) -> list[str]:
        """
        Parses allowed frontend origins safely.
        In development: defaults to localhost:5173.
        In production: rejects wildcard origins ('*') when allow_credentials is True.
        """
        raw = self.frontend_origin.strip()
        if not raw:
            return ["http://localhost:5173"]

        origins = [o.strip() for o in raw.split(",") if o.strip()]
        if self.is_production:
            if "*" in origins:
                raise ValueError(
                    "Security violation: Wildcard CORS origin '*' is strictly prohibited in production when credentials are enabled."
                )
        return origins

    @property
    def sanitized_database_url(self) -> Optional[str]:
        """
        Returns database URL with password masked for safe diagnostic logging.
        Never logs real credentials.
        """
        if not self.database_url:
            return None
        try:
            from sqlalchemy.engine import make_url
            url = make_url(self.database_url)
            return url.render_as_string(hide_password=True)
        except Exception:
            return "[malformed_or_unparseable_url]"

    @property
    def sanitized_direct_database_url(self) -> Optional[str]:
        """
        Returns direct database URL with password masked for safe diagnostic logging.
        Never logs real credentials.
        """
        if not self.direct_database_url:
            return None
        try:
            from sqlalchemy.engine import make_url
            url = make_url(self.direct_database_url)
            return url.render_as_string(hide_password=True)
        except Exception:
            return "[malformed_or_unparseable_url]"

    @property
    def sanitized_worker_database_url(self) -> Optional[str]:
        """
        Returns worker database URL with password masked for safe diagnostic logging.
        Never logs real credentials.
        """
        if not self.worker_database_url:
            return None
        try:
            from sqlalchemy.engine import make_url
            url = make_url(self.worker_database_url)
            return url.render_as_string(hide_password=True)
        except Exception:
            return "[malformed_or_unparseable_url]"

    from pydantic import model_validator

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        if self.is_production:
            if not self.admin_api_key or len(self.admin_api_key.strip()) < 16:
                raise ValueError(
                    "Production configuration error: ADMIN_API_KEY must be set and contain at least 16 characters in production."
                )

            try:
                from sqlalchemy.engine import make_url
            except ImportError:
                make_url = None

            # 1. DATABASE_URL Segregation Guard
            if make_url and self.is_database_enabled and self.database_url:
                try:
                    parsed_db = make_url(self.database_url)
                except Exception as ex:
                    raise ValueError(f"Production configuration error: Malformed DATABASE_URL: {ex}")

                # Reject neondb_owner identity for web API runtime
                if parsed_db.username and parsed_db.username.lower() in ("neondb_owner",):
                    raise ValueError(
                        "Security violation: DATABASE_URL must not connect as migration owner 'neondb_owner' in production. "
                        "Web API runtime must use dedicated restricted role (sebi_api)."
                    )

                # If connecting to Neon in production, enforce pooled connection (port 6543 or -pooler hostname)
                if parsed_db.host and "neon.tech" in parsed_db.host.lower():
                    is_pooled = (parsed_db.port == 6543) or ("-pooler" in parsed_db.host.lower())
                    if not is_pooled:
                        raise ValueError(
                            f"Security violation: Production Neon DATABASE_URL must connect via connection pooler "
                            f"(port 6543 or '-pooler' host), but connected to direct unpooled host '{parsed_db.host}:{parsed_db.port or 5432}'."
                        )

            # 2. WORKER_DATABASE_URL Segregation Guard
            if make_url and self.is_worker_database_enabled and self.worker_database_url:
                try:
                    parsed_worker = make_url(self.worker_database_url)
                except Exception as ex:
                    raise ValueError(f"Production configuration error: Malformed WORKER_DATABASE_URL: {ex}")

                # Reject privileged owner or API identity for worker runtime
                if parsed_worker.username and parsed_worker.username.lower() in ("neondb_owner", "sebi_api"):
                    raise ValueError(
                        f"Security violation: WORKER_DATABASE_URL cannot connect as '{parsed_worker.username}' in production. "
                        "Worker runtime must use dedicated operational role (sebi_worker)."
                    )

            # 3. DIRECT_DATABASE_URL Segregation Guard
            if make_url and self.is_direct_database_enabled and self.direct_database_url:
                try:
                    parsed_direct = make_url(self.direct_database_url)
                except Exception as ex:
                    raise ValueError(f"Production configuration error: Malformed DIRECT_DATABASE_URL: {ex}")

                if parsed_direct.host and "neon.tech" in parsed_direct.host.lower():
                    if parsed_direct.username and parsed_direct.username.lower() != "neondb_owner":
                        raise ValueError(
                            f"Security violation: DIRECT_DATABASE_URL on Neon must connect as 'neondb_owner', "
                            f"found '{parsed_direct.username}'."
                        )
                    if "-pooler" in parsed_direct.host.lower() or parsed_direct.port == 6543:
                        raise ValueError(
                            "Security violation: DIRECT_DATABASE_URL must be direct unpooled connection (port 5432), "
                            "not a pooled endpoint."
                        )

        return self

    @field_validator("database_url", "worker_database_url", "direct_database_url")
    @classmethod
    def validate_database_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v_stripped = v.strip()
        if not v_stripped:
            return None
        # Basic scheme validation
        allowed_prefixes = (
            "postgresql://",
            "postgres://",
            "postgresql+asyncpg://",
            "postgresql+psycopg://",
            "postgresql+psycopg2://",
            "sqlite://",
            "sqlite+aiosqlite://",
        )
        if not any(v_stripped.startswith(prefix) for prefix in allowed_prefixes):
            raise ValueError(
                "Unsupported database dialect scheme. Must begin with postgresql:// or sqlite://"
            )
        # Normalize postgres:// and postgresql:// to postgresql+psycopg:// for SQLAlchemy 2.0 with installed psycopg v3
        if v_stripped.startswith("postgres://"):
            return "postgresql+psycopg://" + v_stripped[len("postgres://"):]
        if v_stripped.startswith("postgresql://"):
            return "postgresql+psycopg://" + v_stripped[len("postgresql://"):]
        return v_stripped

    @field_validator("synthesis_provider")
    @classmethod
    def validate_synthesis_provider(cls, v: str) -> str:
        if not v or not v.strip():
            return "mock"
        norm = v.strip().lower()
        if norm not in ("mock", "gemini", "groq"):
            raise ValueError(
                f"Unsupported SYNTHESIS_PROVIDER '{v}'. Must be 'mock', 'gemini', or 'groq'."
            )
        return norm

    @field_validator("retrieval_mode")
    @classmethod
    def validate_retrieval_mode(cls, v: str) -> str:
        if not v or not v.strip():
            return "hybrid"
        norm = v.strip().lower()
        if norm not in ("hybrid", "lexical_only"):
            raise ValueError(
                f"Unsupported RETRIEVAL_MODE '{v}'. Must be either 'hybrid' or 'lexical_only'."
            )
        return norm


_settings: Optional[Settings] = None


def get_settings(force_reload: bool = False) -> Settings:
    """Singleton getter for application settings."""
    global _settings
    if _settings is None or force_reload:
        _settings = Settings()
    return _settings
