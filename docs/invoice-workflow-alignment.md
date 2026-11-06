# Invoice workflow alignment: first implementation

Prepared 4 November 2026 after comparing Ecovis's downloaded invoices with the
Test Inc examples. The migrations were applied and compatible staging applications
verified on 5 November; production applications have not been promoted. See the
[staging verification report](invoice-lifecycle-staging-2026-11-05.md).
Existing Ecovis recipients, prices and preview mode are unchanged.

## Recurring charges

Active monthly and person-per-month services now participate in invoice review
even without approved time. Missing prices or quantities stop that client's
review; the automatic batch lists the reason rather than issuing a partial bill.

Company settings → Projects → Edit pricing now has a **Default monthly quantity**.
Use 1 for a monthly fee or an explicitly agreed payroll headcount. This setting
also makes a non-hourly service recur monthly. Leave it empty when the quantity
must be entered in manual invoice review. Hourly services cannot have this
setting. The unsupported two-month unit remains manual.

A saved recurring quantity allows batch review to include a service without
time entries. The confirmation includes the resulting line and amount. A manual
quantity can override the saved quantity for that review. Zero cannot silently
exclude a monthly service. Recurring billing requires a complete calendar month;
a monthly fee's quantity must be 1. Inactive projects are excluded unless their
approved time is being invoiced, in which case the existing activation check
still applies. One-off non-hourly projects retain explicit quantity selection.

Resolved quantities are included in the review fingerprint and reservation
snapshot. Database reservation independently checks recurring-project
completeness and price versions before any provider request. Saved recurring
quantities and invoice names carry forward through the annual price rollover.

## Invoice settings and customer-facing text

Each client's billing profile now exposes paper/electronic invoice type,
fulfillment date policy (period end, payment deadline or issue date), optional
numbering prefix, and an optional seller bank name/account pair. Existing profiles
keep paper mode and period-end fulfillment until an administrator explicitly
chooses different settings. Leaving prefix and both bank fields empty preserves
the provider account defaults. Prefixes must already exist in Számlázz.hu.

The outgoing XML carries the selected electronic invoice flag, date, prefix and
seller bank fields. Manual and batch confirmation show these settings. Reservation
validates the saved document/date settings as well as the existing profile version
check, so an outdated or altered review cannot proceed.

Generated invoices now include readable service-period dates. Project pricing
also exposes an optional invoice line name, invoice line note and PO reference;
these appear in review and outgoing XML. Internal project names remain unchanged.
Use an invoice line name in the recipient's language when needed; changing the
invoice template language does not translate project names automatically.

## Release

Migration: `supabase/migrations/20261104194919_recurring_invoice_configuration.sql`.
It adds configuration fields and replaces the private project-price reservation
validator. Existing RLS and tenant-admin restrictions remain in effect; no public
privileged RPC is added.

Ship the migration, backend and frontend as one coordinated release while Ecovis
remains in preview mode. The backend and frontend require the new columns. The
database guard deliberately rejects old reviews that omit recurring services or
newly configured metadata. No historical invoice snapshots are rewritten. Compare
hosted migration history before applying this single migration; the workspace's
existing history spans two repositories.

Configure representative staging clients with their reviewed monthly quantities,
electronic invoice preferences, dates and bank details before enabling live use.

## Initial verification (4 November)

- Complete backend suite: 252 passed; database suites skip unless their explicit
  disposable DSNs are supplied.
- Actual PostgreSQL pricing/reservation suite: 45 checks, covering omitted
  recurring services without time, zero-time reservations, metadata tampering,
  accepted electronic/date/bank settings, rollover and existing tenant, price,
  multiplier and rounding protections.
- Frontend production build, affected-file ESLint and 77 unit tests.
- Existing pricing/billing browser suite extended to check saved recurring
  quantity/name/note/PO, bank-pair validation, saved invoice preferences and review
  rendering, in addition to its desktop/mobile and English/Hungarian checks.
- Official unchanged XSD fixtures validate representative XML containing the
  electronic flag, bank overrides, prefix and period note.
- Actual Számlázz.hu staging preview accepted electronic-invoice mode, due-date
  fulfillment, bank overrides, a monthly fee plus 26 payroll units and period/PO
  notes: HUF 229,000 net, HUF 290,830 gross, with no invoice number and email off.
  This establishes preview acceptance, not signed issued-document or email delivery.

## Remaining work from the comparison

Fee currency versus invoice currency conversion (including reviewed rate/date)
awaits the company's clarification. Storno and reservation release for reviewed
replacement passed the deployed staging flow; see [invoice-storno.md](invoice-storno.md).
Per-invoice annex generation, archive storage and email upload are implemented
and exercised in staging; see [invoice-annex-delivery.md](invoice-annex-delivery.md) for verification
and delivery limits. Live recipient metadata, contract rates and unit-price
rounding still need approved reconciliation; the implementation does not infer
or overwrite those business decisions from the sample PDFs.
