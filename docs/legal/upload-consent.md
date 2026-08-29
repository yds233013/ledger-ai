# Upload Consent — DRAFT, REQUIRES LEGAL REVIEW

**Version:** `2026-08-draft-1`

Shown before a beta account's first upload, and again whenever this version
changes. Demo accounts are exempt: their data is synthetic and the account
deletes itself within 24 hours.

## Text shown to the user

> **Before you upload financial data**
>
> You are about to upload a real financial document. Please read this once.
>
> - Your file is stored in Cloudflare R2 and its contents are written to a
>   database hosted on Railway. Both are third parties.
> - Files are stored **unencrypted at rest** beyond the encryption those
>   providers apply to their own disks. Ledger AI adds no encryption of its own,
>   so anyone with access to that storage could read your file.
> - This is a **beta**. There is no guarantee of availability or durability.
>   Keep your own copy of anything that matters.
> - Ledger AI is **not a financial adviser** and every figure is computed only
>   from what you upload.
> - You can export everything, or delete all of it, at any time from Settings.
>
> **Please remove or mask** full account numbers, Social Security numbers and
> similar identifiers before uploading. Ledger AI tries to detect and refuse
> files containing them, but that check is best-effort and is not a guarantee.
> Masked endings such as `••••4821` are fine and are expected in statements.
>
> ☐ I understand, and I have the right to upload this data.

## Notes for the reviewer

The checkbox records an acceptance row with the version above. It gates
uploading only — reading, exporting and deleting your own data are never
blocked on consent, because withholding somebody's own records until they
accept a document would be leverage rather than consent.
