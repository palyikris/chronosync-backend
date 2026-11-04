# Automatic invoices during attachment generation

The attachment page offers **Create real invoices automatically**, off by default,
beside the remaining-hours finalization option. Selecting it runs
`POST /api/v1/invoices/batch-review` for the selected company, clients and period.
This endpoint requires the same active company administrator access as manual
invoicing, and the invoicing module must be enabled. The checkbox is hidden when
the company has only the attachment module. It checks live issuance mode and the locally stored provider credential,
then checks each client for overlapping issued or unresolved ledger records and
builds the existing invoice review. It neither reserves work nor calls the provider.

The confirmation dialog lists eligible recipients, totals and email preferences,
with expandable tax, date and project details. Ineligible clients are listed with
the reason. Missing billing information, missing project prices, projects requiring
manual quantities, and overlapping invoices prevent automatic issuance for that
client. The user must acknowledge the results before continuing, including when
every client is skipped.

After confirmation the existing XLSX attachment request completes first, preserving
all selected clients and the remaining-hours option. Each eligible review then goes
through the existing `client-issue` endpoint sequentially, with its original review
fingerprint and explicit confirmation. That endpoint rechecks the current data,
reserves approved work and finalizes the invoice using the existing ledger rules.
No invoice is issued if the attachment request fails. Skipped clients still receive
attachments where reportable work exists.

The result dialog reports each successful invoice number and each failure. Failures
do not prevent later eligible clients from being processed. Creation requests are
never automatically retried; check invoice history and reconcile uncertain outcomes
before starting another attempt. Closing the dialog is blocked while processing.
The batch runs in the open browser, so keep it open until completion. A closed tab
can leave later clients unprocessed; already-started invoices remain protected by
the existing ledger and reservation flow.

No database migration or provider XML change is required. Deploy the backend with
the new preflight endpoint before deploying the frontend.

## Verification

Backend checks: `venv/bin/python -m pytest -q`. The batch-specific checks are in
`tests/test_invoice_batch_review.py`; existing manual invoice tests cover review
changes, reservation, provider rejection and uncertain outcomes.

Frontend: `node --test --test-isolation=none tests/*.test.mjs`, `npm run build`, and
ESLint on the attachment page, invoice review component, billing service and types.

The isolated browser checks exercise the active attachment page in the frontend's
`tests/invoice-attachment-ui/smoke.mjs`. Start Vite on port 5193 with synthetic
`VITE_SUPABASE_URL=https://invoice-tests.supabase.co`,
`VITE_SUPABASE_PUBLISHABLE_KEY=synthetic-public-key`, and
`VITE_API_BASE_URL=https://invoice-api.test/api/v1`, then run the script with a
Playwright installation available through `PLAYWRIGHT_MODULE_PATH` if necessary.
All external requests are intercepted. It covers attachment-only generation,
confirmation and cancellation, mixed eligibility, remaining-hours finalization,
attachment failure, uncertain invoice results, all-ineligible selection and the
Hungarian mobile interface. These checks do not issue real provider invoices or
establish deployed readiness.
