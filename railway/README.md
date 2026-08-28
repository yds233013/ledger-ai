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
services this directory once described are gone: both sweeps now run inside the
worker on a Redis-locked schedule — see `ledgerai/maintenance/` and
docs/deployment.md, "Maintenance scheduling".

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

## Maintenance

There are no cron services. `cron-demo-cleanup.json` and `cron-retention.json`
were deleted along with the services they configured.

Both sweeps run on a daemon thread inside the `worker` service, which is why its
start command is `ledgerai-worker` rather than a bare `rq worker` — that entry
point starts the RQ consumer *and* the schedule in one process. A Redis lock
keeps exactly one replica running each sweep, and the last-run timestamp lives
in Redis so restarts neither repeat nor skip a run.

`ledgerai-demo-cleanup` and `ledgerai-retention-sweep` remain installed console
scripts, so an operator can still force a sweep:

```bash
railway run --service worker ledgerai-demo-cleanup
```
