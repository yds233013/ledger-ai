# Ledger AI

An AI-powered personal finance data workspace. Upload synthetic CSV bank
statements, get them normalized and categorized, correct anything that's wrong,
and ask questions in plain English — with the entire analysis process visible
and inspectable.

> **Demo project.** Every figure in this repository is synthetic and generated
> by `scripts/seed_synthetic.py`. Ledger AI does not connect to any real
> financial institution and does not provide financial advice.

---

## The idea

Most "AI finance app" demos hand a prompt to a language model and print whatever
comes back. You cannot check the number, and often neither can the model.

Ledger AI is built so that cannot happen:

- **A question becomes a validated plan, not SQL and not an answer.** The planner
  emits an `AnalysisPlan` — a small, closed, Pydantic-validated struct
  (`apps/api/ledgerai/services/analysis/plan.py`). Anything outside that
  vocabulary fails validation loudly.
- **The plan is compiled into a parameterized SQL aggregate.** Every number the
  user sees is computed by Postgres (`services/analysis/executor.py`). No
  model-authored SQL is ever executed; there is no code path that could.
- **The explanation is checked before it is shown.** Every numeric token in the
  narration is matched against the computed result set. A figure that isn't in
  the result set causes the narration to be discarded
  (`services/analysis/narrate.py`).
- **The whole process is streamed and openable.** Five steps — understanding,
  selecting, aggregating, visualizing, explaining — arrive over SSE as they
  complete, each with an expandable payload containing the resolved plan, the
  filters, the actual SQL, the result rows, and the chart spec.

Phase 1 ships with **no language model at all**: a deterministic rules engine
plans the question, seeded merchant patterns and your own corrections do the
categorization, and template narration writes the prose. Adding an API key in
Phase 2 changes who *proposes* the plan and who *words* the answer — never who
does the arithmetic.

---

## Screens

| Page | What it does |
|---|---|
| **Dashboard** | Total spend, month-over-month change, spending by category, a 12-month trend, and recent transactions. Transfers and card payments are excluded so money moved between your own accounts isn't counted as spending. |
| **Upload** | Drag-and-drop CSV or receipt import with live job progress through `queued → extracting → normalizing → categorizing → analyzing → complete`. Re-uploading the same file creates zero duplicate transactions. |
| **Receipts** | JPEG, PNG and PDF receipts read locally with Tesseract. Original image beside editable fields with per-field confidence, then create a transaction or link the receipt to one you already imported. |
| **Transactions** | Searchable, filterable table with inline correction, optimistic updates, confidence indicators, and a manual-review queue. A correction can be applied retroactively to every matching transaction — on by default, with the affected count shown before you confirm. |
| **Ask Ledger** | Chat-style analysis with streamed inspectable steps, a generated Recharts visualization, the supporting transactions, and a plain-language explanation. |
| **Settings** | Profile, an honest AI disclosure, and the real status of every feature — including the ones that aren't built. |

The dashboard also carries a live **alerts** surface, grouped by how much
attention each item deserves:

| Band | Contains | Framing |
|---|---|---|
| **Worth reviewing** | exact and near duplicates | the only class that suggests an action — you may have been charged twice |
| **Unusual for you** | unusual amounts, charges large for a merchant | larger than your own history suggests, often perfectly normal |
| **For information** | first-time merchants | noted in passing, nothing to act on |

Every alert opens to show the statistics that produced it, and none of them is a
fraud claim.

---

## Quick start

### Prerequisites

