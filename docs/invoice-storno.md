# Invoice storno

Implemented on 4 November 2026 and verified through the deployed staging flow on
5 November, including actual test-account storno E-TESZT-2026-2. See the
[staging verification report](invoice-lifecycle-staging-2026-11-05.md) for the
current release state and evidence. Production applications have not been promoted.

## Administrator flow

In Client billing → Invoice history, an issued invoice has **Storno** and
**Check existing storno** actions. Storno requires the original invoice number,
a reason and explicit confirmation. The backend checks the original document by
its ledger external ID in the company's Számlázz.hu account, including its number,
totals, currency, type and electronic/paper format. Preview mode blocks creation.

The new document uses the original's format and today's Budapest issue date.
The provider supplies the reversal's remaining dates and negative items; the
application does not recalculate them or select new tax treatment. The request
supplies an empty buyer email and does not request customer notification.

One durable request is reserved before the mutation. A confirmed result requires
a distinct storno number, exact negative net/gross amounts and a valid PDF.
Only then does one database transaction record the storno, release the period and
entry reservations, return billed entries to approved and pause automatic
invoicing for the client. This prevents a scheduled run from immediately
recreating the cancelled invoice. An administrator can review corrected billing
and deliberately enable scheduling again.

Original invoice values, PDF, issue date and entry snapshots remain in history.
The storno number, reason, negative totals and PDF are stored alongside them.
Released entry snapshots remain in `invoice_ledger_entries` with `released_at`;
a partial unique index permits a replacement to reserve the same work without
removing the original audit trail. Both original and storno PDFs remain available.
A replacement requires a fresh invoice review and gets a new ledger identity and
provider order number. No replacement is issued automatically by storno.

## Unconfirmed outcomes and external cancellations

A timeout, malformed success, unexpected error, interrupted request or failed
local finalization retains the original invoice and its reservations. Repeating
storno is blocked. After ten minutes, an abandoned request becomes uncertain.
The request ID prevents a late response from finalizing a later retry.

**Check existing storno** never creates a document. It queries the original and
the supplied storno number through Számlázz.hu's XML API, requiring the original
to be cancelled and the document to be type `SS`, reference the original via
`hivszamlaszam`, match its currency/format and exactly negate its amounts. The
PDF query must independently match the number and negative totals. This flow
also records a cancellation made directly in Számlázz.hu, without a previous
Chronosync request.

An uncertain request can be resolved as **No storno was issued** only after a
read-only provider query confirms that the original is active. Its work stays
invoiced and reserved. A subsequent explicit attempt queries the provider again.
There is a five-attempt limit and no automatic mutation retry.

The optional `sztornozott` field is omitted on active originals in the dedicated
staging account; the parser treats omission as false and rejects invalid Boolean
values, duplicate fields/blocks, unsafe XML and contradictory headers.

## Release and verification

For a new installation, apply `supabase/migrations/20261104201736_invoice_storno.sql` after
`20261104194919_recurring_invoice_configuration.sql` and the existing project
pricing/multiplier migrations. Deploy the compatible backend and frontend as a
coordinated release. The ledger has a new `reversed` status, and history requires
the new reversal columns. Existing RLS policies and read-only ledger grants remain;
new administrative RPCs check tenant access and revoke public/anonymous execution.

Checks cover API confirmation and result contracts, official unchanged request
XSD validation, provider transport, uncertain/rejected/interrupted outcomes,
read-only recovery, failed local finalization, concurrent reservation, stale
request IDs, tenant/employee/anonymous restrictions, exact negative totals,
archived clients, automatic scheduling pause, preserved PDFs and snapshots, and
new reviewed reservations for the same period. English/Hungarian browser checks
exercise response loss, reference mismatch, recovery and both PDF downloads.

Initial implementation verification on 4 November: 366 backend/database tests passed;
96 tests requiring other isolated integration environments were skipped. Frontend
build, affected-file ESLint, 77 frontend unit tests, the extended pricing/storno
browser suite and 17 existing invoice-dialog browser checks passed. The mobile
Hungarian confirmation and retained original/storno document history were also
inspected visually.

Supabase security advisors were run against the disposable database. They found
no ledger/storno findings; four RLS findings concern the deliberately minimal
fixture's unrelated clients, projects, timesheets and leave tables. This is not a
hosted-project security assessment.

A real read-only lookup of an existing staging invoice verified the query
namespace, format, amount fields and omitted cancellation flag. On 5 November,
the deployed browser issued E-TESZT-2026-1 and reversed it as E-TESZT-2026-2.
Provider queries, database state and retained downloads passed; the final suite
passed 398 tests with 96 skipped. Email receipt remains pending. See the linked
staging report for the exact deployment scope and evidence.

## Provider sources

- [Reversal request](https://docs.szamlazz.hu/agent/reversing_invoice/request)
- [Reversal XML and XSD](https://docs.szamlazz.hu/agent/reversing_invoice/xml)
- [Reversal response](https://docs.szamlazz.hu/agent/reversing_invoice/response)
- [Invoice XML query](https://docs.szamlazz.hu/agent/querying_xml/response)
- [Invoice query schema](https://www.szamlazz.hu/szamla/docs/xsds/szamla/szamla.xsd)
- [Error handling and retry limits](https://docs.szamlazz.hu/agent/basics/error-handling)
