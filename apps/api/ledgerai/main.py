"""FastAPI application entry point."""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import settings
from .routers import alerts, analysis, auth, dashboard, receipts, transactions, uploads
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


@app.get("/health", tags=["meta"])
async def health() -> dict[str, object]:
    """Liveness plus the state of the dependency that can silently degrade.

    Returns 200 even when the rate-limit store is down. The container is alive
    and most of the API still works, and returning unhealthy would have the
    orchestrator kill every replica over an outage none of them caused —
    turning a degraded limiter into a total one. The degradation is reported
    instead, which is what monitoring needs to see.
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
    }


app.include_router(auth.router)
app.include_router(uploads.router)
app.include_router(transactions.router)
app.include_router(dashboard.router)
app.include_router(analysis.router)
app.include_router(receipts.router)
app.include_router(alerts.router)
app.include_router(settings_router.router)
