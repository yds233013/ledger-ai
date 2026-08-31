"""FastAPI application entry point."""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .config import settings
from .db import async_engine
from .routers import (
    alerts,
    analysis,
    auth,
    dashboard,
    receipts,
    statements,
    transactions,
    uploads,
    webhooks,
)
from .routers import settings as settings_router
from .security.logging import install_redaction
from .security.ratelimit import probe_limiter_store

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
)
# A safety net beneath the call-site convention of logging ids, not content.
install_redaction()
logger = logging.getLogger("ledgerai")

# Access logs record full query strings, and this API's query strings carry
# merchant search terms. Silence them where real data lives.
if settings.is_production and not settings.enable_access_log:
    logging.getLogger("uvicorn.access").disabled = True


DEFAULT_DEV_SECRET = "dev-only-insecure-secret-change-me"  # noqa: S105


def _check_production_config() -> tuple[list[str], list[str]]:
    """Inspect production configuration.

    Returns (fatal, advisory). The split matters: a weak signing secret or a
    published default password is exploitable and must stop the process, while
    a localhost CORS origin is usually just someone running the production
    images locally to verify them — worth saying, not worth refusing.
    """
    fatal: list[str] = []
    advisory: list[str] = []
    if not settings.is_production:
        return fatal, advisory

    if settings.auth_secret == DEFAULT_DEV_SECRET or len(settings.auth_secret) < 32:
        fatal.append("AUTH_SECRET is unset, too short, or still the development default.")
    if settings.demo_user_password == "demo1234":  # noqa: S105
        fatal.append("DEMO_USER_PASSWORD is still the development default.")

    if settings.trust_proxy_headers and not settings.trusted_proxy_networks:
        advisory.append(
            "TRUST_PROXY_HEADERS is on but TRUSTED_PROXY_IPS is empty or unparseable, "
            "so no forwarded address is believed and rate limits are keyed by the "
            "socket peer. Set TRUSTED_PROXY_IPS to the reverse proxy's address/CIDR."
        )
    if any("localhost" in origin for origin in settings.cors_origin_list):
        advisory.append(
            "CORS_ORIGINS points at localhost — expected only when verifying the "
            "production images locally."
        )
    if settings.storage_backend == "local":
        advisory.append(
            "STORAGE_BACKEND=local: receipts live on a mounted disk, so the API and "
            "worker must share it."
        )
    return fatal, advisory


@asynccontextmanager
async def lifespan(app: FastAPI):
    fatal, advisory = _check_production_config()
    for note in advisory:
        logger.warning("Production configuration note: %s", note)
    if fatal:
        for problem in fatal:
            logger.critical("Unsafe production configuration: %s", problem)
        raise RuntimeError(
            "Refusing to start in production with an unsafe configuration: "
            + " ".join(fatal)
        )

    logger.info(
        "Ledger AI API starting — env=%s storage=%s ai_enabled=%s",
        settings.environment,
        settings.storage_backend,
        settings.ai_available,
    )
    yield
    # uvicorn stops accepting connections on SIGTERM and waits for in-flight
    # requests before this returns, so a rolling deploy does not cut a stream.
    logger.info("Ledger AI API shutting down")


