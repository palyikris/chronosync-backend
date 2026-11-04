# Invoice multiplier E2E validation — 30 October 2026

The deployed entry-multiplier implementation correctly generated the tested invoice attachments and four Számlázz.hu test invoices. One attachment-finalization regression was reproduced, fixed, applied to hosted Supabase with approval, and verified through the actual REST API.

## Environment and authorization

- Deployed backend: `eb040dd68e9b31e2ae6cceacd4db712cfce845b9`, Railway deployment `d6e452b5-ed8d-415b-870b-e151376fdd29`.
- Hosted Supabase: `kqihfxhewjgrwoepdsau`; both entry-multiplier migrations were present before testing.
- All hosted fixture writes were restricted to the existing synthetic company `d2026102-7000-4000-8000-000000000001`, **ChronoSync staging E2E 2026-10-27**.
- The user reconfirmed **Teszt cég** was visibly in **TESZTÜZEM**, approved temporary staging-admin/worker activation and fixture creation, and approved the focused hosted database fix.
- The saved Agent key was retrieved from the OS keyring into a child process. No key, password, JWT or encrypted credential is included in the evidence. Existing company credentials were preserved.
- Every invoice and preview explicitly disabled email. These checks do not establish customer-email delivery or NAV reporting.

## Actual provider invoices

| Case | Invoice | Logged hours | Attachment hours | Invoiced quantities | Net | VAT | Gross |
| --- | --- | ---: | ---: | --- | ---: | ---: | ---: |
| Mixed HUF | TESZT-2026-7 | 5.00 | 26.50 | Payroll 21.25 hours; consulting 0.75 hour; monthly fee 1 | 4,778 HUF | 1,291 HUF | 6,069 HUF |
| Mixed EUR | TESZT-2026-8 | 2.00 | 19.31 | Payroll 15.96 hours; fixed units 2 | 323.58 EUR | 87.37 EUR | 410.95 EUR |
| Private HUF rounding boundary | TESZT-2026-9 | 0.50 | 1.00 | Payroll 1 hour at net unit price 1.5 HUF | 2 HUF | 0 HUF | 2 HUF |
| AAM exemption | TESZT-2026-10 | 0.50 | 2.82 | Payroll 2.82 hours | 31 HUF | 0 HUF | 31 HUF |

The HUF payroll line uses `1.25 × 3 + 2.50 × 7 = 21.25` billable hours. The attachment also includes consulting `0.75 × 1` and monthly-project work `0.50 × 9`, totaling 26.50 hours. The monthly invoice quantity remains 1, independent of the time-entry multiplier.

The EUR payroll line uses `1.33 × 12 = 15.96` hours at 19.955 EUR. Its other project has `0.67 × 5 = 3.35` attachment hours and an explicit invoice quantity of 2 units. The multiplier therefore affects the hourly invoice line and attachment time, while the fixed-unit line uses its explicit quantity.

For every case, the hosted review, stored ledger, invoice quantities, provider-confirmed totals, stored PDF and a subsequent provider PDF lookup agreed. All captured work became invoiced, raw logged hours remained unchanged, and repeat issuance was blocked with 404 or 409. Exactly four provider creation requests succeeded. There were no uncertain or issuing attempts left over.

## Verification coverage

- Four actual provider previews, followed by four invoices through the deployed review/reservation/issuance/finalization path.
- Four separate attachments and a combined workbook with four localized sheets. Every detail value and summary formula was checked.
- LibreOffice recalculated and printed the attachments. All four attachment pages and all four issued invoice pages were rendered and visually inspected. Totals were correct; text and rows were legible and unclipped.
- Four ledger-based XML reconstructions passed the unchanged official XSD. The current downloadable XSD was fetched again and matched the fixture SHA-256 `90af7504bab00e92bcf84971ed3088d9b7c67dd70219148dabe454e32a3b5498`. The saved reconstructions use a dummy Agent key.
- Changing an entry multiplier invalidated the reviewed fingerprint. Hourly quantity overrides, partial-month monthly billing and cross-year pricing were rejected.
- Changing a project's payroll flag preserved existing entry factors and historical billable quantities.
- Worker review, attachment and PDF requests were denied. A foreign company ID could not read a staging invoice PDF. Issued-entry multiplier edits were denied and the stored factors stayed unchanged.
- The synthetic worker could sign in to the deployed application, but directly opening `/admin/billing` redirected back to its timesheets. Both browser sessions were signed out afterward.
- October carry-over finalized as `100 + 2 - 26.50 = 75.50`. A repeated finalization made no second change. November used that balance and finalized to `100 + 75.50 - 1 = 174.50`. Re-downloading/finalizing October again used its original snapshot and preserved November's current balance.
- Removing only the cached PDF of synthetic invoice TESZT-2026-8 caused the deployed download endpoint to recover the existing provider document. Issuance time, attempt count and invoice count were preserved; a repeated download returned identical bytes. A local backup was retained until recovery completed.
- The deployed browser showed all four invoice-history entries and the actual November entry `0.25 × 4`, with quantity 1 and 100/27/127 HUF totals. Preview mode disabled both confirmation and issuance.
- The browser attachment export completed and returned to its ready state with no console warnings/errors. The automation download-event capture failed before the first click; the export was subsequently triggered directly. The browser-downloaded file itself was not captured. The same hosted endpoint's downloaded workbooks were independently inspected as described above.
- A local mobile review had no page-level horizontal overflow. A simulated uncertain result cleared confirmation and disabled issuance pending fresh confirmation.

