"""Application settings, loaded from the repo-root .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _find_env_file() -> Path | None:
    """Locate the repo-root .env, if there is one.

    In development this file lives three directories above the package. In a
    container the package sits at /app/ledgerai with no repo above it and
    configuration arrives as real environment variables, so walking up must not
    assume a depth that isn't there.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / ".env"
        if candidate.is_file():
            return candidate
    return None


ENV_FILE = _find_env_file()
# Kept for callers that resolve paths relative to the project (the local
# storage backend). Falls back to the working directory inside a container.
REPO_ROOT = ENV_FILE.parent if ENV_FILE else Path.cwd()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Database -----------------------------------------------------------
    database_url: str = "postgresql+psycopg://ledgerai:ledgerai@localhost:5433/ledgerai"

    # --- Redis --------------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"

    # --- Auth ---------------------------------------------------------------
    auth_secret: str = "dev-only-insecure-secret-change-me"  # noqa: S105 - overridden by .env
    access_token_ttl_minutes: int = 15
    demo_user_email: str = "demo@ledgerai.local"
    demo_user_password: str = "demo1234"  # noqa: S105 - dev default, overridden by .env

    # --- Storage ------------------------------------------------------------
    storage_backend: str = "minio"  # "minio" | "local"
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "ledgerai"
    s3_secret_key: str = "ledgerai"  # noqa: S105 - dev default, overridden by .env
    s3_bucket: str = "ledgerai-uploads"
    s3_region: str = "us-east-1"
    local_storage_dir: str = ".localstorage"

    # --- AI -----------------------------------------------------------------
    # Phase 1 ships the deterministic engine only. These exist so the Phase 2
    # LLM planner/categorizer/narrator can be switched on without refactoring.
    ai_enabled: bool = False
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    # --- App ----------------------------------------------------------------
    api_host: str = "0.0.0.0"  # noqa: S104 - bound inside the dev container/host by design
    api_port: int = 8000
    # Comma-separated in .env; parsed by the cors_origin_list property below.
    # Kept as a plain str because pydantic-settings JSON-decodes list-typed
    # fields before validators run, which rejects "a,b" syntax.
    cors_origins: str = "http://localhost:3000"
    max_upload_bytes: int = 10 * 1024 * 1024
    log_level: str = "INFO"
    # "development" | "production". Production tightens startup checks and
    # turns off request access logging.
    environment: str = "development"
    # Only trust X-Forwarded-For when a known proxy sits in front; otherwise a
    # caller can forge it and sidestep rate limits.
    trust_proxy_headers: bool = False
    # Request access logs record full query strings, which for this API include
    # merchant search terms. Off by default in production.
    enable_access_log: bool = True
    analysis_cache_ttl_seconds: int = 3600

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def sync_database_url(self) -> str:
        """Synchronous URL for the RQ worker, Alembic and seed scripts."""
        return self.database_url.replace("+asyncpg", "+psycopg")

    @property
    def async_database_url(self) -> str:
        """Async URL for the FastAPI request path (psycopg3 async)."""
        return self.database_url

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() == "production"

    @property
    def ai_available(self) -> bool:
        """True only when AI is switched on *and* a key is actually present."""
        return self.ai_enabled and bool(self.openai_api_key.strip())

    @property
    def local_storage_path(self) -> Path:
        path = Path(self.local_storage_dir)
        return path if path.is_absolute() else REPO_ROOT / path


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
