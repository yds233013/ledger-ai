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

## Receipt handling

| Control | Implementation |
|---|---|
| Serving | `GET /api/receipts/{id}/image` requires authentication and is owner-scoped (404 otherwise). No public or pre-signed URL exists. |
| Content type | Taken from a fixed allow-list, never echoed from the upload's claim. |
| Headers | `X-Content-Type-Options: nosniff`, `Cache-Control: private, no-store`, `Content-Disposition: inline` with a sanitized filename, `Referrer-Policy: no-referrer`, and a `default-src 'none'; sandbox` CSP. |
| PDFs | Rasterized server-side to PNG for preview, so a browser is never asked to render an untrusted PDF. |
| Decompression bombs | `Image.MAX_IMAGE_PIXELS` set explicitly; dimensions and pixel count checked before a full decode; PDFs capped at 5 pages. |
| EXIF | Images are re-encoded to grayscale PNG before OCR, which strips EXIF — receipt photos routinely carry GPS coordinates. |
| Logging | No raw OCR text, extracted financial fields, storage keys or tokens reach the logs. Receipt logging carries ids, page counts and status only, asserted by test. |

## Known limitations

- No rate limiting on login or analysis endpoints.
- No CSRF token on the API; it relies on bearer auth plus a CORS allowlist
  rather than cookies, so it is not CSRF-exposed, but a cookie-based deployment
  would need one.
- No audit log beyond `transaction_corrections`.
- Uploaded files are stored unencrypted at rest in MinIO. A production
  deployment would enable bucket encryption.
- Data export and deletion are not implemented (Phase 3). **Deleting a receipt
  and purging its stored original from object storage is part of that work** —
  in Phase 2 a receipt's file stays in storage for the life of the account.
- No FX conversion; mixed-currency totals are restricted and disclosed rather
  than converted.
