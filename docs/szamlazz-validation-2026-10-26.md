**Számlázz.hu invoice validation — 26 October 2026**

The original “ready in theory” assessment was too optimistic. The audit reproduced a production finalization failure and found several gaps in concurrency, response classification and recovery. Those defects now have local fixes and regression tests. Production readiness is still pending deployment and an actual Számlázz.hu test-account run. No invoice request was sent to a provider account during this audit.

**Evidence and scope**

Reviewed the backend provider, API contracts and endpoints, credential lifecycle, billing profile conversion, review fingerprint, reservation/finalization/reconciliation RPCs, timesheet triggers, RLS and frontend review/history screens. Inspected the hosted Supabase schema read-only. The hosted database had zero ledger rows, two companies in preview mode, and neither company had a configured provider key. The local backend also had no `ENCRYPTION_MASTER_KEY`; hosted backend secret configuration was not inspected.

The PostgreSQL suite recreates relevant tables and roles in a disposable local database. It uses the original ledger migration, reproduces the unqualified-table behavior of the hosted timesheet trigger, and then applies the new hardening migration. Local PostgreSQL is 18.6; the hosted project is 17.6.1.147. This is real SQL execution and concurrent sessions, but it does not replace a test on the hosted version with every production trigger and policy.

| Validation | Result |
| --- | --- |
| Existing backend baseline | 29 passed |
| Expanded backend suite | 144 passed, including 37 real PostgreSQL cases |
| Frontend tests | 21 passed (19 existing and 2 billing-readiness regressions) |
| Invoice browser checks | 14 passed in Chromium, desktop and mobile |
| Production frontend build | Passed; existing large-bundle warning |
| Invoice-related ESLint checks | Passed |
| Repository-wide ESLint | 11 existing errors and 2 warnings outside the invoice changes |
| Actual provider invoice / provider PDF rendering / email delivery | Pending test-account credential |
| Deployment of these fixes | Pending |

**Reproduced defects and fixes**

| Finding | Failure before the fix | Local result |
| --- | --- | --- |
| Timesheet trigger search path | `finalize_manual_invoice` has an empty search path, while the hosted `check_timesheet_client_active` trigger refers to `clients` and `projects` without schemas. Finalization raises `relation "clients" does not exist` after a provider could already have created the invoice. | Qualified table references. The regression explicitly proves the old failure before applying the fix. |
| Archiving a client during issuance | A successful remote invoice could become impossible to finalize because the active-client trigger rejects even a billing status transition. | Permit the exact approved-to-invoiced transition without changing work details; reserved-entry protection still applies. |
| Ambiguous XML mistaken for rejection | Any XML lacking `<sikeres>true</sikeres>` was considered rejected, potentially releasing a reservation after an HTML gateway page or malformed provider response. | Validate the response root, explicit success/failure values and error evidence. Unknown or contradictory responses remain uncertain. Accept XSD boolean forms `1` and `0`. |
| Provider duplicate errors | Codes 71/152 were treated as ordinary rejection and unlocked retries despite evidence of an existing invoice. | Keep these outcomes uncertain for external reconciliation. |
| Edits between review and reservation | The old RPC compared entry counts and aggregate hours but missed description changes, compensating edits and billing-profile changes. | Lock rows, compare per-entry snapshot details and profile `updated_at`, then copy the reservation in one transaction. |
| Edits after reservation | Approved work could be changed, deleted or moved while remote issuance was running. | Database trigger protects reserved and issued work details and billing status. A concurrent-edit regression verifies the guard after waiting for reservation commit. |
| Overlapping periods | Exact period uniqueness did not prevent another overlapping period, especially fixed fees with no shared entries. | Serialize reservations per client and reject overlapping active ledger periods. |
| Fixed monthly periods | Arbitrary partial or multiple-month selections were billed as quantity one. | Require exactly one full calendar month in the backend and RPC. Hourly billing retains arbitrary supported ranges. |
| Interrupted worker | `issuing` could remain permanently stuck, with no reconciliation controls. | History refresh changes attempts older than ten minutes to uncertain, retaining every reservation. No automatic retry. |
| Lost database confirmation | A commit followed by a lost response could be presented as failure. | Local finalization is idempotent; the service checks history for a matching committed invoice before reporting uncertainty. |
| Missing RPC confirmation | Empty reservation responses could fall back to a generated UUID and still call the provider. | Require a valid reservation response before outbound issuance; validate finalization responses too. |
| Credential cleanup error after submission | An HTTP exception after entering the credential context could be mistaken for a pre-submission configuration failure. | Track whether the provider call started; post-submission failures stay uncertain. |
| Foreign currency | Requests lacked an exchange-rate bank and rate policy. | Use the documented automatic MNB policy for client invoices; show it in review. Direct payloads support a positive explicit rate and bank. Unsupported currency codes fail validation. |
| Response size | The limit was checked after buffering, and a 25 MB XML cap could reject a valid 25 MB PDF after base64 expansion. | Bound streamed XML at 36 MB and decoded PDFs at 25 MB. |
| Recipient validation | Whitespace, XML-invalid characters and missing required tax identifiers reached later stages. | Strip text, reject XML-invalid characters, and require identifiers for the selected recipient type. Non-EU business readiness now requires a tax number. |
| Reconciliation UI | Field labels lacked input IDs; a failed reconciliation was difficult to see under the modal; successful reconciliation left it open. | Accessible labels, in-modal errors, retained inputs on failure and close on success. Disable fields during submission. |
| Misleading attachment control | A checked preference suggested that timesheet reports were attached, but no provider attachment is generated. | Disable that control and explain that reports must be downloaded separately. |

