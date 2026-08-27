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
| **Upload** | Drag-and-drop CSV import with live job progress through `queued → extracting → normalizing → categorizing → complete`. Re-uploading the same file creates zero duplicate transactions. |
| **Transactions** | Searchable, filterable table with inline correction, optimistic updates, confidence indicators, and a manual-review queue. A correction can be applied retroactively to every matching transaction — on by default, with the affected count shown before you confirm. |
| **Ask Ledger** | Chat-style analysis with streamed inspectable steps, a generated Recharts visualization, the supporting transactions, and a plain-language explanation. |
| **Settings** | Profile, an honest AI disclosure, and the real status of every feature — including the ones that aren't built. |

---

## Quick start

**Prerequisites:** Docker Desktop, Node 20+, and [uv](https://docs.astral.sh/uv/).

```bash
make setup      # install backend + frontend dependencies, create .env files
# Set AUTH_SECRET in .env AND apps/web/.env.local to the same value:
#   openssl rand -base64 32
make up         # Postgres :5433, Redis :6379, MinIO :9000/:9001
make migrate    # apply database migrations
make seed       # generate ~700 synthetic transactions over 14 months
make dev        # api :8000, rq worker, web :3000
```

Then open <http://localhost:3000> and sign in with the demo account:

```
demo@ledgerai.local / demo1234
```

> Compose binds Postgres to host port **5433**, not 5432, so it doesn't collide
> with a Postgres you may already be running locally.

### Try it

1. **Dashboard** — 14 months of synthetic spending with a seasonal utilities curve.
2. **Upload** — import `docs/samples/sample_statement_synthetic.csv`, watch the
   stages advance, then **upload the identical file again**: it reports
   "already processed" and imports nothing.
3. **Transactions** — filter to *Needs review* and recategorize a `Zorblax
   Quantum Widgets` row. Before you confirm, Ledger tells you how many other
   transactions from that merchant it will also change. Accept, and all of them
   update at once; the *next* file containing that merchant is categorized
   automatically too. Correct a single row with the option turned off, and a
   later bulk change will leave that row alone.
4. **Ask Ledger** — ask *"How much did I spend on groceries last month compared
   to the month before?"* Then open the **Running a structured aggregation**
   step and read the SQL that produced the number.

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
parsing, never a float. Negative is outflow, positive is inflow.

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
make test      # 165 backend tests + 40 frontend tests
make lint      # ruff, mypy, eslint, tsc --noEmit
```

The backend suite covers normalization and money parsing, filename/JWT/upload
security, the deterministic categorizer, the plan contract, date resolution,
the numeric guard, and HTTP-level integration tests against a real Postgres
test database — including that a second user cannot reach the first user's data
through any endpoint, and that a fabricated figure in a narration is caught.

---

## Roadmap

**Phase 1 — complete.** Local infrastructure, development authentication,
synthetic seed data, CSV upload with background processing and idempotency,
normalization, deterministic categorization, dashboard, editable transactions,
and the full Ask Ledger flow with SSE steps, a generated chart, supporting data
and an explanation.

**Phase 2.** Receipt upload with Tesseract OCR · confidence-based review
workflow · duplicate and unusual-charge detection (median + MAD, robust to the
outliers being hunted) · more question types · optional OpenAI planner,
categorizer and narrator behind the existing interfaces.

**Phase 3.** OAuth providers · real S3 · data export and deletion · production
Dockerfiles · GitHub Actions CI · deployment packaging and portfolio polish.

### Deliberately not built

**"Unused subscription" detection.** Transaction data proves only that a
subscription was *charged* — never that it went unused. Ledger AI reports
recurring charges and says so explicitly. Detecting genuinely unused
subscriptions would require product-usage or account-level data from each
provider, which is a future integration, not an inference we can make from a
bank statement.
