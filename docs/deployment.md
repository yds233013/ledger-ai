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

Seven services, of which five are ours:

| Service | Root Directory | Config file | Purpose |
|---|---|---|---|
| `web` | `apps/web` | `railway/web.json` | Next.js. The only service the public reaches for pages. |
| `api` | `apps/api` | `railway/api.json` | FastAPI. Reached by the browser for data, and by `web` for auth. |
| `worker` | `apps/api` | `railway/worker.json` | RQ consumer: CSV parsing, OCR, categorization, alert detection. |
| `cron-demo-cleanup` | `apps/api` | `railway/cron-demo-cleanup.json` | Hourly. Deletes expired demo accounts. |
| `cron-retention` | `apps/api` | `railway/cron-retention.json` | Daily. Stuck jobs, failed-upload files, stale receipts. |
| `postgres` | — | Railway managed | Every durable record. |
| `redis` | — | Railway managed | RQ broker, analysis cache, rate-limit counters. |

**There is no `railway.json` at the repository root, and there must not be.**
Railway applies a root config to every service that has not been told
otherwise, so one root file naming the API's Dockerfile and start command would
have built and started `web` and `worker` as second copies of the API. Each
service points at its own file instead; `railway/README.md` explains the two
dashboard settings that cannot live in those files, and
`apps/api/tests/test_railway_config.py` asserts the split holds.

`api` and `worker` are two **targets of one Dockerfile**, so they share the
Python environment, the Tesseract install and the non-root user, and neither
can be built without the other's base being reproducible. Build them with:

```bash
docker build --target api    -t ledgerai-api    apps/api
docker build --target worker -t ledgerai-worker apps/api
```

Both build from a clean clone with nothing else present. CI asserts this by
deleting the API image and rebuilding the worker from source alone.

**On Railway the worker runs the API image.** Config-as-code has no field for a
Docker build target, so every service rooted at `apps/api` gets the file's
default final stage, which is `api`. That is sound because both targets share
the `runtime` stage — including `RQ_QUEUE` and a writable `HOME`, which is why
those two live on the shared base rather than on the worker stage. The start
command is the only thing separating the services, which is why it is in
version control rather than in dashboard state.

### Who talks to whom

