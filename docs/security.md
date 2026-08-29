# Security notes

## Data isolation

Every user-owned read is built from `services/scoping.py`, and the analysis
executor applies `Transaction.user_id == user_id` inside `_base_conditions`,
which every query path goes through. A route cannot forget the predicate
because no route writes it.

Access to another user's resource returns **404, not 403** — a 403 confirms the
row exists. `tests/test_api.py` asserts this for transactions, analysis runs and
upload jobs.

## Upload handling

| Control | Implementation |
|---|---|
| Size limit | Bytes counted while streaming in 64 KB chunks. `Content-Length` is client-supplied and never enforces the limit. |
| Type | Content sniffed with `filetype` first, extension second. A PNG named `statement.csv` is classified as an image; an executable is rejected. |
| CSV structure | Header and shape validated before anything is stored; the error message names the missing column. |
| Filename | `sanitize_filename` strips both separator styles, path traversal, and non-ASCII, cleaning stem and extension separately. The original name is stored as display data only. |
| Storage key | Generated as `users/{uuid}/uploads/{uuid}/{safe-name}` and re-validated against a strict regex on every read and write. The local backend additionally refuses any path that resolves outside its root. |

## Authentication

Next.js owns the browser session (httpOnly cookie) and mints a 15-minute HS256
bearer token per request burst; FastAPI verifies it with the shared
`AUTH_SECRET`, requiring `exp`, `sub`, `iss` and `aud`.

Login returns an identical response for an unknown email and a wrong password,
so the endpoint cannot be used to enumerate accounts. Passwords over bcrypt's
72-byte limit are **rejected rather than truncated**, so two different long
passwords can never authenticate each other.

HS256 with a shared secret is appropriate here because both services are ours
and co-deployed. Exposing the API to third-party clients would mean moving to
RS256/JWKS so the verifier no longer holds the signing key.

### Three ways in

| Provider | Credential | Available |
|---|---|---|
| Credentials | email + password, verified by the API | always |
| Demo | none — the server provisions an account | always |
| GitHub OAuth | GitHub account | only when `AUTH_GITHUB_ID`/`AUTH_GITHUB_SECRET` are set |

All three end at the same contract: an Auth.js session whose `user.id` is a
Ledger AI user id, from which `/api/auth/token` mints the bearer token.

### Demo accounts

"Try the demo" provisions a **real user row** — which is the whole security
argument. Every `user_id` predicate that already protects a real account
protects a demo visitor too, because there is no second, weaker path.

* The account is created server-side. The browser never receives an address or
  password for it, so a demo account cannot be re-entered, shared or guessed
  at later. Its password hash is random bytes no one holds the pre-image of.
* The address is `demo-<random>@demo.ledgerai.invalid` — a domain that cannot
  receive mail and cannot collide with a real sign-up.
* The documented development password is never reused for one, asserted by
  test.
* Provisioning takes an idempotency key, `UNIQUE` on `users`. A retried request
  returns the account the first attempt created; two concurrent requests with
  the same key collide on the index and one re-reads the winner.
* The user, accounts and every transaction are written in **one** transaction.
  A failure half-way leaves no user at all, rather than an empty shell someone
  then signs into.

### Demo expiry cannot be renewed

`demo_expires_at` lives on the **row**, and `get_current_user` checks it on
every single request.

Putting expiry in the token would not hold: the browser mints a fresh
short-lived token whenever the old one nears expiry, so a visitor who simply
kept the tab open would renew their way past the deadline forever. A column
cannot be renewed by refreshing. The token issued at provisioning is
additionally capped so it never outlives the account.

`demo_expires_at IS NOT NULL` is the single marker of an ephemeral account and
the only thing the cleanup sweep selects on. The permanent development demo
user is `is_demo` with that column NULL; a real account has neither. Neither
can be reached by the sweep, and `delete_demo_user` raises rather than proceed
if handed anything else.

### OAuth account linking

A GitHub identity resolves to a Ledger AI account by GitHub's **immutable
account id** and by nothing else.

An existing account is never adopted because its email matches the address
GitHub reported — **not even when GitHub says it verified that address**.
"Verified by the provider" means the provider believes the person controls that
mailbox; it says nothing about who owns the Ledger AI account already using it.
Merging on it would mean anyone able to set their GitHub address to a known
user's address inherits that user's financial data. A GitHub identity that has
not been seen before therefore always gets its own new account.

The reported address is stored only when GitHub verified it *and* no other
account holds it; otherwise a non-routable placeholder is used, so an
unverified or contested address never becomes an account identifier.

Authentication failures log a status code and nothing else. No token, callback
parameter, or provider payload reaches a log line or a response.

## Secrets

