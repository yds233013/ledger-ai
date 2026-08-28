# Railway service configuration

One file per service. There is deliberately **no `railway.json` at the
repository root**: Railway applies a root config to every service that has not
been told otherwise, so a single root file meant `web` and `worker` would each
have been built and started as a second copy of the API.

Two settings cannot live in these files and must be set in each service's
dashboard **Settings** page. Railway reads the config path *before* it knows
anything else about the service, so it is not something the config can declare
about itself.

| Service | Root Directory | Config-as-code path |
|---|---|---|
| `api` | `apps/api` | `/railway/api.json` |
| `worker` | `apps/api` | `/railway/worker.json` |
| `web` | `apps/web` | `/railway/web.json` |

**Five services, not seven.** Railway's Hobby plan caps a project at five, and
Postgres, Redis, `api`, `worker` and `web` fill it exactly. The two cron
services this directory once described have been folded into the worker, which
runs both sweeps on a Redis-locked schedule — see `ledgerai/maintenance/` and
docs/deployment.md, "Maintenance scheduling". `cron-demo-cleanup.json` and
`cron-retention.json` remain here as the deploy-them-separately configuration
for anyone on a plan with room; nothing deploys them today.

**Root Directory is the Docker build context**, which is why it is not
optional. `apps/web/Dockerfile` begins `COPY package.json package-lock.json ./`
and `apps/api/.dockerignore` excludes `tests/` and `.venv/` — both assume the
context is the app directory, exactly as `docker build ./apps/api` does
locally. `dockerfilePath` is then relative to that directory, so it is simply
`Dockerfile` in every file here.

**The config path is absolute from the repository root and does not follow
Root Directory.** That is what makes it possible for `api` and `worker` to
share a Root Directory while reading different configs.

## Why `worker` builds the API image

`apps/api/Dockerfile` has two deployable targets, `api` and `worker`. Railway's
config-as-code has **no field for a Docker build target**, so every service
rooted at `apps/api` builds the file's default final stage — which is `api`.

The `worker` service therefore runs the API image with its start command
overridden to `rq worker`. That is sound because both targets share the
`runtime` stage: the same virtualenv, the same Tesseract install, the same
non-root user, and — since the change that accompanied these files — the same
`RQ_QUEUE` and writable `HOME`, which were the only two things the worker
target used to add that a worker actually needs at runtime.

The start command is the one thing keeping the two services distinct, which is
why it lives in version control here rather than in dashboard state, and why
`apps/api/tests/test_railway_config.py` asserts that each file's command
matches its role.

Compose and CI still build both targets explicitly (`--target api`,
`--target worker`), so the worker target does not become dead code.

## Cron services

`cron-demo-cleanup` and `cron-retention` are ordinary services with a
`cronSchedule`: Railway starts the container on the schedule, runs the start
command, and expects it to exit. `restartPolicyType` is `NEVER` because a
failed sweep must not restart-loop — it should show as a failed run and wait
for the next tick.

Their start commands are console scripts installed by the package itself
(`ledgerai-demo-cleanup`, `ledgerai-retention-sweep`), so they resolve on PATH
inside the image. The repository-root `scripts/` directory is **not** in the
image and cannot be invoked from a deployed container.
