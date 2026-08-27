# Data model

Eleven tables. Every user-owned table carries `user_id` with an index, and every
read goes through `services/scoping.py` so the ownership predicate lives in one
place.

## Conventions

- **Money is integer cents** (`BigInteger`). Never a float, anywhere.
  Negative = outflow/spend, positive = inflow/credit.
- **Enums are VARCHAR**, not native Postgres ENUMs — adding a value later is a
  code change, not a migration that locks the table.
- **Idempotency is enforced by constraints**, not by read-then-write checks.

## Tables

### `users`
`id` · `email` (unique) · `password_hash` (bcrypt) · `display_name` · `is_demo`

### `accounts`
Synthetic bank/card accounts. `user_id` · `name` · `institution` ·
`account_type` · `mask` · `currency`. Unique on `(user_id, name)`.

### `categories`
System categories have `user_id IS NULL`; user categories are scoped.

Two uniqueness rules, because Postgres treats NULLs as distinct in a UNIQUE
constraint: `UNIQUE(user_id, slug)` for user categories, plus a **partial unique
index** on `(slug) WHERE user_id IS NULL` for system ones. Without the partial
index, re-running the seed silently duplicates every system category.

### `uploads`
`filename` (sanitized) · `original_filename` (display only) · `content_hash` ·
`kind` · `content_type` · `size_bytes` · `storage_key` · `status`.

**`UNIQUE(user_id, content_hash)`** — file-level idempotency.

### `processing_jobs`
`upload_id` · `rq_job_id` · `stage` · `progress` · `error_message` ·
`rows_total` / `rows_imported` / `rows_skipped` · timestamps.

`stage` is the contract with the UI: `queued → extracting → normalizing →
categorizing → complete | failed`.

### `transactions`
`posted_date` · `amount_cents` · `currency` · `raw_description` ·
`normalized_description` · `merchant` · `merchant_key` · `category_id` ·
`confidence` · `categorized_by` · `needs_review` · `is_corrected` ·
`dedupe_hash` · `source_row_index`.

`merchant_key` is the normalized merchant, stored rather than computed, so
"apply this correction to all matching transactions" is an exact indexed
predicate on `(user_id, merchant_key)` instead of a `LIKE` over a display
string. Renaming a merchant updates the key too, so later corrections group
correctly.

**`dedupe_hash` is UNIQUE** —
`sha256(user_id, account_id, posted_date, amount_cents, normalized_description, source_row_index)`.

`source_row_index` is in the hash deliberately: a statement can legitimately
contain the same charge twice on the same day, and those are different rows of
the same file. Reprocessing that file produces the same indices, so retries stay
idempotent while genuine repeats survive.

Indexes: `(user_id, posted_date)`, `(user_id, category_id)`,
`(user_id, needs_review)`, `(user_id, merchant)`, `(user_id, merchant_key)`.

### `transaction_corrections`
Audit trail *and* the highest-priority categorization signal.
`field` (merchant | category) · `old_value` · `new_value` · `merchant_key` ·
`scope`.

`merchant_key` is the normalized merchant at time of correction — the lookup key
for the correction-memory stage.

`scope` is `individual` or `bulk`, and it is what makes retroactive corrections
safe. A row carrying an **individual** correction is excluded from every later
"apply to all matching" for that field: if the user deliberately set one
transaction to something different, a subsequent bulk change must not silently
undo that decision.

### `alerts` *(Phase 2)*
`alert_type` (duplicate | near_duplicate | unusual_amount | new_merchant) ·
`severity` · `message` · `evidence JSONB` · `status`.
Unique on `(transaction_id, alert_type)`.

### `analysis_runs`
`question` · `normalized_question` · `plan JSONB` · `planner` · `narrator` ·
`status` · `result JSONB` · `chart_spec JSONB` · `narration` · `cache_key` ·
`duration_ms`.

Persisting `plan`, `result` and `chart_spec` is what lets a cached answer replay
the identical inspectable process instead of showing a different UI shape.

### `analysis_steps`
`run_id` · `seq` · `step` · `status` · `title` · `payload JSONB` ·
`duration_ms`. Unique on `(run_id, seq)`.

`payload` is the body the UI expands: the resolved plan, the filter summary, the
aggregation description plus result rows, the chart spec, or the narration
verification.

### `merchant_rules`
Seeded `pattern` → `category_slug` mapping with a `priority` derived from
pattern length, so the most specific pattern wins.