`.env` and `.env.local` are gitignored. Only `.env.example` files with
placeholders are committed. `AUTH_SECRET` must be identical in the repo-root
`.env` and `apps/web/.env.local`.

## Receipt handling

| Control | Implementation |
|---|---|
| Serving | `GET /api/receipts/{id}/image` requires authentication and is owner-scoped (404 otherwise). No public or pre-signed URL exists. |
| Content type | Taken from a fixed allow-list, never echoed from the upload's claim. |
| Headers | `X-Content-Type-Options: nosniff`, `Cache-Control: private, no-store`, `Content-Disposition: inline` with a sanitized filename, `Referrer-Policy: no-referrer`, and a `default-src 'none'; sandbox` CSP. |
| PDFs | Rasterized server-side to PNG for preview, so a browser is never asked to render an untrusted PDF. |
| Decompression bombs | `Image.MAX_IMAGE_PIXELS` set explicitly; dimensions and pixel count checked before a full decode; PDFs capped at 5 pages. |
| EXIF | Images are re-encoded to grayscale PNG before OCR, which strips EXIF — receipt photos routinely carry GPS coordinates. |
| Logging | No raw OCR text, extracted financial fields, storage keys or tokens reach the logs. Receipt logging carries ids, page counts and status only, asserted by test. A redaction filter scrubs bearer tokens, API keys, credentials in connection strings and `?search=` query strings as a second layer, and request access logging is off in production because this API's query strings carry merchant names. |

## Rate limiting

Redis fixed-window counters, applied per user where a user exists and per IP
for login (the thing being throttled there is credential guessing, and the
guesser chooses the account name). Budgets are named constants so a test can
assert the boundary: login 10/5min, uploads 30/hour, analyses 60/hour, exports
5/hour, destructive operations 5/hour.

### What happens when the store is down

"Fail open" and "fail closed" are each wrong in one of the two situations this
limiter runs in, so the policy is asymmetric and set per limit:

| Limit | Store unavailable, development | Store unavailable, production |
|---|---|---|
| login · demo session · upload · analysis | allow | **refuse, 503** |
| export · account deletion | allow | allow |

The top row is what an anonymous or cheaply-obtained caller can reach, and it is
exactly where an uncounted request is the thing being defended against. An
attacker who can knock the store over must not thereby win unlimited
credential-stuffing attempts. The bottom row is a signed-in user acting on their
own data; refusing those protects nobody and denies someone their own export
during an outage.

Development always allows, so a developer whose Redis is down can still work.

Refusals are a generic `503` with `Retry-After: 30` and the text *"Service
temporarily unavailable. Please try again shortly."* — the failing dependency is
not named, in the response or in the log line, because that is both an internal
detail and a hint about what to attack next.

`GET /health` reports `status: degraded`, `dependencies.rate_limit_store:
unavailable`, and whether the policy currently in force is `failing_closed` or
`failing_open`. It still returns **200**: an orchestrator that killed every
replica over a dependency outage would turn a degraded limiter into a total one.

`GET /health/ready` answers the different question of whether the instance
should still receive traffic, and returns **503** when a production instance
cannot enforce a limit it fails closed on. See `docs/deployment.md`.

### The counter and its expiry are set atomically

`INCR` followed by a separate `EXPIRE` is the classic broken limiter. Anything
that interrupts the process between the two — a client disconnect cancelling
the task, `SIGTERM` during a rolling deploy, a connection reset — leaves a
counter with **no TTL**. Nothing re-arms it, because the "first request" branch
never runs again, so the count climbs forever and whoever that key identifies
is locked out permanently. A brief Redis blip becomes a durable denial of
service against a legitimate user.

Both operations therefore run in a single Lua `EVAL`, which Redis executes
without interleaving another command. The script also repairs a key that has
already lost its TTL (`PTTL < 0`), so a counter stranded by an older build
heals on its next use instead of needing an operator to `DEL` it.

### Trusting a proxy, narrowly

Rate limits identify callers by IP, so behind a proxy the socket peer is the
proxy. Consulting `X-Forwarded-For` requires **both** of:

```
TRUST_PROXY_HEADERS=true
TRUSTED_PROXY_IPS=<an address observed from the deployment>
```

