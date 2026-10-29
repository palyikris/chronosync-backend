---
name: szamlazz-hu
description: Implement, audit, debug, or validate Számlázz.hu Számla Agent integrations, including invoice XML, PDF responses, recipient tax fields, rounding, previews, duplicate protection, and test-account setup. Use for Számlázz.hu API work; this does not select a tax treatment for the business.
---

# Számlázz.hu / Számla Agent

Read the current official documentation for the operation being changed. The starting point is [Számla Agent](https://docs.szamlazz.hu/agent). Read [the protocol reference](references/protocol.md) for the important request/response invariants and source links. For ChronoSync, also read [the application map](references/chronosync.md).

## Work safely with invoices

- Distinguish a PDF preview (`elonezetpdf=true`) from a provider account in **Tesztüzem**. The API endpoint is the same for test and real accounts. An application's own “test mode” flag cannot change the provider account's mode.
- For staging, use a separate billing account. Verify its selected company and visible **TESZTÜZEM** status before issuing test invoices. Keep a real invoicing account in its existing mode. Existing users can register an additional, empty billing account under their login, then activate Tesztüzem before its first invoice; NAV linking is not needed for this test flow.
- Default previews and synthetic smoke tests to `sendEmail=false`. Test-account email is redirected to the account contact address; it does not establish real customer delivery or NAV reporting.
- Preserve the application's review, reservation, issuance and reconciliation boundaries. Unknown remote outcomes retain reservations; never replay invoice creation merely because the response timed out or local finalization failed.
- Treat error codes `71` and `152` as possible existing invoices. Check the external/order identifier before releasing a reservation. An external ID is a lookup key; order-number duplicate protection depends on provider account settings and matching conditions.
- Use the existing authorization for the requested operation. A saved staging credential is not permission to issue real invoices, change a production account, or send unsolicited email.

## Saved staging credential

The installed skill uses the OS Secret Service keyring for the user-authorized **Teszt cég** staging Agent key. The key is not part of this Markdown, references, or Git. Keyring attributes are `service=szamlazz.hu` and `account=chronosync-staging-teszt-ceg`.

Use the helper without printing the secret:

```bash
python scripts/with_staging_key.py --check
python scripts/with_staging_key.py -- python /path/to/staging_check.py
```

The child receives `SZAMLAZZ_AGENT_KEY` in its environment. Have the runtime consume that value directly; do not echo it, dump the environment, put it in command arguments, or copy it into generated XML fixtures or reports. The user may have to unlock their keyring. If the entry is missing, ask for it to be configured through an appropriate credential UI; do not reuse another company's key. Re-verify provider test mode before creation calls, because account mode and key validity can change.

## Validate the actual contract

- Generate UTF-8, namespaced XML with exact tag case and XSD sequence. Validate representative documents against an unchanged official schema fixture and record its source/date/hash. When official published sources disagree, record the discrepancy and test that case against the dedicated provider account; do not modify the schema to manufacture a pass.
- Use decimal arithmetic. Compare review amounts, outgoing XML, returned totals, stored ledger and the actual rendered PDF. HUF business and private-recipient rounding follow different documented sequences.
- Validate recipient tax status and identifiers together. A Hungarian VAT group needs the member's `-4-` ID in `adoszam` and the group's `-5-` ID in `csoportazonosito`. An EU business uses `adoszamEU`; a non-EU business uses `adoszam`.
- Use explicit VAT codes. A single zero-VAT `K.AFA` line cannot represent a complete margin-scheme invoice; the documented flow splits purchase price and taxable margin. Do not infer OSS settings from an EU address or numeric VAT rate.
- Separate a documented provider rejection from an unconfirmed response. Validate XML namespace/root, scalar field cardinality, Boolean forms, amounts, base64/PDF and response sizes. Compare HTTP headers with XML when both contain a number, total or error code; contradictions are uncertain. Never infer rejection from malformed XML alone.
- Normalize numeric HTTP header decimal commas before comparing them with XML decimal-point amounts. This was observed in actual EUR responses (`317,5` in `szlahu_bruttovegosszeg` versus `317.5` in XML); keep XML number validation strict.
- Test compressed responses through the actual HTTP client. HTTPX byte iteration already decompresses gzip/deflate; rebuilding a response with decoded bytes must remove the original `Content-Encoding` and compressed `Content-Length`. Otherwise a valid provider response fails on a second decompression.
- Exercise a normal success, safe rejection, duplicate, response loss and local finalization failure. Use mocked transport and a disposable database first; then run explicitly authorized provider checks against Tesztüzem. Stop after an uncertain creation result and reconcile it instead of trying again.
- Report exactly which layers passed: schema, transport simulation, real database, browser UI, provider preview, provider invoice, email, deployed app. Local tests alone cannot establish hosted readiness.
