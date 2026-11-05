# Invoice annex generation and delivery

Implemented locally on 4 November 2026. These changes have not been deployed and
no customer email or live invoice was sent during implementation.

## Behavior

The saved **Save billing report and attach it to invoice emails** preference now
controls a per-invoice XLSX workbook. It contains the reviewed project charges,
quantities, unit prices, VAT, totals, notes and PO references, followed by the
reviewed time entries with their individual billing multipliers. Fixed monthly
and other quantity-based charges have a meaningful report even without time.
Labels follow the client's Hungarian/English attachment language; configured
service names and source descriptions retain their saved text.

Invoice review shows the filename, size and delivery choice. **Review billing
report** downloads the exact current reviewed workbook after checking the review
fingerprint. The batch confirmation shows the corresponding client's report and
email choice in its billing details.

Email-enabled invoices upload that client's workbook alongside the XML using
`attachfile1`. When email is disabled, the workbook is saved for download without
requesting notification or uploading an attachment the provider would ignore.
Turning the report preference off omits report generation, upload and storage.

Each report is generated exclusively from one invoice review. It does not fetch
additional clients, later time entries, current carry-over balances or changed
pricing. This per-invoice breakdown records the charges actually being issued;
the separate monthly attachment export continues to handle its own carry-over
finalization flow.

## Review, storage and failures

XLSX ZIP timestamps and workbook modification metadata are normalized, so the same
review produces the same bytes and SHA-256 digest. The report's name, language,
size and digest are part of the review fingerprint. Changed data, preferences,
language or generator output requires another review before issuance.

The report is generated and checked against the conservative 2,000,000-byte
provider limit before reservation. Oversized reports make that client ineligible
in batch preflight; other eligible clients can continue after confirmation.
Workbook cells escape formula-leading text, and text exceeding Excel's cell limit
blocks the report rather than silently truncating it.

After reservation and before any provider creation call, an authenticated RPC
stores the bytes. PostgreSQL independently verifies the size, ZIP signature and
digest against the reviewed metadata. A failed or unconfirmed storage result
prevents the provider call. Finalization also requires the stored report when the
review requested it. The storage RPC is tenant-admin scoped, unavailable to
anonymous callers, idempotent for the same bytes and closed after issuance.

Once the provider call starts, unknown outcomes keep the normal uncertain ledger
state and reservations. The stored report survives timeouts and reconciliation;
the application never resends an invoice to recover its report.

**Download billing report** in invoice history serves the stored bytes, checks
their digest and never regenerates from current client data. The original report
remains available after storno alongside the original and reversal PDFs. A safely
failed issuance can replace its report only during a fresh reserved attempt.

## Release

Apply `supabase/migrations/20261104204746_invoice_annex_delivery.sql` after the
pending recurring-configuration and storno migrations. Deploy the compatible
backend and frontend together while production remains in preview mode. The
migration adds private report storage and a generated availability flag to the
existing RLS-protected ledger, a reservation/finalization trigger and an explicit
admin storage RPC. It does not change saved recipient data, pricing, email
preferences, report preferences or production issuance gates.

Older ledger records have no annex metadata and keep their historical behavior.
Old reviews that omit the report when its saved preference is enabled are rejected
and require a fresh review with the compatible app.

## Verification and limits

Tests cover deterministic valid XLSX generation, Hungarian/English labels,
price/VAT totals, payroll multipliers, zero-time monthly fees, formula injection,
size limits, preference/language fingerprint changes, saving before provider
calls, disabled email, exact multipart bytes, storage failure, provider timeout,
read-only review/archive downloads, and oversized-client batch skips.

Disposable PostgreSQL tests cover required storage before finalization, metadata
and byte mismatches, tenant/employee/anonymous restrictions, retry resets,
uncertain-state retention and report preservation through storno. Supabase
security advisors found no annex/ledger issues; their four findings are the
minimal fixture's unrelated tables without production RLS definitions.

Browser checks cover saving both preference states, reviewing and downloading a
report, rejecting an invalid download, and archived report downloads after storno
in English and mobile Hungarian. Representative two-sheet reports were exported
with LibreOffice and their printed layout inspected in both languages.

The final combined backend/database run passed 397 tests, with 96 tests requiring
other integration environments skipped. The frontend production build,
affected-file ESLint, 77 frontend unit tests and both manual/batch browser suites
passed. Sample workbook renders are in `/tmp/chronosync-annex-qa/` for this session.

Provider transport was simulated using the actual HTTP client. The provider's
live attachment acceptance and actual email delivery have not been tested. The
provider documents that invalid attachments can be omitted while the invoice
notification still sends, so a successful invoice response is not a delivery
receipt. Confirm attachment presence in an authorized staging email before
production rollout; never reissue an invoice to repair missing email delivery.

Provider contract:
https://docs.szamlazz.hu/agent/generating_invoice/settings_and_rules/email-notification