| Requirement | Why |
|---|---|
| **Docker Desktop** (running) | Postgres, Redis and MinIO |
| **Node 20+** | the Next.js frontend |
| **Python 3.12** + [uv](https://docs.astral.sh/uv/) | the FastAPI backend |
| **Tesseract 5** | receipt OCR — `brew install tesseract` (macOS) or `apt install tesseract-ocr` |

PDF receipts need **no** extra system package: `pypdfium2` ships its own
rasterizer, so poppler is not required.

### Steps

```bash
git clone <this repo> && cd LedgerAI

make setup                     # deps for both apps, plus .env files from the examples

# 1. Set the SAME secret in BOTH files — the API verifies the token the web app mints:
#      .env                 AUTH_SECRET=...
#      apps/web/.env.local  AUTH_SECRET=...
openssl rand -base64 32        # paste the output into both

make up                        # Postgres :5433, Redis :6379, MinIO :9000 (console :9001)
make migrate                   # apply all four migrations
make seed                      # ~700 synthetic transactions, 14 months, plus alerts
make dev                       # api :8000 + rq worker + web :3000, Ctrl-C stops all three
```

Open <http://localhost:3000> and sign in:

```
demo@ledgerai.local / demo1234
```

> **Ports.** Compose binds Postgres to host **5433**, not 5432, so it does not
> collide with a Postgres you may already be running.

> **macOS worker note.** `make dev-worker` sets
> `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`. macOS aborts Objective-C runtime
> initialisation inside a forked child and RQ forks a work horse per job; the
> variable is a harmless no-op on Linux and in containers.

### Optional: enable the AI features

Everything above works with **no API key**. To switch on the optional planner,
categorizer and narrator, set both in `.env`:

```bash
AI_ENABLED=true
OPENAI_API_KEY=sk-...
```

With the flag off or the key blank, `get_ai_client()` returns `None` and no AI
component is ever constructed. The test suite never needs a key.

### Useful commands

```bash
make test      # 345 backend + 102 frontend tests
make lint      # ruff, mypy, eslint, tsc --noEmit
make reset     # drop all data and reseed from scratch
make sample    # regenerate docs/samples/ CSV and receipts
make down      # stop the containers (volumes are preserved)
```

### Try it

1. **Dashboard** — 14 months of synthetic spending with a seasonal utilities curve.
2. **Upload** — import `docs/samples/sample_statement_synthetic.csv`, watch the
   stages advance, then **upload the identical file again**: it reports
   "already processed" and imports nothing.
3. **Receipts** — upload `docs/samples/receipts/receipt_grocers_synthetic.png` and the
   deliberately-degraded `receipt_faded_synthetic.png`. The clean one reads cleanly; the
   faded one lands in review with every money field marked *not found*, so you can type
   the values from the image and confirm. Upload the cafe receipt after importing a
   statement containing the same charge and Ledger offers to **link** it instead of
   creating a duplicate.
4. **Transactions** — filter to *Needs review* and recategorize a `Zorblax
   Quantum Widgets` row. Before you confirm, Ledger tells you how many other
   transactions from that merchant it will also change. Accept, and all of them
   update at once; the *next* file containing that merchant is categorized
   automatically too. Correct a single row with the option turned off, and a
   later bulk change will leave that row alone.
5. **Ask Ledger** — ask *"How much did I spend on groceries last month compared
   to the month before?"* Open the **Running a structured aggregation** step and read
   the SQL that produced the number, then use a **follow-up chip** to regroup the same
   plan by merchant.

---

## Architecture

```
Browser ──bearer token──▶ FastAPI ──▶ Postgres
   │                         │
   │                         ├──▶ Redis ──▶ RQ worker (upload pipeline)
   │                         └──▶ MinIO (uploaded files)
   └── Next.js (Auth.js session, mints short-lived API tokens)
```

```
apps/
├─ api/                     FastAPI + SQLAlchemy 2.0 + Alembic + RQ
│  └─ ledgerai/
│     ├─ models/            11 tables, integer-cent money, DB-enforced idempotency
│     ├─ routers/           auth · uploads · transactions · dashboard · analysis · settings
│     ├─ security/          JWT, filename sanitization, upload validation
│     ├─ jobs/              RQ queue and the upload pipeline
│     └─ services/
│        ├─ categorize/     deterministic engine + the Phase 2 LLM interface
│        └─ analysis/       plan · planner · executor · charts · narrate · runner
└─ web/                     Next.js 15 App Router + TanStack Query + Zustand + Recharts
```

### Decisions worth explaining

**RQ, not Celery.** Redis is already required for analysis caching, the pipeline
is a single linear function rather than a routing graph, and RQ's `job.meta` maps
one-to-one onto the stage/progress model the upload UI renders. Celery's
strengths — multi-broker transport, chords, complex routing — buy nothing here
and cost a large configuration surface. The trade-off is real and accepted: RQ
is Redis-only and has no built-in scheduler.

**Auth.js, not Clerk.** Next.js owns the browser session and mints a 15-minute
HS256 token from it; FastAPI verifies with the shared `AUTH_SECRET`. Clerk would
give hosted UI and MFA for free, but anyone cloning this repo would need a Clerk
account or the demo is dead, and the build must run offline. HS256 is
appropriate because both services are ours and co-deployed; RS256/JWKS is the
right upgrade the moment third-party clients exist.

**Money is integer cents, everywhere.** `amount_cents BIGINT`, `Decimal` for
parsing, never a float. Negative is outflow, positive is inflow — including a
confirmed receipt, whose total becomes a **negative** transaction so it
increases spending rather than reducing it.

**Currencies are never added together.** FX conversion is out of scope, so every
aggregate is restricted to the user's base currency by a predicate that lives in
the same shared builder as the user-id check, and anything excluded is named:
*"1 in EUR not included — Ledger AI does not convert between currencies."* A
non-base-currency receipt warns before it can be confirmed.

**Alerts are observations, not accusations.** Unusual-amount detection uses the
median and median absolute deviation rather than mean and standard deviation,
because the outlier being hunted inflates a standard deviation and hides itself.
An amount that is normal *for its own merchant* is never reported as unusual —
without that rule a $59.99 subscription is flagged every month simply for costing
more than the typical $15.99 one. Every alert carries the statistics that produced
it and a standing note that this is not fraud detection.

**Follow-ups carry no hidden state.** A follow-up is a named, deterministic
transform of the validated plan that produced the current answer, identified by
`(run_id, refinement_key)`. There is no pronoun resolution and no conversation
history: the refined plan is re-validated and displayed exactly as a fresh
question would be.

**Corrections are retroactive by default, but never destructive.** Applying a
correction to "all matching transactions" updates every row sharing the same
normalized merchant — except rows the user previously corrected one at a time,
which are protected and reported in the confirmation. The count shown before
confirming is produced by the same code that performs the write, so the number
you approve is the number that happens.

**Idempotency is a database constraint, not an application check.**
`uploads(user_id, content_hash)` is unique, so identical bytes can only be
ingested once. `transactions.dedupe_hash` is unique and inserts use
`ON CONFLICT DO NOTHING`, so a retried or partially-failed job converges instead
of duplicating spend. Two identical coffees on the same day survive as two rows,
because the source row index is part of the hash.

**Chart colour follows the data's job.** Spending-by-category, the trend, and
period comparisons are all a *single measure* — the axis labels already carry
identity, so those charts use one colour rather than burning a categorical
palette on redundant encoding. Where colour must carry identity (a pie), the
palette is a validated 7-slot categorical order capped with an "Other" fold;
beyond seven, hues stop being distinguishable.

---

## Security and privacy

| Concern | How it's handled |
|---|---|
| Data isolation | Every user-owned query goes through a shared predicate builder; a route cannot forget it. Cross-user access returns **404**, never 403 — no existence leak. Tested explicitly. |
| Upload validation | Content sniffing beats the extension (a PNG named `.csv` is treated as an image), 10 MB cap enforced by counting streamed bytes rather than trusting `Content-Length`, CSV header and shape validation. |
| Filenames | An uploaded name is display data only. Storage keys are generated from UUIDs and validated against a strict pattern; the user's name never participates in a path. |
| Secrets | `.env` is gitignored; only `.env.example` files with placeholders are committed. |
| AI data minimization | The categorizer may send **merchant name strings only**; the planner gets the schema plus category/merchant *names*; the narrator gets already-computed rows. Raw files never leave the system. Enforced by tests that assert the executor cannot even import an AI client. |
| Synthetic data | All demo data is generated from a fixed seed. Descriptions carry a `[SYNTHETIC]` marker and accounts are named `SANDBOX — …`. |
| Not financial advice | Advice-seeking questions are detected and declined with a scoped message; a disclaimer is present on every page. |

---

## Testing

```bash
make test      # 343 backend tests + 102 frontend tests
make lint      # ruff, mypy, eslint, tsc --noEmit
```

The backend suite covers normalization and money parsing, filename/JWT/upload
security, the deterministic categorizer, the plan contract, date resolution,
the numeric guard, and HTTP-level integration tests against a real Postgres
test database — including that a second user cannot reach the first user's data
through any endpoint, and that a fabricated figure in a narration is caught.

Phase 2 adds: OCR parsing against synthetic fixtures through a **fake engine**
(so no Tesseract binary is needed), alert thresholds asserted exactly at their
boundaries, SSE cancellation at every step and stream-failure containment,
receipt idempotency and non-destructive linking, mixed-currency behaviour, and
AI fallback for timeout, rate limit, malformed JSON, schema violation and
fabricated figures — every one with an injected fake client, so **the suite never
needs an API key and never touches the network**.

---

## Roadmap

**Phase 1 — complete.** Local infrastructure, development authentication,
synthetic seed data, CSV upload with background processing and idempotency,
normalization, deterministic categorization, dashboard, editable transactions,
and the full Ask Ledger flow with SSE steps, a generated chart, supporting data
and an explanation.

**Phase 2 — complete.** Receipt OCR for JPEG, PNG and PDF with confidence-scored
fields and a manual-review workspace · create-or-link confirmation so a receipt
never silently double-counts a statement charge · duplicate, near-duplicate,
unusual-amount, new-merchant and large-for-merchant detection · currency
correctness across the dashboard and Ask Ledger · explicit plan-refinement
follow-ups · optional OpenAI planner, categorizer and narrator behind
`AI_ENABLED`, every response Pydantic-validated with deterministic fallback.

**Phase 3.** OAuth providers · real S3 · data export and deletion · production
Dockerfiles · GitHub Actions CI · deployment packaging and portfolio polish.

### Known limitations

- **OCR is English-only** (`eng` traineddata) and handwritten receipts are out of
  scope.
- **No FX conversion.** Non-base-currency amounts are reported separately and
  never summed into a base-currency total.
- **Receipt deletion and storage cleanup are Phase 3.** Deleting a receipt and
  purging its stored original from object storage is not implemented.
- Alerts are statistical observations about your own uploaded data, not fraud
  detection.

### Deliberately not built

**"Unused subscription" detection.** Transaction data proves only that a
subscription was *charged* — never that it went unused. Ledger AI reports
recurring charges and says so explicitly. Detecting genuinely unused
subscriptions would require product-usage or account-level data from each
provider, which is a future integration, not an inference we can make from a
bank statement.
