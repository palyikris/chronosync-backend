# Invoice attachment generation and delivery

Every new invoice uses the existing invoice attachment (certificate of completion)
template. The separate two-sheet billing report and its opt-out setting have been
removed. The `attach_timesheet_report` wire field remains for compatibility with
older clients and ledger snapshots; new reviews always set it to true.

## Contents

The shared `ExcelReportService` template produces one client sheet in Hungarian
or English, with the existing branding, service period and issue date. Issued
attachments show the reviewed time entries with their dates, service names,
logged hours, billing multipliers and multiplied hours. A service table includes
reviewed quantities, units, notes and PO references, including fixed-fee invoices
without time entries. Prices and VAT remain on the invoice.

The ordinary monthly attachment export retains its contracted-hours and carry-over
calculations. The issued attachment uses the reviewed invoice snapshot only;
it does not fetch current balances, change carry-over or finalize a month.

## Review, delivery and archive

The attachment is generated for every review, checked against the provider's
2,000,000-byte attachment limit, and included in the review fingerprint through
its filename, language, size and SHA-256 digest. Workbook and ZIP timestamps are
normalized, so review download and issuance reproduce identical bytes. Changed
entries, language or template output require another review.

The attachment is stored after reservation and before contacting the provider.
The database verifies size, ZIP signature and digest. A storage failure prevents
the provider call, and invoice finalization requires the stored reviewed bytes.
When invoice email delivery is enabled, the same workbook is uploaded using the
existing `attachfile1` transport. Otherwise it remains saved for download.

Invoice review offers **Review invoice attachment**. Invoice history offers
**Download invoice attachment**, which returns the stored bytes after checking
their digest. Historical reports, including those on reversed invoices, are
preserved exactly as issued. They are never regenerated from current data.

## Deployment

Apply `supabase/migrations/20261110151118_require_invoice_attachment.sql` and
deploy the compatible backend and frontend together. The migration makes
attachments mandatory for new reservations and safe retries even when the old
saved report preference is false. It retains tenant/admin authorization, storage
checks and the existing behavior of already reserved or issued ledger records.
Old reviews without attachment metadata must be refreshed before a new issuance.
No saved historical files or recipient email preferences are changed.

## Verification

Regression tests cover localized deterministic template output, service quantities
without time entries, multiplier reconciliation, formula safety, size limits,
mandatory attachments regardless of the old preference, review fingerprints,
storage-before-provider ordering, email-disabled storage, actual multipart bytes,
provider uncertainty, and unchanged archive downloads after storno.

Disposable PostgreSQL checks verify mandatory metadata and saved bytes, legacy
preferences, language and digest validation, authorization, retry behavior and
archive preservation. Browser checks exercise the removed toggle, profile saving,
review downloads and archived attachment downloads. English and Hungarian print
layouts are rendered with LibreOffice and visually inspected.

These local checks do not establish a new live provider transaction or email
receipt. A provider success response is not an email-delivery receipt. The provider
contract is documented at
https://docs.szamlazz.hu/agent/generating_invoice/settings_and_rules/email-notification.
