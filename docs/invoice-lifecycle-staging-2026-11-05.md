# Invoice lifecycle staging verification — 5 November 2026

The deployed staging browser, API, shared database and dedicated Számlázz.hu
test account completed invoice issuance and storno. Email receipt is pending at
the user's request. Production application branches have not been promoted.

## Deployment

| Component | Verified target |
| --- | --- |
| Frontend | `e5dc6ad136583ee8505b3740176e9c004e36b679`, Vercel preview `dpl_bmef6JABYN6VXJHCETKFY8y5B3x8` |
| Backend | `a85aa1bf726c9bb6a7712099269e1316a40ecabb`, Railway deployment `a06269d4-4f99-4ae3-ad27-d9597d72cb47` |
| Branch in both repositories | `invoice-staging-20261105` |
| Frontend URL | https://chronosync-git-invoice-staging-20261105-palyikris-projects.vercel.app |
| Backend URL | https://chronosync-invoice-staging-production.up.railway.app |
| Supabase | `kqihfxhewjgrwoepdsau` |
| Synthetic company | `d2026110-5000-4000-8000-000000000001` |

The backend is a separate service in the existing Railway project's environment
named `production`. Its source is pinned to the tested commit. The production
backend service and Vercel production aliases continue to use their existing
releases. The staging backend has a separate encryption key and exact frontend
origin; the frontend's API override applies only to this preview branch.

The following migrations passed a transaction that was rolled back against the
hosted schema, then were applied together with their original version numbers in
one transaction. An advisory lock, migration replay guard and check for unsettled
invoices protected application. Migration history was read back afterward.

- `20261104194919_recurring_invoice_configuration`
- `20261104201736_invoice_storno`
- `20261104204746_invoice_annex_delivery`

These are shared database schema changes. Old reviews with missing required
reports are rejected and need a compatible app and a new review. Ecovis remains
in preview mode; its recipients, rates and provider configuration were not edited.

## Actual invoice and reversal

The user confirmed **Teszt cég** remains in **TESZTÜZEM** before creation. The
actual provider XML subsequently confirmed `teszt=true`; both PDFs have the
provider's test watermark. No NAV reporting or real customer delivery is claimed.

| Charge | Quantity | Net unit price | Net total, HUF |
| --- | ---: | ---: | ---: |
| Monthly accounting, with no time entries | 1 | 125,000 | 125,000 |
| Monthly payroll | 26 people | 4,000 | 104,000 |
| Hourly advisory | 2.5 hours | 24,000 | 60,000 |

The review, provider PDF and ledger agree on **289,000 HUF net**, **78,030 HUF
VAT**, and **367,030 HUF gross**. The electronic invoice uses issue date
5 November, fulfillment/payment deadline 20 November and service period
1–31 October. Reviewed names, notes, PO references and bank override appear in the
PDF. The two captured time entries include a payroll multiplier of four.

Exactly one browser creation request produced **E-TESZT-2026-1**, ledger
`ec2a110e-abde-497e-b835-080bb3c36a07`. Its order/external identifier is
`chronosync-ec2a110e-abde-497e-b835-080bb3c36a07`. Both time entries became
`invoiced`; the PDF and 7,401-byte report were available in history.

Exactly one browser cancellation request produced **E-TESZT-2026-2**. The
provider independently reports type `SS`, electronic format `3`, a reference to
E-TESZT-2026-1, net **−289,000 HUF** and gross **−367,030 HUF**. The original XML
reports it reversed. The ledger retains the original positive amounts and stores
the separate negative reversal amounts and PDF.

Database checks confirmed both historical reservation entries remain, both have
been released, the two timesheets are `approved` again, and automatic invoicing
for this synthetic client is paused. A fresh replacement review returned the
same charges and both approved entries. No replacement invoice was created.

## Report and browser checks

Review and history downloads returned the same report bytes before and after
storno. Original PDF download after storno also returned the original bytes.
Browser review, issuance, storno and archived downloads ran against the actual
deployed services without HTTP mocks and completed without page errors.

The run found and fixed a real quantity fingerprint bug: a database default such
as `1.0000` and the browser's numeric `1` produced different reviews. Quantities
are now normalized before building invoice lines. The new regression uses the
actual review and workbook generator, passes quantities through the browser's
numeric conversion and verifies the fingerprint and report download remain valid.

Chromium's network detector failed in this execution environment. Firefox reached
the hosted app and initially completed sign-in. Subsequent sign-in form driver
timeouts were avoided in the final runs by loading a fresh, real Supabase fixture
session into the browser. Authorization remained enforced by the deployed app,
API and RLS; this is not a claim that all sign-in browser behavior was retested.

Final validation: **398 backend/database tests passed, 96 skipped** because their
other integration environments were not configured. Frontend **77 tests**, full
ESLint and production build passed. Anonymous execution is denied for every new
public storno/report RPC. Security advisors flag authenticated SECURITY DEFINER
RPCs; their intended tenant-admin checks and restricted grants are covered by
the database suite. Existing unrelated advisor findings remain outside this work.

## Email receipt remains pending

The issued invoice requested email and uploaded the reviewed workbook as
`attachfile1`. A successful invoice response does not prove attachment delivery:
the provider can omit invalid attachments while still sending its notification.
In test mode, notification goes to the account's configured contact address.
See the [official email contract](https://docs.szamlazz.hu/agent/generating_invoice/settings_and_rules/email-notification).

Neither invoice number nor the exact report filename was found in the connected
`palyi.kristof@gmail.com` mailbox. This does not establish delivery failure because
the Teszt cég contact mailbox is unknown. Read-only invoice XML returned only our
synthetic recipient address, not the account contact. The provider browser was
signed out. The user chose to leave email verification pending; account email
settings were not changed and no further invoice/email was sent to investigate.

## Cleanup and evidence

The synthetic company is inactive, its application preview gate is enabled and
its provider credential has been removed. Its administrator's sessions were
revoked with the normal Auth logout endpoint and the login was banned through
5 November 2126. Password sign-in subsequently returned `400 user_banned`.
Temporary passwords, session files and browser authentication artifacts were
removed. The invoice, reversal and audit history are retained. No unsettled
invoice remains in any company. The disposable PostgreSQL server is stopped.

Evidence is in [staging-evidence-2026-11-05](staging-evidence-2026-11-05/):

- [Original invoice](staging-evidence-2026-11-05/E-TESZT-2026-1.pdf)
- [Storno](staging-evidence-2026-11-05/E-TESZT-2026-2.pdf)
- [Exact archived report](staging-evidence-2026-11-05/invoice-annex-ZS1-2026-10-01-2026-10-31.xlsx)
- Hosted review, issued/history and storno screenshots; sanitized provider query results.

| Artifact | SHA-256 |
| --- | --- |
| Original PDF, 377,287 bytes | `3985de525b9baa4feeb7c19634017a0c76862cf06ce8003d469312ad4b7f84c2` |
| Storno PDF, 377,461 bytes | `183d20f0d3a2823e0260e2b0e90e35ac8a04aed3527c89e3c7e68e601ec157e9` |
| Report, 7,401 bytes | `36aa945a5cc8eb33b01fe85a6be8e2e1c8905dfbc2b6887309593ebe4bd88096` |

Production rollout still requires its compatible application releases and the
business-data reconciliation described in the invoice workflow alignment. Currency
conversion remains deferred for company clarification. Email receipt should be
verified in the correct test-account mailbox before relying on provider delivery.