## Defect found and fixed

The payroll multiplier migrations replaced `finalize_invoice_attachment_period` and reinstated SQLSTATE `40001` at both stale-data conflict sites. Hosted inspection confirmed that version was active. PostgREST 14 treats this code as a serialization retry signal; Supabase documents the resulting retry loop in its [troubleshooting guide](https://supabase.com/kb/troubleshooting/high-cpu-and-infinite-transaction-retries-when-using-custom-error-codes-in-rpc-functions).

Two new real-PostgreSQL regressions exercised a stale new attachment and a stale already-finalized snapshot. Both failed before the correction. Migration [`20261030142034_fix_attachment_conflict_retry.sql`](../supabase/migrations/20261030142034_fix_attachment_conflict_retry.sql) restores `PT409` in that one function. It changes no invoice data or billing calculation.

The migration was applied to hosted Supabase with explicit approval. Read-back confirmed the expected definition hash `f4a34e3276ccfb3ec835a186058571c2`, no `40001`, and unchanged signature, owner, search path, security mode and grants. The two actual PostgREST conflict requests returned HTTP 409/`PT409` in **0.096 s** and **0.065 s**. All 37 project-pricing/database checks passed after the fix.

## Regression checks

| Layer | Result |
| --- | --- |
| Complete backend suite, including isolated real PostgreSQL invoice, pricing and tenant-security tests | 300 passed |
| Frontend regression suite | 59 passed |
| Frontend TypeScript and production build | Passed |
| Python compilation and whitespace validation | Passed |
| Provider XML schema validation for the four staging ledger snapshots | Passed |

The backend suite also covers concurrent reservation, reservation locks, explicit provider rejection, ambiguous responses, duplicate-provider responses, incomplete confirmations, committed finalization with lost response, reconciliation, missing-PDF recovery, rounding, pagination and authorization. Those fault conditions were simulated locally; no real provider creation was retried after an uncertain outcome.

One pre-existing pytest warning concerns an empty exception-message match in a test. No failing regression remains. Existing Supabase security-advisor warnings were reviewed; this narrowly scoped migration preserved permissions and did not attempt unrelated security changes.

## Evidence and limits

- [Detailed assertion results](multiplier-e2e-results-2026-10-30.json).
- [Issued invoices, previews, attachment workbooks and redacted XML](staging-evidence-2026-10-30/).
- [Actual deployed invoice review](staging-evidence-2026-10-30/deployed-review.png).
- [Deployed attachment-export screen](staging-evidence-2026-10-30/deployed-attachment-export.png).
- [Reproducible staging runner](../scripts/multiplier_e2e.py).

The provider account's browser page was unreachable in the test browser. Current test mode was explicitly confirmed by the user; returned invoices carry the test watermark and TESZT numbering. Email delivery, NAV reporting, production-company issuance, all browser engines and all mobile flows are outside this result. Supporting timesheets are downloaded separately; the application's attachment-email switch is visibly disabled and invoice emails currently include the invoice only.

Synthetic invoice and attachment evidence is retained. One interrupted setup left an additional draft entry under a separate synthetic client; it was not included in the invoice matrix. Cleanup was independently verified: the staging company is inactive and in preview mode; the worker is inactive and unconfirmed; both logins are banned and new sign-in attempts fail; the private session/password file is removed. There are four new issued test invoices, zero enabled invoice emails, and zero uncertain or issuing attempts. The temporary local Vite and PostgreSQL servers were stopped. Final cleanup assertions are recorded in the result manifest.
