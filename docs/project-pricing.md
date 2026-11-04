# Project prices and annual indexes

Company settings → Projects is the price editor. Each existing project has an annual
net price, currency, unit and annual increase rule. Original imported fees and their
denomination are historical data and never substitute for a missing annual price.
The content-sized pricing dialog is at most 512px wide and fits mobile screens.

Company settings also contains the following year's KSH and EU index percentages.
They are stored as fractional rates in `company_price_indexes`, with an effective
year. Changing next year's rate leaves all saved current-year prices unchanged.
The browser and database both use the Europe/Budapest calendar year.

## Annual rollover

- KSH/EU rules compound the previous year's saved indexed fee, rounded to four decimals.
- No annual increase carries the saved price unchanged.
- Manual review creates an unset price for the new year.
- A missing prior price stays missing, including when the original fee exists. Zero is valid.
- The original fee, original currency/unit and project invoice currency/unit are preserved.
- Existing prices for a target year are never overwritten, including manual overrides.
- Effective-year index history is retained. An unchanged rate carries forward until
  an admin configures a different rate for a later year.
- Generated records identify their source year and applied index rate.

The `annual-project-price-rollover` database job runs daily at 23:00 GMT, which is
midnight in Budapest at New Year. It inserts absent annual prices and catches up
missed years. Price reads and invoice preparation also call a tenant-admin-only,
idempotent current-year catch-up RPC. Inactive/deleted companies are excluded.
The private deterministic target-year function is inaccessible to authenticated
users; public callers cannot generate arbitrary future years.

## Invoicing

Review, preview and issuance resolve annual project prices for the selected invoice
period. All approved work in that client period must be represented. Hourly units
use approved project hours; quantities for other units are entered explicitly.
Monthly units require quantity 1 and exactly one complete calendar month. Periods
crossing a pricing year require separate invoices. Mixing currencies in the same
client period is blocked; this flow does not yet split one period into invoices by
currency. None of Ecovis’s imported priced clients has mixed currencies.

The billing screen displays prices read-only and links to Company settings. It
only edits recipient, payment, tax and delivery details. Legacy client rate columns
remain for compatibility with existing data but cannot be changed or used for new
invoices after the source-switch migration is activated.

Reviews fingerprint the actual project lines and their price versions. Reservation
locks and compares the saved project prices, units, currency, project identity,
approved hours, entered quantities and rounded totals. A changed or missing price
requires another review before any provider request. Issued invoice snapshots stay
unchanged when a price is subsequently edited. Existing duplicate protection and
uncertain-provider-outcome reconciliation remain in place.

## Deployment status on 28 October 2026

The initial project price schema, original-denomination schema, annual indexing
schema and database job are live in Supabase project `kqihfxhewjgrwoepdsau`.
Ecovis has 124 imported existing projects: 82 prices, 42 unset prices. Workbook
index settings are KSH 4.4% and EU 2.4%. Imported rules map to 41 KSH, 5 EU,
36 no-increase and 42 manual-review rows. No projects were created by this import.
The checksum of all original imported fields is unchanged after the index migration.

The frontend and backend application changes are local. The following migration
is deliberately pending until the backend is deployed:

`supabase/migrations/20261028100422_invoice_from_project_pricing.sql`

Deploy the backend, apply that migration, then deploy the frontend as one release.
The migration switches new reservations to project lines and freezes legacy client
pricing. Applying it ahead of the backend would temporarily reject old invoice requests.
The new frontend refuses legacy-rate reviews to prevent issuance from an outdated
backend. No production invoice or invoice email was generated during verification.

The base pricing migrations are stored in the sibling frontend repository. Existing
database history spans both repositories; do not blindly push either migration folder
without comparing it with hosted migration history.

## Verification

- Backend suite: 186 passed; database-dependent tests skip when their explicit local DSNs are absent.
- 25 disposable PostgreSQL checks cover real reservation, tampered/stale prices,
  tenant denial, annual compounding, zero/null/manual behavior, idempotency,
  historical rates, next-year-only edits, multiple invoice lines and provider rounding.
- Frontend production build, ESLint, recipient readiness and isolated Playwright checks pass.
  The existing invoice confirmation/reconciliation harness also passes all 14 checks.
- Browser checks cover editable index settings, compact desktop/mobile dialogs,
  Budapest New Year label changes in an already open page, preserved editor target
  year, read-only invoice rates, explicit quantities, provider preview requests,
  multiline review, stale-price rejection and refusal of legacy backend reviews.
- All browser external requests are intercepted. Local SQL tests require the
  explicitly guarded disposable database; no provider credentials are used.

The advisor reports one new expected warning for the authenticated SECURITY DEFINER
catch-up RPC. It authenticates the active tenant admin internally, has a fixed empty
search path and denies other tenants and employees in database tests. The standard
advisor guidance is [here](https://supabase.com/docs/guides/database/database-linter?lint=0029_authenticated_security_definer_function_executable).
No anonymous execution or new missing-RLS warning was introduced.

An optional live all-tenant catch-up verification was rejected by automatic approval
review because it exceeded Ecovis-scoped authorization. It was not run. Read-only
checks confirmed that only Ecovis has imported prices and the cron timezone is GMT.
