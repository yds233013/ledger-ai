# AI usage and disclosure

## What runs in Phase 1

**Nothing.** No language model is configured and none is called.

- Questions are interpreted by `RulePlanner` — regex intent classification,
  hand-rolled relative-date resolution, and matching against the user's own
  category and merchant names.
- Categories come from correction memory, ~428 seeded merchant patterns, and
  description keywords.
- Explanations are written from fixed templates using only computed figures.

The app is fully demonstrable with no API key, no network, and no account.

## What Phase 2 adds

An OpenAI key switches on three optional components. **None of them computes a
number.**

| Component | What the model does | What it is sent |
|---|---|---|
| `LLMPlanner` | Proposes an `AnalysisPlan` via structured output | The plan JSON schema, today's date, and the user's distinct **category and merchant names** |
| `LLMCategorizer` | Suggests a category for an unknown merchant | **Merchant name strings only**, batched, cached per merchant so any merchant is sent at most once |
| `LLMNarrator` | Words the explanation | The plan and the **already-computed result rows** |

Never sent: raw uploaded files, file contents, account numbers or masks,
balances, dates, individual amounts, or full statements.

## The guarantees, and how they're enforced

**"No language model performed a calculation."** This is structural, not a
promise:

1. The executor (`services/analysis/executor.py`) is the only module that
   produces figures, and `tests/test_privacy.py` asserts it cannot even import
   an AI client.
2. The planner emits a validated struct, never SQL. There is no code path that
   executes model-authored SQL.
3. The narrator receives only the computed result and never touches the
   database — also asserted by test.
4. Every numeric token in a narration is matched against the result set before
   display. A figure that isn't there causes the narration to be discarded and
   the deterministic template used instead
   (`verify_numeric_claims`, tested with a deliberately fabricated figure).

**Fallback.** Any LLM failure — a rate limit, a timeout, a schema mismatch, a
hallucinated field — falls back to the deterministic path, and the step payload
records that it did.

## Where the user sees this

- An **AI badge** on every AI-touched output, showing "Deterministic engine"
  when no key is configured.
- A disclosure banner on Ask Ledger describing what actually produced the answer.
- The **understanding** step states whether a model was involved in interpreting
  the question; the **aggregate** step states that the figures came from
  Postgres; the **explain** step shows the numeric verification result.
- Settings → AI disclosure, reflecting live configuration rather than marketing.

## Not financial advice

Ledger AI reports on uploaded data. Questions asking what the user *should* do
are detected (`is_advice_request`) and answered with a scoped decline. A
disclaimer is present on every page of the app shell.

### On "unused subscriptions"

Ledger AI does **not** claim to detect unused subscriptions. Transaction data
proves only that a subscription was *charged*. The recurring-charges analysis
says so in its own caveat text. Identifying genuinely unused subscriptions would
require product-usage or account-level data from each provider — a future
integration, not an inference available from a bank statement.
