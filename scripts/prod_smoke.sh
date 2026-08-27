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

echo "== building images =="
$COMPOSE build --quiet
pass "all three images build"

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
