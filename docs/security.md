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

## Secrets

`.env` and `.env.local` are gitignored. Only `.env.example` files with
placeholders are committed. `AUTH_SECRET` must be identical in the repo-root
`.env` and `apps/web/.env.local`.

## Known limitations (Phase 1)

- No rate limiting on login or analysis endpoints.
- No CSRF token on the API; it relies on bearer auth plus a CORS allowlist
  rather than cookies, so it is not CSRF-exposed, but a cookie-based deployment
  would need one.
- No audit log beyond `transaction_corrections`.
- Uploaded files are stored unencrypted at rest in MinIO. A production
  deployment would enable bucket encryption.
- Data export and deletion are not implemented (Phase 3).
