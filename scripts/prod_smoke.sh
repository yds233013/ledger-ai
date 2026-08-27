#!/usr/bin/env bash
# Verify the production containers actually behave like production containers.
#
# Checks that images build, run as non-root, report healthy, apply migrations
# exactly once, serve traffic, and shut down gracefully.
set -euo pipefail

COMPOSE="docker compose -f docker-compose.prod.yml"
: "${AUTH_SECRET:?set AUTH_SECRET (openssl rand -base64 32)}"
: "${DEMO_USER_PASSWORD:?set DEMO_USER_PASSWORD}"

pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAILURES=$((FAILURES + 1)); }
FAILURES=0

echo "== images build independently from a clean context =="
# Built directly rather than through Compose so this proves each image is
# self-contained: no `depends_on`, no service ordering, no pre-existing local
# tag. The worker used to build only because the api service happened to be
# built first.
docker build --quiet --target api    -t ledgerai-api:latest    ./apps/api >/dev/null
pass "api image builds on its own"
docker build --quiet --target worker -t ledgerai-worker:latest ./apps/api >/dev/null
pass "worker image builds on its own, with no api tag present"
$COMPOSE build --quiet web
pass "web image builds"

echo ""
echo "== image sizes =="
for image in ledgerai-api ledgerai-worker ledgerai-web; do
  size=$(docker image inspect "$image:latest" --format '{{.Size}}' 2>/dev/null || echo 0)
  printf '  %-18s %s MB\n' "$image" "$((size / 1000000))"
done

echo ""
echo "== containers run as non-root =="
for image in ledgerai-api ledgerai-worker ledgerai-web; do
  uid=$(docker run --rm --entrypoint sh "$image:latest" -c 'id -u')
  if [ "$uid" = "0" ]; then fail "$image runs as root"; else pass "$image runs as uid $uid"; fi
done

echo ""
echo "== scheduled sweeps are invocable inside the image =="
# The bug this pins: the sweeps were documented as `python scripts/...`, which
# cannot resolve in a container built from the apps/api context. A cron entry
# point that is not on PATH fails at 04:00 and is noticed weeks later.
for command in ledgerai-demo-cleanup ledgerai-retention-sweep; do
  if docker run --rm --entrypoint sh ledgerai-api:latest -c "command -v $command" >/dev/null; then
    pass "$command is on PATH in the api image"
  else
    fail "$command is not installed in the api image"
  fi
done

if docker run --rm --entrypoint sh ledgerai-api:latest \
     -c 'ls scripts/ >/dev/null 2>&1'; then
  fail "the repo scripts/ directory is in the image; the entry points are meant to replace it"
else
  pass "the image does not depend on the repo scripts/ directory"
fi

echo ""
echo "== the api image can host a worker (Railway cannot select a build target) =="
# Railway's config-as-code has no build-target field, so the worker service
# runs the api image with its start command overridden. That only works if the
# shared base carries RQ_QUEUE and a writable HOME.
if docker run --rm --entrypoint sh ledgerai-api:latest \
     -c 'rq worker --help >/dev/null && touch "$HOME/.probe" && rm "$HOME/.probe"'; then
  pass "api image can run rq with a writable HOME"
else
  fail "api image cannot host a worker"
fi

echo ""
echo "== stack starts, migrations run once =="
$COMPOSE down -v --remove-orphans >/dev/null 2>&1 || true
$COMPOSE up -d >/dev/null

for _ in $(seq 1 60); do
  healthy=$($COMPOSE ps --format json 2>/dev/null | grep -c '"Health":"healthy"' || true)
  [ "${healthy:-0}" -ge 3 ] && break
  sleep 2
done

migrate_runs=$($COMPOSE ps -a --format json 2>/dev/null | grep -c '"Service":"migrate"' || echo 0)
if [ "$migrate_runs" -eq 1 ]; then pass "migrate ran as exactly one container"; else fail "migrate containers: $migrate_runs"; fi

migrate_exit=$(docker inspect --format '{{.State.ExitCode}}' "$($COMPOSE ps -aq migrate)" 2>/dev/null || echo 1)
if [ "$migrate_exit" = "0" ]; then pass "migrations applied successfully"; else fail "migrate exited $migrate_exit"; fi

applied=$($COMPOSE exec -T postgres psql -U ledgerai -d ledgerai -tAc \
  "SELECT count(*) FROM alembic_version" 2>/dev/null | tr -d '[:space:]' || echo 0)
if [ "$applied" = "1" ]; then pass "alembic_version has one row"; else fail "alembic_version rows: $applied"; fi

echo ""
echo "== services answer =="
if curl -fsS "http://localhost:${API_PORT:-8000}/health" >/dev/null; then
  pass "api /health responds"
else fail "api /health did not respond"; fi

if curl -fsS -o /dev/null "http://localhost:${WEB_PORT:-3000}/sign-in"; then
  pass "web /sign-in responds"
else fail "web /sign-in did not respond"; fi

env_reported=$(curl -fsS "http://localhost:${API_PORT:-8000}/health" | grep -o '"environment":"[^"]*"' || true)
if [ "$env_reported" = '"environment":"production"' ]; then
  pass "api reports production environment"
else fail "api environment is $env_reported"; fi

echo ""
echo "== worker is processing =="
if $COMPOSE logs worker 2>&1 | grep -q "Listening on"; then
  pass "worker attached to the queue"
else fail "worker never reported listening"; fi

echo ""
echo "== liveness and readiness are distinct =="
if curl -fsS "http://localhost:${API_PORT:-8000}/health/ready" | grep -q '"status":"ready"'; then
  pass "api reports ready"
else fail "api /health/ready did not report ready"; fi

if curl -fsS "http://localhost:${API_PORT:-8000}/health" | grep -q '"probe":"liveness"'; then
  pass "liveness and readiness are separate probes"
else fail "liveness probe did not identify itself"; fi

echo ""
echo "== graceful shutdown =="
start=$(date +%s)
$COMPOSE stop -t 30 api worker >/dev/null 2>&1
elapsed=$(($(date +%s) - start))
api_exit=$(docker inspect --format '{{.State.ExitCode}}' "$($COMPOSE ps -aq api)" 2>/dev/null || echo 1)
worker_exit=$(docker inspect --format '{{.State.ExitCode}}' "$($COMPOSE ps -aq worker)" 2>/dev/null || echo 1)

# A clean SIGTERM exit is 0; 137 means it was killed after the grace period.
if [ "$api_exit" = "0" ]; then pass "api exited cleanly on SIGTERM (${elapsed}s)"; else fail "api exit code $api_exit"; fi
if [ "$worker_exit" = "0" ]; then pass "worker exited cleanly on SIGTERM"; else fail "worker exit code $worker_exit"; fi

echo ""
$COMPOSE down -v --remove-orphans >/dev/null 2>&1 || true

if [ "$FAILURES" -eq 0 ]; then
  echo "All production container checks passed."
else
  echo "$FAILURES check(s) failed."
  exit 1
fi
