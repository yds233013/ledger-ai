# Ledger AI API and RQ worker — production images.
#
# Multi-stage so the build toolchain never ships. Dependencies are installed
# from uv.lock with --frozen, so an image built today and one built next month
# contain the same versions.
#
# Two deployable images come out of this one file:
#
#   docker build --target api    -t ledgerai-api .     (also the default)
#   docker build --target worker -t ledgerai-worker .
#
# The worker used to live in its own Dockerfile beginning
# `FROM ledgerai-api:latest`, which meant it could only be built after the API
# image happened to exist locally under exactly that tag — so a clean clone
# could not build it, CI never built it at all, and it worked only because
# Compose iterates services in file order. Sharing a `runtime` stage here makes
# both images self-contained and independently buildable from a bare checkout.

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

# ---------- runtime (shared base; not a deployable image on its own) ----------
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

# Queue name and HOME live on the shared base, not just the worker stage.
#
# Docker does not derive HOME from /etc/passwd for a USER instruction, and the
# rq CLI writes its own bookkeeping there. Railway's config-as-code has no
# field for a Docker build target, so the worker service there runs THIS image
# — the default `api` stage — with its start command overridden. That only
# works if the shared base is already capable of hosting a worker, so the two
# variables a worker needs are set once, here, for every stage.
ENV RQ_QUEUE=ledgerai \
    HOME=/home/ledgerai

# Receipt files when STORAGE_BACKEND=local. Created here so the directory
# exists and is writable before any volume is mounted over it.
RUN mkdir -p /app/.localstorage && chown ledgerai:ledgerai /app/.localstorage

# --create-home above already made /home/ledgerai owned by this user, which is
# the writable HOME the rq CLI needs for its own bookkeeping.
USER ledgerai


# ---------- worker ----------
# Same base as the API so OCR behaves identically. The two run side by side and
# read the same receipts out of one object-storage bucket — not a shared disk,
# which separate services do not have. Compose and CI build this target
# explicitly; Railway cannot select a build target and runs the `api` stage
# with the worker start command instead.
FROM runtime AS worker

# RQ_QUEUE and HOME are inherited from `runtime`; see the note there.

# A worker has no port, so liveness is "can it still reach Redis".
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
  CMD python -c "import os,redis,sys; redis.Redis.from_url(os.environ['REDIS_URL']).ping()" || exit 1

# Runs the RQ consumer AND the maintenance scheduler in one process. `rq worker`
# alone still consumes the queue perfectly well, but it cannot run the periodic
# sweeps, which lost their own services to Railway's five-service Hobby cap —
# see ledgerai/worker.py and docs/architecture.md.
#
# SIGTERM still triggers RQ's warm shutdown, because RQ owns the main thread and
# installs its own handlers: the job in flight finishes before the process
# exits, so a deploy never abandons a half-parsed receipt.
CMD ["ledgerai-worker"]


# ---------- api ----------
# Last, so a plain `docker build` with no --target produces the API image.
FROM runtime AS api

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${API_PORT}/health/ready" || exit 1

# Deliberately NO --proxy-headers / --forwarded-allow-ips.
#
# Those flags make uvicorn rewrite scope["client"] from X-Forwarded-For before
# the application sees the request, and with --forwarded-allow-ips="*" it does
# so for any TCP peer. The rate limiter identifies callers by client address,
# so that hands an attacker a fresh login-attempt budget per forged header.
#
# Forwarded addresses are resolved in the application instead
# (security/ratelimit.py), where trust is gated on TRUST_PROXY_HEADERS plus an
# explicit TRUSTED_PROXY_IPS allow-list and is unit-tested. request.client.host
# therefore always holds the real TCP peer.
#
# uvicorn stops accepting connections on SIGTERM and drains in-flight requests,
# so a rolling deploy does not cut an in-progress SSE stream.
CMD ["sh", "-c", "exec uvicorn ledgerai.main:app --host 0.0.0.0 --port ${API_PORT} --no-access-log"]
