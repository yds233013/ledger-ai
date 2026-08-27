# Ledger AI API — production image.
#
# Multi-stage so the build toolchain never ships. Dependencies are installed
# from uv.lock with --frozen, so an image built today and one built next month
# contain the same versions.

# ---------- builder ----------
FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first: this layer is cached until the lock file changes, so an
# application edit does not reinstall 74 packages.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY ledgerai ./ledgerai
COPY alembic ./alembic
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

# ---------- runtime ----------
FROM python:3.12-slim-bookworm AS runtime

# tesseract-ocr is a genuine runtime dependency: receipts are read locally and
# no OCR data leaves the machine. eng is the only language pack the parser
# supports today.
RUN apt-get update \
 && apt-get install --no-install-recommends -y \
      tesseract-ocr \
      tesseract-ocr-eng \
      curl \
 && rm -rf /var/lib/apt/lists/*

# Non-root. The uid is fixed so a mounted volume's ownership is predictable.
RUN groupadd --gid 10001 ledgerai \
 && useradd --uid 10001 --gid ledgerai --create-home --shell /usr/sbin/nologin ledgerai

WORKDIR /app

COPY --from=builder --chown=ledgerai:ledgerai /app /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ENVIRONMENT=production \
    API_PORT=8000

# Receipt files when STORAGE_BACKEND=local. Created here so the directory
# exists and is writable before any volume is mounted over it.
RUN mkdir -p /app/.localstorage && chown ledgerai:ledgerai /app/.localstorage

USER ledgerai
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${API_PORT}/health" || exit 1

# uvicorn stops accepting connections on SIGTERM and drains in-flight requests,
# so a rolling deploy does not cut an in-progress SSE stream.
CMD ["sh", "-c", "exec uvicorn ledgerai.main:app --host 0.0.0.0 --port ${API_PORT} --no-access-log --proxy-headers --forwarded-allow-ips='*'"]
