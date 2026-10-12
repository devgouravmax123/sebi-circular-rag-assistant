"""
Application configuration management for the SEBI Regulatory Intelligence Platform.
Supports environment-based configuration, secure defaults, and database URL parsing
without exposing secrets in exceptions or logs.
"""
import os
from typing import Optional
from pydantic import BaseModel, Field, field_validator


class Settings(BaseModel):
    """
    Core application settings.
    Defaults to file-backed local mode when DATABASE_URL is not set.
    """
    app_name: str = "SEBI Regulatory Intelligence Platform"
    environment: str = Field(default_factory=lambda: os.getenv("APP_ENV", "development"))
    database_url: Optional[str] = Field(default_factory=lambda: os.getenv("DATABASE_URL"))
    frontend_origin: str = Field(default_factory=lambda: os.getenv("FRONTEND_ORIGIN", "http://localhost:5173"))
    processed_dir: str = Field(default_factory=lambda: os.getenv("PROCESSED_DIR", "data/processed"))
    indexes_dir: str = Field(default_factory=lambda: os.getenv("INDEXES_DIR", "data/indexes"))
    raw_dir: str = Field(default_factory=lambda: os.getenv("RAW_DIR", "data/raw"))
    backups_dir: str = Field(default_factory=lambda: os.getenv("BACKUPS_DIR", "data/backups"))
    admin_api_key: Optional[str] = Field(default_factory=lambda: os.getenv("ADMIN_API_KEY"))
    # Database connection pool settings (for hosted PostgreSQL)
    db_pool_size: int = Field(default_factory=lambda: int(os.getenv("DB_POOL_SIZE", "5")))
    db_max_overflow: int = Field(default_factory=lambda: int(os.getenv("DB_MAX_OVERFLOW", "10")))
    db_pool_timeout: float = Field(default_factory=lambda: float(os.getenv("DB_POOL_TIMEOUT", "30.0")))
    db_pool_recycle: int = Field(default_factory=lambda: int(os.getenv("DB_POOL_RECYCLE", "300")))
    # Phase 8E: Synthesis Provider Configuration
    synthesis_provider: str = Field(default_factory=lambda: os.getenv("SYNTHESIS_PROVIDER", "mock"))
    synthesis_timeout_seconds: float = Field(default_factory=lambda: float(os.getenv("SYNTHESIS_TIMEOUT_SECONDS", "5.0")))
    max_synthesis_evidence_chunks: int = Field(default_factory=lambda: int(os.getenv("MAX_SYNTHESIS_EVIDENCE_CHUNKS", "5")))
    # Phase 10.1.20: Public Rate Limiting Configuration
    rate_limit_query_per_minute: int = Field(default_factory=lambda: int(os.getenv("RATE_LIMIT_QUERY_PER_MINUTE", "20")))
    rate_limit_query_burst: int = Field(default_factory=lambda: int(os.getenv("RATE_LIMIT_QUERY_BURST", "10")))
    # Phase 10.1.26: Configurable Retrieval Mode (hybrid vs lexical_only)
    retrieval_mode: str = Field(default_factory=lambda: os.getenv("RETRIEVAL_MODE", "hybrid"))


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
    def is_database_enabled(self) -> bool:
        """Returns True only when a non-empty, non-whitespace DATABASE_URL is configured."""
        return bool(self.database_url and self.database_url.strip())

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

    from pydantic import model_validator

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        if self.is_production:
            if not self.admin_api_key or len(self.admin_api_key.strip()) < 16:
                raise ValueError(
                    "Production configuration error: ADMIN_API_KEY must be set and contain at least 16 characters in production."
                )
        return self

    @field_validator("database_url")
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
        return v_stripped

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