Other UI changes reset issuance confirmation after an attempt, refresh timesheet queries after successful issuance, and poll history while an attempt is issuing. The existing fingerprint-based review remount remains in place.

**Protocol checks against primary sources**

The endpoint, POST method and multipart field match the official [invoice request specification](https://docs.szamlazz.hu/hu/agent/generating_invoice/request): `https://www.szamlazz.hu/szamla/` and `action-xmlagentxmlfile`. The XML namespace, element order, response version and preview field were checked against the [published XML/XSD](https://docs.szamlazz.hu/hu/agent/generating_invoice/xml). An unchanged downloaded XSD is committed as a test fixture with URL, date and SHA-256 provenance. Fifteen language/currency combinations validate through `xmllint`, including escaped accented names, ampersands and angle brackets.

Namespaced XML v2 success, amounts and base64 PDF extraction match the [response specification](https://docs.szamlazz.hu/hu/agent/generating_invoice/response). Adversarial cases cover wrong namespaces and roots, absent/invalid/duplicate status fields, contradictory error/success evidence, XML entities, malformed XML, non-finite totals, invalid base64, non-PDF bytes, oversized responses and HTTP 400/401/429/500/502/503. Failed requests are not automatically retried.

HUF net-based line rounding matches the documented sequence: round line net, calculate and round VAT from that net, then add the integer amounts. Independent fractional examples test both review totals and generated XML. EUR decimal examples are also covered. See [rounding](https://docs.szamlazz.hu/hu/agent/generating_invoice/settings_and_rules/rounding). The currency allowlist follows [supported currencies](https://docs.szamlazz.hu/hu/agent/generating_invoice/settings_and_rules/currencies); the automatic MNB exception is documented in the invoice XSD comment. Provider acceptance of that policy is still part of the external smoke test.

The [order-number rules](https://docs.szamlazz.hu/hu/agent/generating_invoice/settings_and_rules/order-number) make provider duplicate protection dependent on an account setting. `szamlaKulsoAzon` is a lookup identifier, not an unconditional idempotency guarantee. Keep the local ledger lock as the primary protection and enable the provider's “Rendelésszám ismétlődés tiltása” setting as an additional safeguard. A duplicate request may return an existing invoice when specified matching conditions hold; other duplicate outcomes can be codes 71/152.

The [error guidance](https://docs.szamlazz.hu/hu/agent/basics/error-handling) forbids endless retries and gives a five-attempt ceiling. The local RPC now limits a ledger record to five attempts. Correcting an exhausted record requires an operator/support decision rather than repeated clicking.

Preview endpoints force preview/PDF download and disable recipient email regardless of submitted values. Normal requests explicitly send the email flag, consistent with [email notification rules](https://docs.szamlazz.hu/hu/agent/generating_invoice/settings_and_rules/email-notification). Agent keys stay encrypted in database storage and do not enter the frontend. Lowercase normalization follows [authentication guidance](https://docs.szamlazz.hu/hu/agent/basics/authentication).

The official [session-cookie guidance](https://docs.szamlazz.hu/hu/agent/basics/session-cookie) recommends cookie reuse, particularly at volume. HTTPX handles cookies within each client, but production currently creates a fresh client per manual operation. Cross-request session reuse remains a performance improvement, not a prerequisite proven by these tests.

**Public implementation comparisons**

Inspected actual source in [theriverman/szamlazz.py — client.py](https://github.com/theriverman/szamlazz.py/blob/master/szamlazz/client.py) and [ewngs/szamlazz.js — Client.js](https://github.com/ewngs/szamlazz.js/blob/master/lib/Client.js), plus the Node client's [Invoice.js](https://github.com/ewngs/szamlazz.js/blob/master/lib/Invoice.js) and [cookie client](https://github.com/ewngs/szamlazz.js/blob/master/lib/InitAxios.js).

Both implementations corroborate the multipart field, Agent authentication, invoice structure and PDF handling. The Python client validates generated XML against its bundled schema. The Node implementation inspects provider error headers, handles XML v2 base64 PDFs, exposes exchange bank/rate and reuses a cookie jar. These are public implementation references; their existence is not evidence of successful production use of this application's workflow. No third-party package was installed or copied into the runtime.

**Remaining limits and release conditions**

1. Configure a separate Számlázz.hu test account and use its Agent key for a staging company. The [test API instructions](https://tudastar.szamlazz.hu/gyik/teszt-api-hozzaferes) describe creating a second account and enabling Tesztüzem before issuance. ChronoSync's `szamlazz_test_mode` gates our application; it cannot change the provider account's mode. Keep all real-company issuance disabled during validation.
2. Set a persistent, valid backend `ENCRYPTION_MASTER_KEY` through the deployment secret manager. The checkout's local environment currently lacks it. Existing encrypted credentials require the original key; never replace a deployed encryption key without a migration plan. Do not paste keys into chat or logs.
3. Deploy the hardening migration, backend and frontend together before enabling any company for issuance. The new reservation RPC requires the billing-profile timestamp now included by the new backend. The migration was tested locally and has **not** been applied to the hosted database during this audit.
4. Group tax IDs need an actual provider test. The [official recipient tax guidance](https://tudastar.szamlazz.hu/gyik/vevo-adoszama-szamlan) supports `csoportazonosito`, while the downloadable XSD omits it. The existing group-field mapping was retained. No schema extension was invented to manufacture a passing test. The ordinary-recipient schema tests do not establish group-recipient acceptance.
5. Manual reconciliation as issued currently stores the confirmed invoice number and totals without a PDF. Remote PDF recovery by external ID is supported by the [provider API](https://docs.szamlazz.hu/hu/agent/querying_pdf/request), but is not implemented here. The normal success path stores a PDF; the manual recovery path can have `pdf_available=false`. This is a remaining usability gap.
6. Before resolving an uncertain attempt as not issued, stop any abandoned worker and establish that the provider request has finished, then verify the external/order ID in Számlázz.hu. Absence during an in-flight request is not sufficient evidence. The system keeps reservations until the operator explicitly resolves the outcome.
7. Confirm the business's fulfillment-date, document-type and foreign-exchange policies. The current client flow sets fulfillment to period end, uses quantity one for a full fixed-fee month, and defaults to paper invoice mode (`eszamla=false`). These are application choices, not accounting-policy validation performed by this audit.
8. Automatic scheduling is not active. Timesheet email attachments remain unavailable; the UI now states this accurately. Existing stored attachment preferences are retained for future support.

The hosted security advisor also reported pre-existing issues outside the invoice changes, including RLS disabled on `public.leave_requests` and mutable search paths on several older functions. Those were not changed by this invoice audit. See the [RLS advisor remediation](https://supabase.com/docs/guides/database/database-linter?lint=0013_rls_disabled_in_public). Invoice ledger tables have RLS enabled; local tests deny unrelated company admins, employees and anonymous callers, including mutation RPCs.

**Provider test-account acceptance sequence**

Use synthetic client and work data, a monitored account contact email, and the dedicated test-account key. Start with email disabled and verify the provider account visibly remains in Tesztüzem.

- Generate and inspect a preview. Verify recipient fields, dates, quantity, unit price, VAT, currency and rounding. Confirm no provider invoice or local ledger finalization occurs.
- Review and issue a small HUF invoice from approved work. Check the exact invoice in Számlázz.hu, stored number/totals, PDF download and work becoming invoiced.
- Double-submit the same review and try an overlapping period. Confirm one provider invoice and one local reservation only.
- Exercise EUR/MNB, exempt VAT, an EU business, a group-tax recipient and a complete fixed-fee month. Inspect each provider PDF rather than relying on HTTP success.
- Test email enabled and disabled. In a [test account](https://tudastar.szamlazz.hu/gyik/teszt-fiok-mukodese), notifications are redirected to the account contact email. This cannot prove delivery to the actual customer address, NAV reporting or online payment behavior.
- Inject a response loss after forwarding one test request, then reconcile against the provider's external ID. Confirm no automatic replay, no duplicate invoice and no early timesheet transition. Exercise process interruption and database finalization failure as separate scenarios.
- Verify the operator can resolve confirmed issuance and confirmed non-issuance. Record invoice number, external ID, timestamps and redacted evidence for each case.

Only after those checks and deployment verification should a separately authorized, controlled real invoice be used to prove the production account path.

**Re-running the checks**

Backend: `venv/bin/python -m pytest -q`. The SQL tests are opt-in and require a disposable local PostgreSQL database named `invoice_audit` under `/tmp/chronosync-invoice-audit`, on port 55439. Set `INVOICE_TEST_PG_DSN='host=/tmp/chronosync-invoice-audit port=55439 dbname=invoice_audit'` to include them. The fixture resets its test schemas and refuses nonmatching DSNs; never point it at a real project. The fixture is a focused production-shape reproduction, not a complete Supabase clone.

Frontend: `node --test --test-isolation=none tests/*.test.mjs`, `npm run build`, and ESLint on invoice-related changed files. For component browser checks, run Vite on port 5178 and `node tests/invoice-ui/test.mjs` with Playwright available. The harness contains synthetic data and does not authenticate or contact the provider. These are real component interactions, not a full deployed login-to-provider end-to-end test.
