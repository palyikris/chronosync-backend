# Invoice notes and date rules

Client invoicing supports a saved invoice-wide note, calendar month-end payment
rules, and manual fulfillment/payment-date overrides for a single invoice.
The existing approved-hours, multiplier, project-price and tax calculations remain
the source of the invoice lines.

## Billing settings

- **Payment deadline:** the existing number of days after issue, or the last day
  of a month. Month-end rules use the issue date or service-period end as their
  reference, with an offset of zero, one or two months. They use calendar months,
  including leap years and year boundaries.
- **Invoice note:** optional plain text, up to 450 characters. The generated
  service-period text stays first; the note follows on a new line. Project line
  notes and PO references remain on their own lines. No VAT wording is inferred.
- Existing profiles default to `days_after_issue`, their existing day count,
  `issue_date` as the month reference, offset 1, and no note. The inactive
  month-end settings have no effect on a day-count rule.

An October service period invoiced on 3 November has a 30 November deadline with
`period_end` plus one month, or a 31 December deadline with `issue_date` plus one
month. The selected reference is explicit in the settings UI.

## Manual reviews and previews

The invoice creation panel has a collapsed **Custom dates for this invoice**
section, so a billing admin can enter dates before the first review. Blank fields
use the saved client rules. This also permits resolving an old service period
whose calculated month-end deadline is already past, without changing the client
defaults. Such a deadline blocks automatic/default generation until resolved.

The review modal exposes the same shared date fields. Editing a date clears the
confirmation and disables issuance, report download and PDF preview until
**Refresh review** completes. The refreshed review requires a new confirmation.
**Use client rules** removes the overrides and obtains another fresh review.

Manual payment deadlines must be on/after issue and at most 366 days later;
manual fulfillment dates must be within 366 days before/after issue. Issue dates
remain today's date in Europe/Budapest and cannot be supplied by the caller.
Automatic batch requests do not accept manual overrides.

The final dates, override metadata and composed note participate in the review
fingerprint and stored snapshot. Preview, issuance and reviewed-annex requests
carry the same overrides. PDF downloads from a review also send its fingerprint;
a changed profile, price, approved entry, date or issue day requires another
review instead of silently downloading a different invoice.

## Reservation protection

Migration `20261110115432_client_invoice_terms.sql` adds four constrained columns
to `client_billing_profiles` and replaces the existing private project invoice
validator. It adds no public RPC, privileged function or permission grant.
Existing billing-admin authorization, tenant checks and RLS stay in place.

Under the reservation's existing locks, the database independently derives the
deadline and fulfillment date, validates override shape/type/range, and compares
the composed note with the saved profile. Invalid XML control characters and
overlong/empty non-null notes cannot be saved. The existing project, money,
recurring-service, multiplier, reservation, duplication and outcome-recovery
checks remain in the same validator and lifecycle.

## Local verification — 10 November 2026

- Full backend suite with the isolated invoice and project-pricing databases:
  **494 passed, 57 skipped**. The skipped tests require separate optional company
  module/tenant-security database fixtures; invoice tenant/admin checks ran.
- Real PostgreSQL coverage includes accepted backend-generated notes/dates,
  rejected tampering, invalid note constraints, override archival, unchanged
  approved hours, private function permissions and cross-tenant/admin denial,
  alongside the existing issuance, storno, duplicate and annex regressions.
- Unchanged official invoice XSD validates generated XML containing the composed
  multiline note and final dates; parsed outgoing fields match the reviewed data.
- Frontend: **89 unit tests**, production TypeScript/Vite build and affected-file
  ESLint passed.
- Integrated application browser harness passed in English/Hungarian on desktop
  and mobile. It covers saved notes and month-end settings, overrides before and
  during review, disabled issuance on edits, fresh confirmation, reviewed preview
  fingerprints, preview errors inside the open dialog, reset to defaults,
  matching issue payloads, and existing pricing,
  storno and annex flows. External requests are intercepted and synthetic.
- The existing invoice confirmation/reconciliation browser harness also passed.
- Screenshots were visually checked, including the new Hungarian mobile fields.

These local checks preceded the hosted migration and provider checks below.

## Hosted migration and provider E2E — 10 November 2026

Migration `20261110115432_client_invoice_terms` is applied to hosted ChronoSync
(`kqihfxhewjgrwoepdsau`). Its generated hosted version is also the local migration
filename and the database fixture's reference. All 100 existing billing profiles
kept their original values and received the compatible defaults. The hashes of
the existing profiles, company rows and 22 ledger records stayed unchanged after
the complete test. There are no unsettled invoice attempts. The validator remains
security-invoker, has an empty search path, and is not executable by anonymous or
authenticated callers. The security advisor reported no new findings.

The updated local frontend and backend ran against that real hosted database and
the dedicated **Teszt cég** provider. The user confirmed visible **TESZTÜZEM**;
the returned invoice XML independently reports `teszt=true`. This used a separate
synthetic mock tenant, **Test Inc invoice terms staging 2026-11-10**, leaving the
existing Test Inc credentials, records and configuration intact. The local
backend used a fresh temporary encryption key for that isolated tenant only.

All **54 E2E checks passed**, including browser-saved month-end rules and multiline
notes, dates entered before and during review, cleared confirmation and disabled
actions while dates are dirty, refreshed confirmation, actual provider previews,
stale review/preview/annex/issuance rejection, hosted constraints, regular-user and
cross-tenant denial, duplicate issuance rejection, immutable archived documents,
and approved-work finalization/restoration. A focused regression rerun passed
**36 tests**.

The browser issued **TESZT-2026-14** for **2.5 approved hours**, **25,000 HUF net**,
**6,750 HUF VAT**, **31,750 HUF gross**, with fulfillment **2026-11-05** and payment
deadline **2027-01-15**. Provider XML, actual rendered preview and issued PDF, and
the stored snapshot match the review's dates, note and totals. The archived annex
matches the reviewed bytes and SHA-256. Draft, submitted and rejected work stayed
unbilled. After explicit user approval, browser cancellation produced
**TESZT-2026-15** for the negative amounts; provider lookup confirms its original
reference and cancellation status. Both documents remain archived.

No customer email was requested. The temporary sessions were revoked, synthetic
passwords cleared and identities banned, staging credential removed, mock tenant
disabled and reset to preview mode, temporary secret files deleted, and local
servers stopped. Application changes were **not deployed** by this run.

Evidence: [E2E report and documents](invoice-terms-e2e-2026-11-10/README.md).

## Release order

1. The new migration is applied and verified in hosted history as
   `20261110115432_client_invoice_terms`.
2. Deploy the compatible backend, then the frontend. Both read the new columns.
3. Check a default profile and a configured month-end/note profile through a fresh
   review and provider preview before enabling any new live invoicing setup.

Migration defaults permit the old application to continue using its existing
rules during a coordinated rollout. Once nondefault rules/notes are configured,
an old backend cannot produce a matching reservation; rollback planning must
account for those profiles. Historical invoice snapshots are not rewritten.
