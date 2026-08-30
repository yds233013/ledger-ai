"""Application settings, loaded from the repo-root .env file."""

from __future__ import annotations

from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_network
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

    # --- Clerk (managed authentication) -------------------------------------
    # OFF by default. With this false the API rejects every RS256 token and the
    # demo flow is the only way in, which is exactly the rollback position: one
    # variable, no deploy.
    clerk_enabled: bool = False
    # Frontend API URL of the Clerk instance; also the `iss` claim value.
    # e.g. https://<slug>.clerk.accounts.dev
    clerk_issuer: str = ""
    # Comma-separated origins permitted to have produced a token. Clerk's docs
    # are explicit that skipping the azp check opens the app to CSRF, so this is
    # required whenever clerk_enabled is on.
    clerk_authorized_parties: str = ""
    # Optional. Clerk session tokens carry no `aud` by default; if a custom
    # template adds one, set this and it becomes mandatory.
    clerk_audience: str = ""
    clerk_webhook_signing_secret: str = ""
    # Clerk Backend API key. Read ONLY to revoke an identity during account
    # deletion. Never logged, never returned, never placed in an error message.
    clerk_secret_key: str = ""
    clerk_api_base: str = "https://api.clerk.com/v1"
    # Bounded, so a hung provider cannot pin a worker thread. The sweep retries.
    clerk_http_timeout_seconds: float = 10.0
    # Seconds of clock skew tolerated on exp/nbf.
    clerk_leeway_seconds: int = 30

    # --- Private beta --------------------------------------------------------
    # Persistent accounts require a matching local invitation. Independent of
    # Clerk's own invite-only mode, which is the primary gate.
    beta_invite_only: bool = True
    # Version strings recorded against each consent. Bumping one re-prompts.
    terms_version: str = "2026-08-draft-1"
    privacy_version: str = "2026-08-draft-1"
    upload_consent_version: str = "2026-08-draft-1"

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
    # caller can forge it and sidestep rate limits. BOTH of these must be set
    # for a forwarded address to be believed: the flag on its own does nothing,
    # because "trust the header whenever it is present" IS the bypass.
    trust_proxy_headers: bool = False
    # Comma-separated IPs or CIDRs of the reverse proxies allowed to set
    # X-Forwarded-For. Kept a plain str for the same reason as cors_origins:
    # pydantic-settings JSON-decodes list-typed fields before validators run.
    trusted_proxy_ips: str = ""
    # Request access logs record full query strings, which for this API include
    # merchant search terms. Off by default in production.
    enable_access_log: bool = True
    analysis_cache_ttl_seconds: int = 3600

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def trusted_proxy_networks(self) -> tuple[IPv4Network | IPv6Network, ...]:
        """Parsed TRUSTED_PROXY_IPS. Unparseable entries are dropped, not guessed.

        A bare address becomes a /32 (or /128), so "10.0.0.5" and
        "10.0.0.0/8" are both expressible without a second setting.
        """
        networks: list[IPv4Network | IPv6Network] = []
        for entry in self.trusted_proxy_ips.split(","):
            candidate = entry.strip()
            if not candidate:
                continue
            try:
                networks.append(ip_network(candidate, strict=False))
            except ValueError:
                # Logged by security.ratelimit on first use; config must not
                # import logging machinery just to complain here.
                continue
        return tuple(networks)

    @property
    def proxy_trust_active(self) -> bool:
        """Whether a forwarded client address may be believed at all.

        Fail safe: the flag without a usable allow-list means no proxy is
        trusted, so a misconfiguration under-trusts rather than opening the
        header to everyone.
        """
        return self.trust_proxy_headers and bool(self.trusted_proxy_networks)

    @property
    def sync_database_url(self) -> str:
        """Synchronous URL for the RQ worker, Alembic and seed scripts."""
        return self.database_url.replace("+asyncpg", "+psycopg")

    @property
    def async_database_url(self) -> str:
        """Async URL for the FastAPI request path (psycopg3 async)."""
        return self.database_url

    @property
    def clerk_jwks_url(self) -> str:
        """Derived, never configured separately.

        A second variable could drift from the issuer, and a JWKS fetched from
        somewhere other than the issuer is the whole attack.
        """
        return f"{self.clerk_issuer.rstrip('/')}/.well-known/jwks.json"

    @property
    def clerk_authorized_party_list(self) -> list[str]:
        return [p.strip() for p in self.clerk_authorized_parties.split(",") if p.strip()]

    @property
    def clerk_configured(self) -> bool:
        """Whether Clerk verification may run at all.

        Enabled but unconfigured must not mean "accept anything": without an
        issuer or an authorized party there is nothing to check a token
        against, so this returns False and the RS256 path stays closed.
        """
        return bool(self.clerk_enabled and self.clerk_issuer and self.clerk_authorized_party_list)

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
