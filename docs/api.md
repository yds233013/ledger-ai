# API

33 operations. The live, always-current reference is the OpenAPI document the
application generates:

* **`/docs`** — Swagger UI, publicly reachable so a reviewer can read the API
  without credentials.
* **`/openapi.json`** — the schema behind it.

`/docs` being public does **not** make any data public. Every financial
endpoint requires a bearer token and is scoped to the caller; a test enumerates
the protected operations *from the live OpenAPI document* and asserts each one
returns `401` without a token, so adding a route cannot quietly skip the check.

---

## Authentication

Next.js owns the browser session and mints a 15-minute HS256 bearer token from
it; FastAPI verifies with the shared `AUTH_SECRET`, requiring `exp`, `sub`,
`iss` and `aud`.

```
Authorization: Bearer <token>
```

`/api/auth/token` (on the **web** service) exchanges the session cookie for that
token. `POST /api/auth/token` (on the **API**) exchanges a still-valid token for
a fresh one.

---

## Public operations

Four, and each is public for a reason.

| Operation | Why it is public |
|---|---|
| `GET /health` | liveness probe |
| `GET /health/ready` | readiness probe |
| `POST /api/auth/login` | authentication itself |
| `POST /api/auth/demo-session` | the way in for a demo visitor |
| `POST /api/auth/oauth/github` | the OAuth callback's account resolution |

None of them returns a secret, a connection string or a hostname. A test asserts
that no configured secret appears in any of their responses.

---

## Operations

### Auth

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/auth/login` | Rate limited per IP. Identical response for an unknown email and a wrong password. |
| `POST` | `/api/auth/demo-session` | Provisions an isolated, expiring demo account. Optional `request_key` makes it idempotent. |
| `POST` | `/api/auth/oauth/github` | Resolves a GitHub identity by immutable account id. Never links on an email address. |
| `GET` | `/api/auth/me` | The caller's own profile. |
| `POST` | `/api/auth/token` | Refresh a still-valid token. |

### Transactions

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/transactions` | Filter by `search`, `start_date`, `end_date`, `account_id`, `category_slug`, `merchant`, `review`, **`flagged`**, `min_amount`, `max_amount`; sort and paginate. |
| `GET` | `/api/transactions/facets` | Filter vocabulary plus `review_count` and `flagged_count`. |
| `PATCH` | `/api/transactions/{id}` | Correct merchant and/or category; `apply_to_matching` makes it retroactive. |
| `GET` | `/api/transactions/{id}/correction-impact` | How many rows a retroactive correction would change. Writes nothing. |

**`flagged` and `review` are independent** and must not be conflated.
`review=needs_review` selects rows the *categorizer* was unsure about;
`flagged=true` selects rows carrying an **open alert**. A duplicated charge at a
known merchant is categorized at confidence 1.00 and appears only in the second.

### Uploads and receipts

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/uploads` | CSV or image. Identical bytes return `status: duplicate` and create nothing. |
| `GET` | `/api/uploads` | Upload history with job state. |
| `GET` | `/api/uploads/{id}/job` | Stage and progress for one job. |
| `GET` | `/api/receipts` | Review queue. |
| `GET` | `/api/receipts/{id}` | Extracted fields, per-field confidence, raw OCR text. |
| `PATCH` | `/api/receipts/{id}` | Correct extracted fields. Refused once confirmed. |
| `POST` | `/api/receipts/{id}/confirm` | `mode: "create" \| "link"`. |
| `GET` | `/api/receipts/{id}/match-candidates` | Scored candidates with their signals. |
| `POST` | `/api/receipts/{id}/reject-candidate` | Persisted, so it does not reappear. |
| `GET` | `/api/receipts/{id}/image` | Authenticated and owner-scoped. No public or pre-signed URL exists. |
| `DELETE` | `/api/receipts/{id}` | Removes the receipt and its file; a linked transaction is **kept**. |

### Analysis

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/analysis/runs` | **Server-sent events.** Streams `run`, one event per step, then the result. |
| `GET` | `/api/analysis/runs` | Recent questions. |
| `GET` | `/api/analysis/runs/{id}` | One run, with its stored plan, steps and result. |
| `GET` | `/api/analysis/capabilities` | Which planner and narrator are active, and the AI disclosure. |

The SSE stream emits `understand`, `select`, `aggregate`, `visualize`, `explain`
as each completes. Disconnecting mid-stream is safe: the run is abandoned and a
subsequent identical request runs cleanly (a cached one replays the stored steps
verbatim).

### Dashboard, alerts, settings

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/dashboard` | Totals, category breakdown, trend, recent rows, top alerts. `trend_months` says how many months are actually plotted. |
| `GET` | `/api/alerts` | Filter by status. Carries the standing "not fraud detection" disclaimer. |
| `PATCH` | `/api/alerts/{id}` | Dismiss or resolve. |
| `GET` | `/api/settings/profile` | Profile, capability status, AI disclosure, demo expiry. |
| `GET` | `/api/settings/consents` | Which documents are required, which are accepted, and at what version. |
| `POST` | `/api/settings/consents` | Record acceptance of one or more documents at their current version. |
| `GET` | `/api/settings/usage` | This account's own consumption against each private-beta budget. `applies: false` for demo accounts. |
| `GET` | `/api/settings/export` | Everything as a ZIP. Rate limited. |
| `POST` | `/api/settings/delete-data` | Keeps the sign-in and accounts. `dry_run` previews. |
| `POST` | `/api/settings/delete-account` | Removes everything. `dry_run` previews. |

Both deletion endpoints require `confirmation: "DELETE"` exactly, and both
report `rows_by_table`, `table_labels` and `retained` — what goes **and** what
stays.

---

## Errors

| Status | Meaning |
|---|---|
| `401` | Missing, invalid or expired token — including an **expired demo session**, whose message says so. |
| `404` | Not found *or not yours*. Never `403`: a 403 confirms the row exists. |
| `422` | Validation. The first useful message, not a nested error tree. Also an upload refused for carrying an unmasked identifier — `X-Rejected-Categories` names the categories found, never a row, column or value. |
| `429` | Rate limited, with `Retry-After`; or a durable quota is exhausted, with `X-Quota`, `X-Quota-Limit`, `X-Quota-Remaining` and `X-Quota-Reset`. |
| `500` | Generic message plus a `correlation_id` that ties it to the logged traceback. |
| `503` | A dependency needed to enforce a public rate limit is unavailable in production. |

No error response contains a traceback, a filesystem path, a dependency URL, an
internal hostname or a credential. Which dependency failed is an internal detail
and a hint about what to attack next, so it is not named.