```
browser ──HTTPS──▶ web (Next.js)          public
   │                 │
   │                 └──internal──▶ api   (sign-in, demo provisioning)
   └──HTTPS────────────────────────▶ api   public: data, SSE, receipt images

api ──▶ postgres        worker ──▶ postgres
api ──▶ redis           worker ──▶ redis
api ──▶ R2 bucket       worker ──▶ R2 bucket   (same objects, receipts)
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
| `STORAGE_BACKEND` | `minio` | Selects the S3-compatible adapter. The name is the local service, not the protocol — it is the correct value for R2. See [Storage](#storage). |
| `S3_ENDPOINT_URL` | `https://<account-id>.r2.cloudflarestorage.com` | The R2 S3 API endpoint, not the public bucket URL. |
| `S3_BUCKET` | `ledgerai-uploads` | Must already exist; see [Storage](#storage). |
| `S3_REGION` | `auto` | R2 accepts only `auto`. |
| `S3_ACCESS_KEY` | *(R2 access key id)* | |
| `S3_SECRET_KEY` | *(R2 secret access key)* | |
| `CORS_ORIGINS` | `https://<web-domain>` | No trailing slash. Not `*`. |
| `DEMO_USER_EMAIL` | `demo@ledgerai.local` | The seeded permanent account, if you seed one. |
| `DEMO_USER_PASSWORD` | random | Startup **refuses to boot** in production if this is still `demo1234`. |
| `TRUST_PROXY_HEADERS` | `false` at first deploy | Only ever set with the next one. **Read [Proxy trust](#proxy-trust) before changing it** — the value cannot be guessed. |
| `TRUSTED_PROXY_IPS` | *(blank at first deploy)* | Set from an address the deployment reports, never from a published range. Both are required for either to take effect. |
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

* **Railway:** a `preDeployCommand` in `railway/api.json` and nowhere else.
  `test_railway_config.py` asserts no other service declares one, so a copied
  config cannot quietly add a second migrator.
* **Compose:** a one-shot `migrate` service that other services `depends_on`
  with `condition: service_completed_successfully`.

Every migration has a tested `downgrade()`. CI runs `upgrade head` from empty,
re-runs it (a no-op), then `downgrade base` and back up, and finally asserts
that autogenerate produces an empty diff — so a model change without a
migration cannot merge.

---

## Start commands

Each lives in that service's config file rather than in dashboard state.

| Service | Command |
|---|---|
| `api` | `sh -c 'exec uvicorn ledgerai.main:app --host 0.0.0.0 --port "${PORT:-8000}" --no-access-log'` |
| `worker` | `rq worker ledgerai --url "$REDIS_URL"` |
| `web` | `node server.js` (Next.js standalone output) |
| `cron-demo-cleanup` | `ledgerai-demo-cleanup` |
| `cron-retention` | `ledgerai-retention-sweep` |

**The API command is wrapped in `sh -c`, and that is load-bearing.** Railway
execs the start command directly rather than handing it to a shell, so a bare
`--port $PORT` arrives at uvicorn as four literal characters and it exits with
`Invalid value for '--port': '$PORT' is not a valid integer`. The `exec` keeps
uvicorn as PID 1 so it receives SIGTERM itself and drains in-flight requests
rather than being killed under a shell that ignored the signal.

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

`railway/api.json` points the healthcheck at `/health/ready`, and no other
service declares one — the worker has no port, and the cron services exit by
design, so a healthcheck there would call a successful run a failure. The API
image's `HEALTHCHECK` does the same, which is what makes Compose's
`depends_on: {api: {condition: service_healthy}}` meaningful for `web`.

---

## Storage

Receipt images live in a **Cloudflare R2 bucket**, reached through the existing
S3-compatible adapter (`services/storage.py`). Set `STORAGE_BACKEND=minio` —
the value names the local development service, not the protocol, and the same
adapter serves MinIO, R2 and S3 without a code path of its own.

### Why not a Railway volume

The API writes an uploaded receipt and the worker reads it back to run OCR, so
they must see the same bytes. They are separate services on separate
filesystems, and **a Railway volume attaches to one service**. Mounting it on
the API leaves the worker unable to read anything it was asked to process; the
reverse leaves the API unable to serve an image it just stored.

That failure is quiet in the worst way. Uploads return 200, the job fails on a
missing file, and the container's disk is discarded on the next deploy anyway.
So `get_storage()` **refuses to fall back to local disk when
`ENVIRONMENT=production`** and raises instead, and
`tests/test_storage_config.py` pins that. `STORAGE_BACKEND=local` remains
correct for local development and for `docker-compose.prod.yml`, where both
containers genuinely do share one machine.

### Creating the bucket

The bucket must exist before the first deploy. Auto-creation runs **only**
outside production: an R2 API token scoped to a single bucket — the shape worth
using — has no `CreateBucket` permission, so attempting it would turn a clear
"the bucket is missing" into a confusing "access denied".

In production a bucket that cannot be reached with the configured credentials
is a hard startup error naming the four variables to check, not a silent
degradation.

### R2 specifics

| Setting | Value | Why |
|---|---|---|
| `S3_REGION` | `auto` | R2 accepts nothing else. |
| `S3_ENDPOINT_URL` | `https://<account-id>.r2.cloudflarestorage.com` | The S3 API endpoint. **Not** the public `r2.dev` bucket URL. |
| Addressing | path-style | Set in the adapter. Virtual-host style needs per-bucket DNS that the R2 endpoint does not provide. |
| Public access | **off** | Receipts are private. The API streams them to an authenticated, user-scoped route; the bucket itself is never public. |

Keep the bucket private. Nothing in the application generates a public URL or a
presigned link — `receipts.py` reads the object and returns the bytes on an
authenticated route — so public access buys nothing and would expose every
stored receipt to anyone who guessed a key.

If uploads fail against R2 with a checksum or `aws-chunked` error, set
`AWS_REQUEST_CHECKSUM_CALCULATION=when_required` on `api` and `worker`.
botocore reads that environment variable directly; no code change is involved.

### Migrating between backends

Storage keys are portable — they are stored in `uploads.storage_key` and are
never rewritten — so moving between a volume, MinIO and R2 is a copy plus a
variable change. Copy `users/**` into the destination preserving key paths
exactly, switch `STORAGE_BACKEND` and the `S3_*` variables, redeploy, then
verify a receipt image loads before decommissioning the old backend. Doing it
in the other order leaves every stored receipt unreachable while the database
still references it.

## Proxy trust

Rate limits identify callers by IP address. Behind a proxy the socket peer is
the proxy, so `X-Forwarded-For` has to be consulted — but only when the request
genuinely came from a proxy we operate. Both settings are required, and either
alone does nothing:

```
TRUST_PROXY_HEADERS=true
TRUSTED_PROXY_IPS=<an address you have observed, not one you read about>
```

### Railway publishes no range to put there

This was researched against Railway's own documentation rather than assumed.
`docs.railway.com/networking/public-networking` and the public-networking
reference say nothing about `X-Forwarded-For`, `X-Real-IP`, or edge proxy
addresses. The one "static IP" feature Railway documents,
[Static Outbound IPs](https://docs.railway.com/networking/static-outbound-ips),
is **outbound only** — traffic from your service to third parties — and has no
bearing on what address inbound requests arrive from.

What exists is community-forum guidance, and it does not agree with itself:

* A Railway employee states the edge strips `X-Forwarded-For` so clients cannot
  overwrite it, and that the **leftmost** entry is the real client
  ([thread](https://station.railway.com/questions/security-critical-questions-on-edge-prox-8fddd775)).
* Another employee recommends "use `X-Forwarded-For` and take the first IP",
  while calling the guidance interim and noting `X-Real-IP` is expected to
  change ([thread](https://station.railway.com/questions/which-header-should-i-rely-on-for-real-c-d78a6f96)).
* A third thread is a confirmed spoofing bug in `X-Real-IP`, fixed in August
  2024 ([thread](https://station.railway.com/questions/edge-proxy-x-forwarded-for-and-x-real-ip-c5a50049)).
* The widely-repeated `100.0.0.0/8` figure comes from a **non-employee** comment
  and is contradicted in its own thread.

None of that is a stable, authoritative range, and a security control keyed on
interim forum guidance is not a security control. **Do not put a CIDR here that
you have not observed from this deployment.**

### What to do instead

**Deploy with both settings unset.** That is safe: an unbelieved header means
limits fall back to the socket peer. A misconfiguration under-trusts; it never
opens the header up.

The cost is real but bounded, and it is worth stating plainly: with every
caller resolving to the edge's address, all visitors share **one** bucket per
limit. For `DEMO_SESSION_LIMIT` — 5 per hour — that means five demo sessions
per hour for everyone, which is not enough for a portfolio link.

So the API measures the value for you. The first time a request arrives with a
forwarded chain the deployment is not configured to trust, it logs once:

```
Rate limits are keyed by the socket peer 10.x.x.x, but requests carry an
X-Forwarded-For chain of N hop(s), so every caller behind that proxy shares
one budget. If 10.x.x.x is a proxy you operate, set TRUST_PROXY_HEADERS=true
and TRUSTED_PROXY_IPS to its address or CIDR. Do not guess the range — this
is the address to use.
```

Only the peer and the hop count are recorded; the chain's contents are caller
addresses and never reach the log.

1. Deploy with both unset.
2. Load the site once, then read that line from the `api` service logs.
3. Set `TRUSTED_PROXY_IPS` to the address it names (or its enclosing /24 if it
   varies across deploys) and `TRUST_PROXY_HEADERS=true`.
4. Redeploy and confirm two different networks get independent demo budgets.

**If the observed address is not stable across deploys**, do not widen the
range until it covers whatever appears — a range broad enough to always match
is a range broad enough for someone else's container to match. Leave trust off,
accept the shared bucket, and raise `DEMO_SESSION_LIMIT` to a value that is
sane as a global budget. A global limit that holds is worth more than a
per-visitor limit that can be forged.

The chain is walked **right to left**, stopping at the first hop that is not a
configured proxy. Taking the leftmost entry is the bypass in the general case:
that value is whatever the caller sent. Railway's edge is claimed to rewrite
rather than append, which would make the leftmost entry trustworthy *there* —
but the right-to-left walk reaches the same address once the edge's own hops
are in the allow-list, and it stays correct if that claim ever stops holding.

The API runs uvicorn **without** `--proxy-headers`. Those flags make uvicorn
rewrite `scope["client"]` from the header before the application sees the
request, and with `--forwarded-allow-ips="*"` it does so for any peer — which
hands an attacker a fresh login budget per forged header. Forwarded addresses
are resolved in the application instead, where the allow-list is enforced and
unit-tested (`tests/test_ratelimit_security.py`).

## Scheduled jobs

Two sweeps, both idempotent and both safe to run against live traffic. Each is
a Railway service with a `cronSchedule`: the container starts on the schedule,
runs its command, and exits.

| Job | Command | Schedule | Config |
|---|---|---|---|
| Expired demo cleanup | `ledgerai-demo-cleanup` | `0 * * * *` (hourly) | `railway/cron-demo-cleanup.json` |
| Retention sweep | `ledgerai-retention-sweep` | `0 4 * * *` (daily) | `railway/cron-retention.json` |

**These are console scripts installed by the package**, so they resolve on
PATH inside the image. They are not `python scripts/...`: the image is built
from the `apps/api` context and copies only `ledgerai/`, `alembic/` and the
lock files, so the repository-root `scripts/` directory **does not exist in a
deployed container**. Invoking it there fails with "No such file or directory",
and it fails silently as far as the product is concerned — nothing surfaces the
fact that demo accounts have stopped being reaped until the database has been
growing for weeks. `tests/test_cli.py` and `tests/test_railway_config.py` pin
the entry points and the scheduled commands to each other.

`python -m ledgerai.cli demo-cleanup` is the equivalent for a one-off run from
a shell in the container. Both exit non-zero on failure, so a failed sweep is
recorded as a failed run rather than printing a traceback and reporting
success. `restartPolicyType` is `NEVER` so a failure waits for the next tick
instead of restart-looping.

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

Locally: `make demo-sweep` and `make sweep`. Those wrappers call the same
functions the console scripts do, so the local path cannot drift from the
deployed one.

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
  rows that reference objects that no longer exist. R2 does not version objects
  by default — enable object versioning on the bucket, or accept that restored
  receipts will have working metadata and missing images.
* **Redis needs no backup.** Losing it costs a cold cache and reset rate-limit
  windows.
* **Test the restore.** An untested backup is a hypothesis. Restore into a
  scratch database and run `alembic current` plus a row count.

---

## Cost and trial limits

Honest expectations for a portfolio demo, not a quote:

* Railway's hobby tier is usage-based. Five always-on services (web, api,
  worker, Postgres, Redis) each consume resources continuously; the worker and
  API are mostly idle but do not scale to zero while a healthcheck polls them.
  The two cron services run for seconds a day and cost effectively nothing.
* **The worker is the one to watch.** It holds a Postgres connection and polls
  Redis. It cannot be removed — uploads and OCR run there.
* **Storage is not the cost.** R2's free tier (10 GB stored, no egress charge)
  is far beyond what synthetic receipts need, and moving receipts off Railway
  removes the volume line entirely.
* Expect the demo to exceed a free allowance if left running continuously.
  Mitigations: keep `AI_ENABLED=false` so no per-token cost exists at all, and
  let the hourly demo cleanup keep the database small.

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

* Creating a Railway account and project, and provisioning Postgres and Redis.
* Creating a Cloudflare R2 bucket and a bucket-scoped API token, and setting
  the six `S3_*` variables from it.
* Setting every other environment variable above, including generated secrets.
* Setting each service's **Root Directory** and **config-as-code path** per the
  table in [Services](#services) — those two cannot live in the config files.
* Creating the GitHub OAuth application and its callback URL, if it is wanted.
  The app signs in and demos without one.
* Pushing this repository to a remote and connecting it to Railway.
* The first deploy, and the first `alembic upgrade head`.
* Reading the proxy-trust line out of the first deploy's logs and setting
  `TRUSTED_PROXY_IPS` from it — see [Proxy trust](#proxy-trust). Until that is
  done, rate limits are shared rather than per-visitor.