**The allow-list must be measured, not looked up.** No hosting provider used
here publishes a stable, authoritative CIDR for its inbound edge, and a range
copied from a forum answer fails in the silent direction — it keeps the limits
keyed on the proxy while looking configured. The API therefore logs the peer
address it actually sees, once, when it is ignoring a forwarded chain, and that
observed value is what the setting takes. The full procedure, and what to do
when the address is not stable, is in
[deployment.md](deployment.md#proxy-trust).

Until it is set, the limits are collective rather than per-visitor: every
caller behind the edge shares one budget. That is a deliberate trade — a
shared limit that holds beats a per-visitor limit keyed on a forgeable
header.

The flag alone does nothing. "Trust the header whenever it is present" is
precisely the bypass: an attacker rotates it and receives a fresh budget per
request. If the flag is on and the allow-list is empty or unparseable, no
forwarded address is believed — a misconfiguration under-trusts rather than
opening the header to everyone.

The chain is walked **right to left**, stopping at the first hop that is not a
configured proxy — the last address we can actually vouch for. Taking the
leftmost entry, the obvious reading, is the vulnerability: that value is
whatever the caller sent.

The API runs uvicorn **without** `--proxy-headers` and without
`--forwarded-allow-ips`. Those flags rewrite `request.client.host` from the
header before the application sees the request, and with `"*"` they do it for
any TCP peer — defeating the allow-list one layer below it. Resolution happens
in the application instead, where it is enforced and unit-tested.

## Data lifecycle

Deletion reaches four places, and the last two are the ones usually missed:

| Surface | How |
|---|---|
| PostgreSQL | One `DELETE`; every `users.id` foreign key is `ON DELETE CASCADE` |
| Object storage | `delete_prefix("users/<id>/")` |
| Redis | Cached analyses, found through a per-user key index kept for this purpose — the cache key is a digest and identifies nobody on its own |
| RQ | Pending jobs cancelled by their stored id, so no worker wakes to a vanished upload |

Both deletion endpoints require the literal string `DELETE` as confirmation and
offer a dry run that reports exactly what would be removed.

**Retention.** A sweep (`make sweep`) fails jobs stuck in a non-terminal stage
for over an hour — a worker killed outright never runs its own failure handler
— deletes stored files for uploads that failed more than 7 days ago while
keeping the row visible, and removes receipts never confirmed after 30 days.

## Production configuration

The API refuses to start with `ENVIRONMENT=production` if `AUTH_SECRET` is
missing, short, or still the development default, or if `DEMO_USER_PASSWORD` is
unchanged. A localhost CORS origin and local storage backend are logged as
advisories rather than treated as fatal, because both are legitimate when
verifying the production images locally.

Unhandled exceptions return a generic 500 with a correlation id; the traceback
goes to the logs only.

## Known limitations

- No rate limiting on login or analysis endpoints.
- No CSRF token on the API; it relies on bearer auth plus a CORS allowlist
  rather than cookies, so it is not CSRF-exposed, but a cookie-based deployment
  would need one.
- No audit log beyond `transaction_corrections`.
- Uploaded files are stored unencrypted at rest in object storage (MinIO
  locally, Cloudflare R2 in production). The provider encrypts the underlying
  disk; Ledger AI adds no envelope encryption of its own, so bucket credentials
  are enough to read a receipt. The bucket is private and no code path
  generates a public or presigned URL. A production
  deployment would enable bucket encryption.
- Data export and deletion are not implemented (Phase 3). **Deleting a receipt
  and purging its stored original from object storage is part of that work** —
  in Phase 2 a receipt's file stays in storage for the life of the account.
- No FX conversion; mixed-currency totals are restricted and disclosed rather
  than converted.


## Private beta authentication (Clerk)

The browser sends a Clerk session JWT straight to this API, so the API
establishes identity itself rather than trusting anything the browser asserts.

**Two token families that can never be interchanged.** Demo sessions use HS256
tokens this service mints; Clerk uses RS256 tokens signed by keys only ever seen
through JWKS. Dispatch happens on the *unverified* header — safe, because the
header selects a verifier and is never read as a claim about the caller — and
each verifier pins `algorithms` to exactly one value. There is no fallback: a
token that fails the path it was routed to is rejected, not retried against the
other. Both directions are tested, including an HS256 token forged with a `kid`
header and a token asserting `alg: none`.

**Claims enforced:** signature, `kid` against the issuer's JWKS, `iss`, `azp`
against the exact web origin, `exp`, `nbf` with bounded leeway, `sub` shape, and
`aud` when configured. A missing `azp` is a rejection — Clerk's documentation
calls skipping that check a CSRF exposure.

**Identity is the Clerk subject, never the email address.** Clerk lets a user
change their address, and an identifier the holder can edit is not an identity.
A partial unique index on `clerk_user_id` is what makes concurrent first
requests produce exactly one profile.

**Deleted identities stay deleted.** See the tombstone design in
[deployment.md](deployment.md). The failure it prevents — lazy provisioning
silently recreating an account from a still-valid pre-deletion token — is the
worst outcome this feature could produce.

**Invitation addresses are stored as keyed HMACs.** They must be matchable,
because provisioning finds the invitation from the verified email rather than
asking the user for a code. A plain hash of an email is enumerable; keying it is
what makes the column unsearchable without the key.
