# Ledger AI — developer entry points.
#
# Infrastructure (Postgres, Redis, MinIO) runs in Docker; the app processes run
# natively for fast reload. Production packaging lives in Phase 3.

SHELL := /bin/bash
API := apps/api
WEB := apps/web
PY := $(API)/.venv/bin

.DEFAULT_GOAL := help
.PHONY: help setup up down logs migrate revision seed reset dev dev-api dev-web dev-worker \
        test test-api test-web lint lint-api lint-web typecheck sample sweep lock clean \
        prod-build prod-up prod-down prod-smoke

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup: ## Install backend and frontend dependencies
	@test -f .env || (cp .env.example .env && echo "Created .env from .env.example — set AUTH_SECRET before running.")
	cd $(API) && uv venv --python 3.12 && uv pip install -e ".[dev]"
	cd $(WEB) && npm install
	@test -f $(WEB)/.env.local || (cp $(WEB)/.env.example $(WEB)/.env.local && \
		echo "Created apps/web/.env.local — its AUTH_SECRET must match the repo-root .env.")

up: ## Start Postgres (5433), Redis (6379) and MinIO (9000/9001)
	docker compose up -d
	@echo "Waiting for services to report healthy…"
	@for i in $$(seq 1 30); do \
		if [ "$$(docker compose ps --format '{{.Health}}' | grep -c healthy)" = "3" ]; then break; fi; \
		sleep 2; \
	done
	@docker compose ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'

down: ## Stop infrastructure (volumes are preserved)
	docker compose down

logs: ## Tail infrastructure logs
	docker compose logs -f

migrate: ## Apply database migrations
	cd $(API) && .venv/bin/alembic upgrade head

revision: ## Autogenerate a migration: make revision m="add thing"
	cd $(API) && .venv/bin/alembic revision --autogenerate -m "$(m)"

seed: ## Generate the synthetic demo dataset (use RESET=1 to regenerate)
	$(PY)/python scripts/seed_synthetic.py $(if $(RESET),--reset,)

sample: ## Regenerate the synthetic sample statement in docs/samples/
	$(PY)/python scripts/make_sample_csv.py

reset: ## Drop all data and reseed from scratch
	$(MAKE) migrate
	$(PY)/python scripts/seed_synthetic.py --reset

dev-api: ## Run the FastAPI server on :8000
	cd $(API) && .venv/bin/uvicorn ledgerai.main:app --reload --host 0.0.0.0 --port 8000

dev-worker: ## Run the RQ worker
	# OBJC_DISABLE_INITIALIZE_FORK_SAFETY: macOS aborts Objective-C runtime
	# initialisation inside a forked child, and RQ forks a work horse per job.
	# It is a harmless no-op on Linux and in containers.
	cd $(API) && OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES \
		.venv/bin/rq worker ledgerai --url redis://localhost:6379/0

dev-web: ## Run the Next.js dev server on :3000
	cd $(WEB) && npm run dev

dev: ## Run api, worker and web together (Ctrl-C stops all three)
	@trap 'kill 0' EXIT INT TERM; \
	$(MAKE) dev-api & \
	$(MAKE) dev-worker & \
	$(MAKE) dev-web & \
	wait

test: test-api test-web ## Run all tests

test-api: ## Backend tests (needs `make up`)
	cd $(API) && .venv/bin/pytest -q

test-web: ## Frontend component tests
	cd $(WEB) && npm run test

lint: lint-api lint-web ## Lint and typecheck everything

lint-api: ## ruff + mypy
	cd $(API) && .venv/bin/ruff check ledgerai tests ../../scripts && .venv/bin/mypy ledgerai

lint-web: ## eslint + tsc
	cd $(WEB) && npm run lint && npm run typecheck

typecheck: lint-web ## Alias for the frontend typecheck

prod-build: ## Build the three production images
	docker compose -f docker-compose.prod.yml build

prod-up: ## Run the production stack locally (needs AUTH_SECRET + DEMO_USER_PASSWORD)
	docker compose -f docker-compose.prod.yml up -d
	@docker compose -f docker-compose.prod.yml ps

prod-down: ## Stop the production stack
	docker compose -f docker-compose.prod.yml down

prod-smoke: ## Verify the production containers: non-root, healthy, migrations once
	./scripts/prod_smoke.sh

sweep: ## Run the retention sweep (stuck jobs, failed-upload files, stale receipts)
	$(PY)/python scripts/retention_sweep.py

lock: ## Re-resolve and write apps/api/uv.lock
	cd $(API) && uv lock

clean: ## Remove caches and build output
	rm -rf $(API)/.pytest_cache $(API)/.ruff_cache $(API)/.mypy_cache
	rm -rf $(WEB)/.next $(WEB)/coverage
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
