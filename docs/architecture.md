# Architecture

## Request paths

```
                      ┌──────────────────────────────┐
  Browser ────────────▶ Next.js (App Router)         │
     │                 │  · Auth.js session (cookie) │
     │                 │  · /api/auth/token mints a  │
     │                 │    15-min HS256 bearer      │
     │                 └──────────────────────────────┘
     │
     │  Authorization: Bearer <HS256>
     ▼
  ┌──────────────────────────────────────────────┐
  │ FastAPI                                      │
  │  deps.get_current_user  → CurrentUser        │──▶ Postgres (async, psycopg3)
  │  routers/                                    │
  │  services/analysis/  (SSE stream)            │──▶ Redis  (analysis cache)
  │  routers/uploads     (enqueue)               │──▶ Redis  (RQ queue) ──▶ worker
  └──────────────────────────────────────────────┘                          │
                                                      MinIO ◀───────────────┘
```

Two SQLAlchemy engines share one set of models: an **async** engine for the
FastAPI request path (so SSE streaming never blocks the event loop), and a
**sync** engine for the RQ worker, Alembic, and the seed scripts, which are
plain processes.

## The Ask Ledger pipeline

```
question
   │
   ▼
┌──────────────┐   AnalysisPlan (Pydantic, extra="forbid")
│ RulePlanner  │──────────────────────────────┐
│ (Phase 1)    │                              │
│ LLMPlanner   │  validation failure → falls  │
│ (Phase 2)    │  back to RulePlanner         │
└──────────────┘                              ▼
                                       ┌─────────────┐
                                       │  Executor   │ parameterized SQLAlchemy
                                       │             │ user_id predicate always applied
                                       └──────┬──────┘
                                              │ ExecutionResult (all numbers)
                              ┌───────────────┼───────────────┐
                              ▼               ▼               ▼
                        build_chart     build_narration   supporting rows
                              │               │
                              │        verify_numeric_claims
                              │        (fabricated figure → discard)
                              ▼               ▼
                          ChartSpec       narration
```

Each stage emits `started` then `completed` SSE frames, persisted to
`analysis_steps` so a cached replay renders exactly the same inspectable
process as a live run.

### Why the plan is a struct

An LLM that emits SQL can produce a query nobody reviews. An LLM that emits an
answer can produce a number nobody can check. An LLM that emits a small closed
struct can do neither: the struct is validated, and the struct is the *only*
thing the executor accepts.

`AnalysisPlan` is frozen, forbids unknown fields, and validates coherence
(a comparison requires `compare_to`; a breakdown requires `group_by`; a trend
must group by month or week). A hallucinated field fails loudly rather than
being silently ignored.

## Upload pipeline

```
POST /api/uploads
  ├─ read in 64 KB chunks, counting bytes (never trust Content-Length)
  ├─ sniff content type (a PNG named .csv is an image)
  ├─ validate CSV headers and shape
  ├─ sha256(bytes) → uploads.content_hash
  │    └─ UNIQUE(user_id, content_hash) → identical file returns the original
  ├─ sanitize filename → generate UUID storage key → MinIO
  ├─ INSERT uploads + processing_jobs
  └─ RQ enqueue(process_upload, on_failure=mark_job_failed)

worker:  extracting → normalizing → categorizing → complete
         each transition writes processing_jobs.stage/progress (polled by the UI)
         INSERT ... ON CONFLICT (dedupe_hash) DO NOTHING
```

`on_failure=mark_job_failed` runs in the worker process rather than the work
horse, so a job still surfaces as failed in the UI when the horse is killed
outright (OOM, SIGKILL, a macOS fork-safety abort) and never gets to run its
own `except` block.

## Categorization

Ordered stages, first confident hit wins:

| # | Stage | Confidence | Notes |
|---|---|---|---|
| 1 | Correction memory | 1.00 | Keyed on normalized merchant; a user edit teaches every later import |
| 2 | Merchant rules | 0.90 | ~428 seeded patterns, indexed longest-first so specific beats generic |
| 3 | Keyword heuristics | 0.65 | Words inside the description |
| 4 | Structural | 0.50 | Unmatched credit → income |
| 5 | Uncategorized | 0.00 | → manual review queue |

Below 0.60 sets `needs_review`. Phase 2 inserts the LLM categorizer between 3
and 4; nothing above it changes.

## Corrections

```
user picks a new category
        │
        ▼
GET /api/transactions/{id}/correction-impact     ← nothing is written
        │  matching_count / affected_count / protected_count / affected_ids
        ▼
confirmation row: "Apply to all matching transactions (N others)"  [x] on by default
        │
        ▼
PATCH /api/transactions/{id}  { category_id, apply_to_matching }
        ├─ target row updated, correction recorded with scope=individual|bulk
        ├─ if retroactive: every sibling sharing (user_id, merchant_key)
        │    minus rows carrying an INDIVIDUAL correction for this field
        └─ each sibling gets its own correction row (scope=bulk)
```

The preview and the write share `services/corrections.py::compute_impact`, so
the number the user approves is the number that actually changes. `affected_ids`
comes back with the preview, which is what lets the client update exactly the
right rows optimistically — and roll back to an exact snapshot if the write
fails.

Both paths select rows through `_siblings()`, where `user_id` is the first and
non-optional predicate. Two users with the same merchant name have the same
`merchant_key`, and neither can reach the other's rows.

## Caching

```
cache_key = sha256(user_id | normalized_question | max(updated_at) | row_count)
```

Folding the data watermark into the key means a correction invalidates every
cached answer for that user automatically. There is no manual cache-busting
anywhere in the codebase, and no way to serve a stale number after an edit.
