# ChronoSync integration map

Find the sibling repositories `chronosync-backend` and `chronosync` in the workspace; do not assume another checkout has the same absolute path or deployed revision.

- Backend contract: `app/schemas/invoices.py`.
- XML transport/parser: `app/services/invoice_providers/szamlazz_hu.py`.
- Shared review/XML rounding: `app/services/invoice_providers/amounts.py`.
- Review, reservation, issuance, confirmation and reconciliation: `app/services/client_invoice_service.py`.
- Credential encryption and tenant admin check: `app/services/company_service.py`.
- HTTP endpoints: `app/api/v1/endpoints/invoices.py`.
- Ledger RPCs: `supabase/migrations/20261022140000_create_manual_invoice_ledger.sql`, followed by `20261026083353_harden_manual_invoice_consistency.sql`.
- Frontend billing readiness, profile editor, review/history: `src/utils/clientBillingReadiness.ts`, `src/pages/ClientBillingPage.tsx`, `src/components/client-billing/`.
- Validation evidence: `docs/szamlazz-validation-2026-10-26.md` and the 2026-10-27 follow-up. Treat these as dated observations, not current deployment guarantees.

## Invariants

Manual issuance requires an unchanged review fingerprint and a confirmed database reservation before calling the provider. The RPC locks approved work, rejects active overlapping periods, and protects reserved work against edits. Provider failures of uncertain outcome retain reservations. Finalization is atomic and idempotent; actual invoice number, reviewed totals and a valid PDF are required for the normal success path. Fixed monthly fees cover exactly one complete calendar month. Provider external IDs are `chronosync-<ledger UUID>`.

`szamlazz_test_mode=true` is the application's preview gate. It does not select a Számlázz.hu sandbox. To exercise issuance against the provider's test account, a staging company must have that test account's encrypted key and permit issuance, while the selected provider account remains visibly in Tesztüzem. Never replace a real company's key for this purpose.

Client billing currently creates one line, using net unit prices and approved hours or quantity one. Reject `K.AFA` profiles because they cannot represent both required margin-scheme lines. Private HUF recipients derive the gross amount from the saved net price before B2C rounding. Client invoices use ordinary paper invoice mode, fulfillment at period end, and automatic MNB exchange rates for foreign currency; these remain business-policy choices to confirm with the owner.

The credential service needs a persistent `ENCRYPTION_MASTER_KEY` in backend secret configuration. Never rotate a deployed encryption key without migrating existing ciphertext. Key configuration, selected provider and preview gate are separate checks.

## Checks

Run backend `venv/bin/python -m pytest -q`. The real PostgreSQL suite is opt-in: `INVOICE_TEST_PG_DSN='host=/tmp/chronosync-invoice-audit port=55439 dbname=invoice_audit'`. It destroys its isolated fixture tables and rejects nonmatching DSNs; never point it at a hosted project. It is a focused schema reproduction, not a full Supabase clone.

Run frontend `node --test --test-isolation=none tests/*.test.mjs`, `npm run build`, and ESLint on affected invoice files. The synthetic UI harness is `/tests/invoice-ui/index.html` through Vite. These checks do not authenticate to the deployed app or prove a provider invoice.

Inspect hosted migration/function state and credential *presence* read-only before declaring deployment ready; avoid selecting ciphertext or secrets into tool outputs. Initial inspection on 2026-10-27 found the hardening migration missing, two companies in preview mode, no configured provider keys and zero ledger rows. Later that day, the user authorized applying it: version `20261026083353` is now recorded on hosted ChronoSync, its functions/trigger and authorization checks passed, and ledger counts and company preview modes were unchanged. Deployment of the compatible application and an authenticated staging-app run were still pending. Recheck rather than treating these observations as permanent.
