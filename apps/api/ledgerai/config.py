"""Application settings, loaded from the repo-root .env file."""

from __future__ import annotations

from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_network
from pathlib import Path

from pydantic import model_validator
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

    # --- Private-beta quotas -------------------------------------------------
    # Durable, per-user budgets for PERSISTENT (Clerk) accounts only. Demo
    # accounts keep the existing Redis rate limits and their 24-hour expiry;
    # nothing here applies to them.
    #
    # These are PRIVATE-BETA DEFAULTS, not permanent product limits. They are
    # sized for roughly twenty invited accounts against a 5 GB Postgres volume
    # and Cloudflare R2's free tier, and are expected to change.
    #
    # Daily windows reset at UTC midnight. UTC rather than a local zone because
    # the reset must not move when a user travels, and the server has no
    # business guessing where they are.
    quota_uploads_per_day: int = 25
    quota_upload_bytes_per_day: int = 50 * 1024 * 1024
    quota_stored_bytes: int = 250 * 1024 * 1024
    quota_transaction_rows: int = 25_000
    quota_receipts: int = 500
    quota_concurrent_jobs: int = 3
    quota_max_job_attempts: int = 3

    # --- Statement PDF import -----------------------------------------------
    # Pages, not bytes, are the cost driver: a long statement is a small file
    # that keeps the single worker busy. Receipts keep their own five-page cap
    # in the OCR path and are unaffected by anything here.
    max_statement_pages: int = 40
    quota_statement_pages_per_day: int = 120
    quota_statement_imports: int = 60
    # How long an unconfirmed import — original PDF, renderings and staged rows
    # — survives before it is purged. Ten times shorter than the unconfirmed
    # receipt window, because a statement is a far more sensitive thing to leave
    # lying around, and still long enough to come back to over a weekend.
    statement_review_hours: int = 72
    # A row is pre-ticked for import only at or above this confidence.
    statement_confidence_threshold: float = 0.80
    # Pages sampled for the text-layer/render cross-check. Retained so a
    # deployment that still sets it starts cleanly; the cross-check reads every
    # page that carries money tokens, because sampling three pages of a twelve
    # page statement inspects a tampered one with probability 3/12.
    statement_verify_sample_pages: int = 3
    # Render resolution for the cross-check. Below roughly 150 DPI OCR begins
    # misreading digits on legitimate pages, which shows up as false conflicts
    # rather than as missed tokens, so the base sits a clear step above that and
    # only inconclusive pages pay for the retry.
    statement_verify_dpi: int = 200
    statement_verify_retry_dpi: int = 300
    # Resolution for re-reading a single token's own region when the full-page
    # pass missed it but the pixels show the value really is drawn.
    statement_verify_focus_dpi: int = 400
    # A page must resolve this share of its money tokens, and may leave at most
    # max(count, fraction) of them unread, before it is accepted. Conflicting
    # values are never tolerated at any count.
    statement_verify_min_coverage: float = 0.90
    statement_verify_omission_fraction: float = 0.05
    statement_verify_max_omissions: int = 3

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

    @model_validator(mode="after")
    def _validate_quotas(self) -> Settings:
        """A quota that cannot admit one legal upload is a closed door.

        Checked at construction rather than per request: a deployment
        misconfigured this way should refuse to start, not accept traffic and
        then reject every upload for a reason nobody can see.
        """
        positives = {
            "QUOTA_UPLOADS_PER_DAY": self.quota_uploads_per_day,
            "QUOTA_UPLOAD_BYTES_PER_DAY": self.quota_upload_bytes_per_day,
            "QUOTA_STORED_BYTES": self.quota_stored_bytes,
            "QUOTA_TRANSACTION_ROWS": self.quota_transaction_rows,
            "QUOTA_RECEIPTS": self.quota_receipts,
            "QUOTA_CONCURRENT_JOBS": self.quota_concurrent_jobs,
            "QUOTA_MAX_JOB_ATTEMPTS": self.quota_max_job_attempts,
            "MAX_STATEMENT_PAGES": self.max_statement_pages,
            "QUOTA_STATEMENT_PAGES_PER_DAY": self.quota_statement_pages_per_day,
            "QUOTA_STATEMENT_IMPORTS": self.quota_statement_imports,
            "STATEMENT_REVIEW_HOURS": self.statement_review_hours,
            "STATEMENT_VERIFY_SAMPLE_PAGES": self.statement_verify_sample_pages,
            "STATEMENT_VERIFY_DPI": self.statement_verify_dpi,
            "STATEMENT_VERIFY_RETRY_DPI": self.statement_verify_retry_dpi,
            "STATEMENT_VERIFY_MIN_COVERAGE": self.statement_verify_min_coverage,
        }
        for name, value in positives.items():
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")

        if not 0.0 < self.statement_confidence_threshold <= 1.0:
            raise ValueError("STATEMENT_CONFIDENCE_THRESHOLD must be within (0, 1]")

        if not 0.0 < self.statement_verify_min_coverage <= 1.0:
            raise ValueError("STATEMENT_VERIFY_MIN_COVERAGE must be within (0, 1]")
        if not 0.0 <= self.statement_verify_omission_fraction < 1.0:
            raise ValueError("STATEMENT_VERIFY_OMISSION_FRACTION must be within [0, 1)")
        if self.statement_verify_max_omissions < 0:
            raise ValueError("STATEMENT_VERIFY_MAX_OMISSIONS must not be negative")
        if self.statement_verify_retry_dpi < self.statement_verify_dpi:
            raise ValueError(
                "STATEMENT_VERIFY_RETRY_DPI must be at least STATEMENT_VERIFY_DPI, "
                "or the adaptive retry would read the page less clearly than the "
                "pass that was already inconclusive"
            )

        if self.quota_statement_pages_per_day < self.max_statement_pages:
            raise ValueError(
                "QUOTA_STATEMENT_PAGES_PER_DAY must admit at least one full statement"
            )

        if self.quota_upload_bytes_per_day < self.max_upload_bytes:
            raise ValueError(
                "QUOTA_UPLOAD_BYTES_PER_DAY must be at least MAX_UPLOAD_BYTES, "
                "or no upload of the permitted size could ever succeed"
            )
        if self.quota_stored_bytes < self.max_upload_bytes:
            raise ValueError(
                "QUOTA_STORED_BYTES must be at least MAX_UPLOAD_BYTES, "
                "or a first upload of the permitted size could never be stored"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
