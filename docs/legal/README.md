# Legal documents — DRAFTS REQUIRING REVIEW

> ## ⚠️ These are drafts. They have not been reviewed by a lawyer.
>
> They were written to describe what the software actually does, so that a
> qualified reviewer has something concrete to correct. **They are not legal
> advice and they do not assert compliance with any regime** — not GDPR, not
> CCPA, not any other. Whether those apply to Ledger AI's users, what lawful
> basis applies, whether Ledger AI is a controller or a processor, and whether
> data-processing agreements are needed with Clerk, Railway or Cloudflare are
> all questions for a lawyer and not for this repository.
>
> Do not present these to real users as final.

## Files

| File | Purpose |
|---|---|
| `terms.md` | Terms of Service |
| `privacy.md` | Privacy Notice |
| `financial-disclaimer.md` | Not-a-financial-adviser statement |
| `upload-consent.md` | The text shown before a first upload |

## Versioning

Each document has a version string, and the version a user accepted is recorded
in `user_consents`. The current required versions live in application settings
(`terms_version`, `privacy_version`, `upload_consent_version`); bumping one
re-prompts everybody, and the earlier acceptance is kept rather than
overwritten — "accepted the terms" is not a useful record without knowing which
terms.

## Points a reviewer should look at first

1. **Third-party processors.** Clerk holds user email addresses and
   authentication state. Railway hosts the database. Cloudflare R2 stores
   uploaded receipt images. All three need to be named, and the arrangements
   with them assessed.
2. **What "financial data" means here.** Users upload real bank statements.
   Whether that is a special category, and what that implies, needs a view.
3. **Retention.** Demo accounts are deleted after 24 hours. Persistent accounts
   are kept until deleted. There is no stated maximum retention.
4. **Deletion.** Deletion reaches Postgres, Redis, the job queue, R2 and Clerk.
   It is not instantaneous and is completed by a retrying background sweep, so
   the wording should not promise immediacy.
5. **Jurisdiction.** Not stated anywhere yet. Deliberately left for review
   rather than guessed at.
6. **Beta status.** No availability or durability guarantee is offered, and the
   drafts say so.
