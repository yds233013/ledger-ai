# Deployment

Ledger AI is packaged as three images built from two Dockerfiles, plus managed
Postgres and Redis. This document describes the Railway shape used for the
portfolio demo, and what would change for a real production deployment.

> **Nothing here has been deployed.** No Railway account exists, no
> infrastructure has been provisioned, and no GitHub OAuth application has been
> created. Those steps require account access and are listed under
> [What still needs a human](#what-still-needs-a-human).

---

## Services

Five services, of which three are ours:

| Service | Image | Purpose |
|---|---|---|
| `web` | `apps/web/Dockerfile` | Next.js. The only service the public reaches. |
| `api` | `apps/api/Dockerfile` (target `api`) | FastAPI. Reached by the browser for data, and by `web` for auth. |
| `worker` | `apps/api/Dockerfile` (target `worker`) | RQ consumer: CSV parsing, OCR, categorization, alert detection. |
| `postgres` | Railway managed | Every durable record. |
| `redis` | Railway managed | RQ broker, analysis cache, rate-limit counters. |

`api` and `worker` are two **targets of one Dockerfile**, so they share the
Python environment, the Tesseract install and the non-root user, and neither
can be built without the other's base being reproducible. Build them with:

```bash
docker build --target api    -t ledgerai-api    apps/api
docker build --target worker -t ledgerai-worker apps/api
```

Both build from a clean clone with nothing else present. CI asserts this by
deleting the API image and rebuilding the worker from source alone.

### Who talks to whom

```
browser ──HTTPS──▶ web (Next.js)          public
   │                 │
   │                 └──internal──▶ api   (sign-in, demo provisioning)
   └──HTTPS────────────────────────▶ api   public: data, SSE, receipt images

api ──▶ postgres        worker ──▶ postgres
api ──▶ redis           worker ──▶ redis
api ──▶ volume          worker ──▶ volume   (same disk, receipts)
```

The browser calls the API directly rather than proxying through Next.js,
because Ask Ledger streams over SSE and an extra hop buys nothing but
buffering risk. That is why `NEXT_PUBLIC_API_URL` must be the API's **public**
URL, while `web`'s server-side calls may use the internal one.

---

## Environment variables

### `api` and `worker` (identical)

| Variable | Example | Notes |
|---|---|---|
| `ENVIRONMENT` | `production` | Tightens startup checks, silences access logs, makes public rate limits fail **closed**. |
| `DATABASE_URL` | `postgresql+psycopg://…` | Railway's `DATABASE_URL` uses the `postgresql://` scheme — rewrite it to `postgresql+psycopg://`. |
| `REDIS_URL` | `redis://…` | From the managed Redis. |
| `AUTH_SECRET` | 32+ random bytes | **Must be byte-identical to `web`'s.** The API verifies tokens `web` mints. |
| `ACCESS_TOKEN_TTL_MINUTES` | `15` | |
| `STORAGE_BACKEND` | `local` | A Railway volume. See [Storage](#storage). |
| `LOCAL_STORAGE_DIR` | `/app/.localstorage` | The volume mount path. |
| `CORS_ORIGINS` | `https://<web-domain>` | No trailing slash. Not `*`. |
| `DEMO_USER_EMAIL` | `demo@ledgerai.local` | The seeded permanent account, if you seed one. |
| `DEMO_USER_PASSWORD` | random | Startup **refuses to boot** in production if this is still `demo1234`. |
| `TRUST_PROXY_HEADERS` | `true` | Only with the next one set. See [Proxy trust](#proxy-trust). |
| `TRUSTED_PROXY_IPS` | `10.0.0.0/8` | The proxy/edge CIDR. Both are required for either to take effect. |
| `AI_ENABLED` | `false` | The app is fully functional without a model. |
| `OPENAI_API_KEY` | *(blank)* | Only read when `AI_ENABLED=true`. |
| `LOG_LEVEL` | `INFO` | |
| `ENABLE_ACCESS_LOG` | `false` | Query strings carry merchant search terms. |

### `web`

| Variable | Example | Notes |
|---|---|---|
| `AUTH_SECRET` | *(same as api)* | |
| `AUTH_TRUST_HOST` | `true` | Required behind Railway's proxy. |
| `NEXTAUTH_URL` | `https://<web-domain>` | |
| `NEXT_PUBLIC_API_URL` | `https://<api-domain>` | **Build-time.** Inlined into the bundle, so it is a build arg, not just a runtime variable. |
| `AUTH_GITHUB_ID` | *(optional)* | Omit both to run without GitHub sign-in. |
| `AUTH_GITHUB_SECRET` | *(optional)* | |

### Generating secrets

```bash
openssl rand -base64 32          # AUTH_SECRET
openssl rand -base64 24          # DEMO_USER_PASSWORD, POSTGRES_PASSWORD
```

Generate `AUTH_SECRET` **once** and paste the same value into both services. A
mismatch produces a confusing failure: sign-in succeeds, then every API call
returns 401, because `web` signs a token `api` cannot verify.

---

## Migrations

Migrations run in **exactly one place** and never on service boot. If every
replica ran `alembic upgrade head` at startup they would race, and a
partially-applied migration is the worst state to be in.

* **Railway:** a pre-deploy command on the `api` service only:
  ```
  alembic upgrade head
  ```
* **Compose:** a one-shot `migrate` service that other services `depends_on`
  with `condition: service_completed_successfully`.

Every migration has a tested `downgrade()`. CI runs `upgrade head` from empty,
re-runs it (a no-op), then `downgrade base` and back up, and finally asserts
that autogenerate produces an empty diff — so a model change without a
migration cannot merge.

---

## Start commands

| Service | Command |
|---|---|
| `api` | `uvicorn ledgerai.main:app --host 0.0.0.0 --port $PORT --no-access-log` |
| `worker` | `rq worker ledgerai --url "$REDIS_URL"` |
| `web` | `node server.js` (Next.js standalone output) |

**The API is started without `--proxy-headers`.** That is deliberate and is a
security property, not an oversight — see [Proxy trust](#proxy-trust).

---

## Health checks: liveness vs readiness

Two endpoints answering two different questions. Confusing them is how a
shared-dependency blip becomes a total outage.

| | `/health` | `/health/ready` |
|---|---|---|
| Question | Is this process alive? | Should it receive traffic? |
| On a Redis outage | `200`, `"status": "degraded"` | `503` in production, `200` in development |
| On a database outage | `200` | `503` |
| Wire it to | *nothing that restarts* | Railway healthcheck, load-balancer |

`/health` returns 200 even while degraded on purpose. A liveness probe that
fails makes the orchestrator **restart** the container, and restarting every
replica because a shared Redis blipped fixes nothing while guaranteeing an
outage. Readiness is where "stop sending me traffic" belongs.

Readiness is 503 when:

* the database is unreachable — every user-scoped route needs it; or
* the rate-limit store is unreachable **and** `ENVIRONMENT=production` —
  public endpoints fail closed there, so the instance would refuse every
  anonymous caller. In development the same outage fails open and the
  instance stays ready.

Neither response names a host, a URL or a credential: `"database": "unavailable"`
is a role and a state, and nothing more.

`railway.json` points the healthcheck at `/health/ready`. The API image's
`HEALTHCHECK` does the same, which is what makes Compose's
`depends_on: {api: {condition: service_healthy}}` meaningful for `web`.

---

## Storage

The portfolio demo uses a **Railway volume** mounted at `/app/.localstorage` on
both `api` and `worker`, with `STORAGE_BACKEND=local`.

* The API writes an uploaded receipt; the worker reads it to run OCR. They must
  see the same bytes, so they share one volume.
* Attach the volume to **both** services at the same path. A worker without it
  fails every OCR job with a missing-file error.

This is the honest limitation of a volume: it ties both services to one
machine and does not scale horizontally.

**For a real production deployment**, set `STORAGE_BACKEND=minio` and point the
S3 variables at any S3-compatible service. The adapter already exists and is
the same interface (`services/storage.py`), so the change is configuration
only:

```
STORAGE_BACKEND=minio
S3_ENDPOINT_URL=https://s3.<region>.amazonaws.com
S3_BUCKET=…
S3_ACCESS_KEY=…
S3_SECRET_KEY=…
S3_REGION=…
```

### Migrating from a volume to object storage

1. Deploy with the volume still mounted and `STORAGE_BACKEND` unchanged.
2. Copy `users/**` from the volume into the bucket, preserving key paths
   exactly — keys are stored in `uploads.storage_key` and are not rewritten.
3. Switch `STORAGE_BACKEND=minio` and redeploy.
4. Verify a receipt image loads, then detach the volume.

Do it in that order: keys are portable between backends, but a switch with an
un-copied bucket leaves every stored receipt unreachable while the database
still references it.

---

## Proxy trust

Rate limits identify callers by IP address. Behind a proxy the socket peer is
the proxy, so `X-Forwarded-For` has to be consulted — but only when the request
genuinely came from a proxy we operate.

Both settings are required, and either alone does nothing:

```
TRUST_PROXY_HEADERS=true
TRUSTED_PROXY_IPS=10.0.0.0/8      # the edge/proxy range
```

If `TRUST_PROXY_HEADERS` is on and `TRUSTED_PROXY_IPS` is empty or
unparseable, **no** forwarded address is believed and limits fall back to the
socket peer. A misconfiguration under-trusts; it never opens the header up.

The API runs uvicorn **without** `--proxy-headers`. Those flags make uvicorn
rewrite `scope["client"]` from the header before the application sees the
request, and with `--forwarded-allow-ips="*"` it does so for any peer — which
hands an attacker a fresh login budget per forged header. Forwarded addresses
are resolved in the application instead, where the allow-list is enforced and
unit-tested (`tests/test_ratelimit_security.py`).

The chain is walked **right to left**, stopping at the first hop that is not a
configured proxy. Taking the leftmost entry is the bypass: that value is
whatever the caller sent.

---

## Scheduled jobs

Two sweeps, both idempotent and both safe to run against live traffic.

| Job | Command | Suggested interval |
|---|---|---|
| Expired demo cleanup | `python scripts/demo_cleanup.py` | hourly |
| Retention sweep | `python scripts/retention_sweep.py` | daily |

**Demo cleanup** deletes ephemeral demo accounts past their 24-hour deadline
and everything they own: database rows (one `DELETE` — every `users.id` foreign
key cascades), stored receipt files, cached analyses and their Redis index, and
any queued RQ job. It selects on `is_demo AND demo_expires_at IS NOT NULL AND
demo_expires_at < now()`. A real account has neither column set and the
permanent development demo user has `demo_expires_at` NULL, so neither can be
reached. Running it twice removes nothing the second time.

**Retention** fails jobs abandoned mid-pipeline (a worker killed outright never
runs its own failure handler), removes stored files for uploads that failed
more than 7 days ago, and deletes receipts never confirmed within 30 days.

On Railway, schedule these as cron services running the `api` image with the
command overridden. Locally: `make demo-sweep` and `make sweep`.

---

## Rollback

Railway keeps previous deployments and can redeploy one directly.

1. **Redeploy the previous image** from the deployments list.
2. **Check whether the schema moved.** If the bad release included a migration,
   redeploying the old image alone is not enough — the new schema is still
   live. Run the downgrade as a one-off command on the `api` service:
   ```
   alembic downgrade -1
   ```
   Every migration in this repo has a working `downgrade()`, and CI proves it
   by running `downgrade base` and back up on every push.
3. **Order matters.** Roll the application back *first*, then the schema.
   Additive migrations (this project's are all additive) are backwards
   compatible, so the old code runs fine against the new schema for the minutes
   in between.
4. Redis holds only caches and counters — nothing needs rolling back there. A
   flush is safe at any time; it costs a cold analysis cache.

---

## Backup and restore

* **Postgres is the only source of truth for records.** Enable Railway's
  managed backups. For a manual snapshot:
  ```bash
  pg_dump "$DATABASE_URL" --format=custom --file=ledgerai-$(date +%F).dump
  pg_restore --clean --if-exists --dbname="$DATABASE_URL" ledgerai-*.dump
  ```
* **Receipt files are not in the database.** A database backup alone restores
  rows that reference files that no longer exist. Back up the volume (or the
  bucket) alongside it, or accept that restored receipts will have working
  metadata and missing images.
* **Redis needs no backup.** Losing it costs a cold cache and reset rate-limit
  windows.
* **Test the restore.** An untested backup is a hypothesis. Restore into a
  scratch database and run `alembic current` plus a row count.

---

## Cost and trial limits

Honest expectations for a portfolio demo, not a quote:

* Railway's trial/hobby tier is usage-based. Five services (web, api, worker,
  Postgres, Redis) each consume resources continuously; the worker and API are
  mostly idle but do not scale to zero if a healthcheck is polling them.
* **The worker is the one to watch.** It holds a Postgres connection and polls
  Redis. It cannot be removed — uploads and OCR run there.
* A volume is billed for provisioned size, not usage.
* Expect the demo to exceed a free allowance if left running continuously.
  Mitigations: keep the volume small (1 GB is ample for synthetic receipts),
  keep `AI_ENABLED=false` so no per-token cost exists at all, and let the demo
  cleanup sweep keep the database small.

---

## GitHub OAuth (optional)

The app runs, signs in, and demos with no OAuth application. To enable it:

1. Create an OAuth app at <https://github.com/settings/developers>.
2. Set the **Authorization callback URL** to exactly:
   * local — `http://localhost:3000/api/auth/callback/github`
   * production — `https://<web-domain>/api/auth/callback/github`
3. Set `AUTH_GITHUB_ID` and `AUTH_GITHUB_SECRET` on the `web` service.

Only `read:user user:email` is requested — no repository, organisation or gist
access. A GitHub identity resolves to a Ledger AI account by GitHub's immutable
account id and never by email address; see `docs/security.md`.

---

## What still needs a human

Everything below requires account access, payment, or publishing, and none of
it has been done:

* Creating a Railway account and project, and provisioning Postgres, Redis and
  the volume.
* Setting the environment variables above, including generated secrets.
* Creating the GitHub OAuth application and its callback URL.
* Pushing this repository to a remote and connecting it to Railway.
* The first deploy, and the first `alembic upgrade head`.
* Registering the two cron schedules.