app = FastAPI(
    title="Ledger AI API",
    version="0.1.0",
    description=(
        "Personal finance data workspace. All demo data is synthetic. "
        "Numeric results are computed by SQL aggregates, never by a language model."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    # The browser cannot read Content-Disposition on a cross-origin response
    # unless it is exposed, and without it the export downloads as a generic
    # filename instead of the dated one the server chose.
    expose_headers=["Content-Disposition"],
)


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    """Never leak an internal error to a caller.

    The correlation id ties the generic response to the logged traceback, so a
    problem stays diagnosable without the response describing the internals.
    """
    correlation_id = uuid.uuid4().hex[:12]
    logger.exception("Unhandled error [%s] on %s", correlation_id, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": "Something went wrong. Please try again.",
            "code": "internal_error",
            "correlation_id": correlation_id,
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return the first useful message rather than a nested error tree."""
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(part) for part in first.get("loc", []) if part != "body")
    message = first.get("msg", "Invalid request")
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": f"{field}: {message}" if field else message, "code": "validation_error"},
    )


# Logged at most once per process: a missing taxonomy is a persistent
# condition, and repeating it on every probe would bury the rest of the log.
_warned_reference_data_missing = False


async def _probe_reference_data() -> bool:
    """Whether the system category taxonomy has actually been loaded.

    Its absence is invisible from the outside and catastrophic in a quiet way:
    the categorizer still runs and still returns answers, but `ingest_rows`
    resolves those slugs through a table with no rows, so every transaction is
    written with a NULL category and the whole product looks like it simply
    cannot categorize anything.

    This does NOT make the instance unready. The API serves uploads, search,
    receipts and deletion perfectly well without it, and refusing traffic would
    turn a degraded feature into an outage. It is reported, and warned about
    once, so the condition is visible rather than inferred from screenshots.
    """
    global _warned_reference_data_missing
    try:
        async with async_engine.connect() as connection:
            result = await connection.execute(
                text("SELECT COUNT(*) FROM categories WHERE user_id IS NULL")
            )
            count = int(result.scalar_one())
    except Exception:  # noqa: BLE001 - an unreachable database is reported separately
        return False

    if count == 0 and not _warned_reference_data_missing:
        _warned_reference_data_missing = True
        logger.error(
            "reference_data.missing table=categories system_rows=0 — transactions "
            "cannot be categorized until the taxonomy migration has been applied"
        )
    return count > 0


async def _probe_database() -> bool:
    """Whether the database answers right now.

    A trivial round-trip, not a query against a table: this must report on the
    connection, not on whether any particular migration has run.
    """
    try:
        async with async_engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - an unreachable database is the answer
        # No exception text: it carries the connection string.
        logger.warning("Readiness probe: the database did not answer")
        return False


@app.get("/health", tags=["meta"])
async def health() -> dict[str, object]:
    """LIVENESS — is this process alive and worth keeping?

    Deliberately returns 200 even when a dependency is down, and says so in the
    body. An orchestrator restarts a container that fails its liveness probe,
    and restarting every replica because a shared Redis blipped converts a
    degraded limiter into a total outage while fixing nothing. Whether traffic
    should still be routed here is a different question, answered by
    /health/ready below.
    """
    limiter_ok = await probe_limiter_store()
    return {
        "status": "ok" if limiter_ok else "degraded",
        "service": "ledgerai-api",
        "version": "0.1.0",
        "environment": settings.environment,
        "ai_enabled": settings.ai_available,
        "storage_backend": settings.storage_backend,
        "dependencies": {"rate_limit_store": "ok" if limiter_ok else "unavailable"},
        # Says what the degradation means for callers without naming the
        # technology behind it.
        "rate_limiting": "enforced"
        if limiter_ok
        else ("failing_closed" if settings.is_production else "failing_open"),
        "probe": "liveness",
    }


@app.get("/health/ready", tags=["meta"])
async def readiness(response: Response) -> dict[str, object]:
    """READINESS — should this instance receive traffic right now?

    503 when it should not, so a load balancer drains it instead of serving
    errors, and a rolling deploy waits rather than cutting over to an instance
    that cannot work. Two things make an instance unready:

      * **The database is unreachable.** Every user-scoped route needs it, so
        there is nothing useful left to serve.
      * **The rate-limit store is unreachable IN PRODUCTION.** Public endpoints
        fail closed there by design (see security/ratelimit.py), so login,
        upload and analysis would all be refused — an instance that rejects
        every anonymous caller should not be in rotation. In development the
        same outage fails open and the instance is still perfectly usable, so
        it stays ready.

    The body names which dependency is unhappy but not what or where it is:
    "database" is a role, not a hostname.
    """
    database_ok = await _probe_database()
    limiter_ok = await probe_limiter_store()
    reference_data_ok = await _probe_reference_data() if database_ok else False
    # Only the public/production combination is disqualifying.
    limiter_blocks_traffic = not limiter_ok and settings.is_production

    ready = database_ok and not limiter_blocks_traffic
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    reasons: list[str] = []
    if not database_ok:
        reasons.append("database_unavailable")
    if limiter_blocks_traffic:
        reasons.append("rate_limit_store_unavailable")
    # A missing taxonomy is deliberately NOT listed here. `reasons` answers
    # "why is this instance not ready", so anything in it must be
    # disqualifying; a non-blocking condition would make a `ready` response
    # contradict its own reasons. It is reported through `dependencies`
    # instead, and logged once at ERROR. See _probe_reference_data.

    return {
        "status": "ready" if ready else "not_ready",
        "service": "ledgerai-api",
        "probe": "readiness",
        "dependencies": {
            "database": "ok" if database_ok else "unavailable",
            "rate_limit_store": "ok" if limiter_ok else "unavailable",
            "reference_data": "ok" if reference_data_ok else "missing",
        },
        "reasons": reasons,
    }


app.include_router(auth.router)
app.include_router(webhooks.router)
app.include_router(uploads.router)
app.include_router(transactions.router)
app.include_router(dashboard.router)
app.include_router(analysis.router)
app.include_router(receipts.router)
app.include_router(statements.router)
app.include_router(alerts.router)
app.include_router(settings_router.router)
