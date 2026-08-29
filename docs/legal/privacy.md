# Privacy Notice — DRAFT, REQUIRES LEGAL REVIEW

**Version:** `2026-08-draft-1`
**Status:** Draft. Not reviewed by a lawyer. **Makes no compliance claim.**

This describes what the software actually does with data, so that a qualified
reviewer can correct it. It does not assert that these practices satisfy GDPR,
CCPA or any other regime.

## What is collected

| Data | Where it comes from | Why |
|---|---|---|
| Email address | Clerk, when you sign in | Identifying your account |
| Display name | Derived from your email | Showing you who is signed in |
| Uploaded statement files | You | Producing your transactions |
| Uploaded receipt images | You | Reading amounts and merchants via OCR |
| Transactions, categories, alerts | Derived from your uploads | The product itself |
| Consent records | Your acceptance | Recording which documents you agreed to |
| Operational logs | Automatic | Diagnosing failures |

**Not collected:** payment details, government identifiers, bank credentials.
Ledger AI does not connect to any financial institution.

## Where it is held

| Processor | What they hold | Where |
|---|---|---|
| **Clerk** | Email address, authentication state | Clerk's infrastructure |
| **Railway** | The database (transactions, categories, alerts) | Railway's infrastructure |
| **Cloudflare R2** | Uploaded receipt and statement files | Cloudflare's infrastructure |

*A reviewer should assess what arrangements are required with each.*

## What is deliberately kept out of logs

Operational logs record identifiers, counts and timings. They do **not** record
merchant names, amounts, uploaded file contents, OCR text, authentication tokens
or full public IP addresses. Where an address must be recorded for
infrastructure diagnosis, a visitor's address is reduced to a salted digest that
cannot be reversed.

## How long it is kept

* **Demo accounts** — deleted automatically 24 hours after creation, together
  with everything in them.
* **Beta accounts** — kept until you delete them. No maximum retention is
  currently defined. *Requires review.*
* **Failed uploads** — the stored file is removed after 7 days.
* **Unconfirmed receipts** — removed after 30 days.

## Deletion

Deleting your account removes your records from the database, your files from
object storage, your cached analyses and queued jobs from Redis, and your
identity from Clerk.

Deletion is **not instantaneous**. It is recorded immediately and your access
ends immediately, but the removal across those systems is completed by a
background process that retries until it succeeds. A record that the identity
was deleted is retained — containing no personal data — so that the account
cannot be silently recreated.

## Your choices

You can export everything at any time, delete your data while keeping your
sign-in, or delete the account entirely, all from Settings.

*What rights apply beyond these, and to whom, requires review.*

## Contact

*Not stated. Requires review.*
