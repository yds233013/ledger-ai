"""FastAPI application entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import settings
from .routers import analysis, auth, dashboard, transactions, uploads
from .routers import settings as settings_router

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
)
logger = logging.getLogger("ledgerai")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Ledger AI API starting — storage=%s ai_enabled=%s",
        settings.storage_backend,
        settings.ai_available,
    )
    yield
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
    return {
        "status": "ok",
        "service": "ledgerai-api",
        "version": "0.1.0",
        "ai_enabled": settings.ai_available,
        "storage_backend": settings.storage_backend,
    }


app.include_router(auth.router)
app.include_router(uploads.router)
app.include_router(transactions.router)
app.include_router(dashboard.router)
app.include_router(analysis.router)
app.include_router(settings_router.router)
