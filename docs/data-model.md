# Data model

Thirteen tables. Every user-owned table carries `user_id` with an index, and every
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
`id` · `email` (unique) · `password_hash` (bcrypt) · `display_name` · `is_demo` ·
`base_currency` · `demo_expires_at` · `demo_request_key` (unique) ·
`github_id` (unique).

`base_currency` is what every aggregate is restricted to. Ledger AI does not
convert between currencies, so a total that mixed them would be meaningless.

Three columns carry the account's provenance, and the distinctions between them
are load-bearing:

| Column | Meaning |
|---|---|
| `is_demo` | holds synthetic data — true for the permanent local development account **and** for ephemeral per-visitor demos |
| `demo_expires_at` | set **only** on an ephemeral demo. NULL means "not ephemeral". |
| `demo_request_key` | provisioning idempotency key. UNIQUE, so two concurrent requests with the same key cannot both create an account. |
| `github_id` | GitHub's immutable account id, when the account signs in with GitHub. UNIQUE, and the only key an OAuth identity is resolved by. |

`demo_expires_at IS NOT NULL` is the single discriminator the cleanup sweep
selects on. A real account has neither it nor `is_demo`; the permanent
development demo user has `is_demo` with this column NULL. Neither can be
reached by the sweep — see `docs/security.md`.

`demo_request_key` and `github_id` are NULL for most rows. Postgres treats NULLs
as distinct in a UNIQUE constraint, so neither index serialises ordinary
sign-ups.

### `accounts`
Synthetic bank/card accounts. `user_id` · `name` · `institution` ·
`account_type` · `mask` · `currency` · `is_synthetic`. Unique on `(user_id, name)`.

`is_synthetic` marks the `Cash / Receipt Purchases` holding account. A
receipt-created transaction is never silently attached to a real bank account,
so the fallback destination has to be visibly distinct.

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

### `receipts`
One row per uploaded receipt. `upload_id` is **UNIQUE**, so a retried job cannot
produce a second receipt for the same file.

`status` (`pending` / `needs_review` / `confirmed` / `failed`) ·
`transaction_id` (set on confirm) · `link_mode` (`created` / `linked`) ·
`page_count` · `ocr_engine` · `ocr_confidence` · `raw_text` ·
`merchant` · `posted_date` · `subtotal_cents` · `tax_cents` · `tip_cents` ·
`total_cents` · `currency` · `field_confidence JSONB` · `parse_notes JSONB`.

**Extracted money stays positive here.** The receipt keeps what was printed on
it; the outflow sign is applied once, when the transaction is created. A $30.36
receipt produces a transaction with `amount_cents = -3036`.

`field_confidence` is JSONB rather than a column per field, so adding a field
later is not a migration.

### `receipt_match_rejections`
`receipt_id` · `transaction_id` · `user_id`, unique on the first two. A
suggestion the user dismissed does not come back — persisted rather than held in
session state so it survives a reload.

### `alerts`
`alert_type` (duplicate | near_duplicate | unusual_amount | new_merchant |
large_for_merchant) · `severity` · `message` · `evidence JSONB` · `status`.
Unique on `(transaction_id, alert_type)`, which makes re-running detection free.

`severity` doubles as the presentation contract: `high` for both duplicate
kinds, `medium` for unusual amounts and merchant outliers, `low` for first-time
merchants. `evidence` carries the median, MAD, sample size and threshold that
produced the alert, so it can be audited rather than believed.

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
