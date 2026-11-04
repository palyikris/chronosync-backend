# Számlázz.hu validation — 27 October 2026

The application's provider path now passes real Számlázz.hu test-account previews and invoice creation, including EUR/MNB, VAT-group recipients and a private-customer rounding boundary. The first real preview reproduced a blocking double-decompression bug that the earlier mocked tests missed. That defect and the additional contract gaps below are fixed locally and covered by regressions.

**The hosted application is not yet cleared for real issuance.** The invoice-hardening migration was subsequently applied and verified on 27 October at the user's request. Remaining requirements are deployment of the current backend/frontend, persistent backend encryption configuration, and an application-level staging-company run. Direct provider validation does not establish those deployment conditions.

## Real provider evidence

The user created a separate billing account named **Teszt cég** under their existing login. We enabled and visibly verified **TESZTÜZEM** on that new, empty account before any invoice creation. No existing real-company account, invoice, NAV connection or provider setting was modified. The user authorized a dedicated Agent key and its retention for future staging work.

The Agent key is saved in the local OS Secret Service keyring, identified by `service=szamlazz.hu` and `account=chronosync-staging-teszt-ceg`. It is not in this repository, skill Markdown, fixtures or reports. The installed `szamlazz-hu` skill retrieves it through `scripts/with_staging_key.py`, injecting `SZAMLAZZ_AGENT_KEY` into a child process without printing it. Keyring retrieval and child-process injection both passed. No production company key was replaced.

Calls used the actual application's `SzamlazzHuProvider`, XML builder, streamed HTTPX transport, response parser and shared amount calculation. They did **not** use the hosted app's authentication, review/reservation API or ledger. Separate local tests exercise those boundaries. All actual requests explicitly disabled recipient email, and invoice amounts were synthetic. The [provider's test-mode description](https://tudastar.szamlazz.hu/gyik/teszt-fiok-mukodese) confirms that these are not accounting documents and do not exercise NAV reporting. [Test-account Agent use is free](https://tudastar.szamlazz.hu/gyik/szamla-agent-automatikus-szamlazas-koltsege).

| Provider check | Result |
| --- | --- |
| HUF business preview | PDF returned; net 250 / gross 318 HUF |
| EUR/MNB business preview | PDF returned; net 250 / gross 317.50 EUR |
| Hungarian VAT-group preview | PDF returned with both member and group identifiers |
| Private HUF invoice | `TESZT-2026-1`; net 4 / gross 5 HUF |
| EUR/MNB invoice | `TESZT-2026-2`; net 250 / gross 317.50 EUR; rendered PDF shows MNB exchange rate and HUF tax reporting amounts |
| VAT-group invoice | `TESZT-2026-3`; net 250 / gross 318 HUF; rendered PDF includes member `-4-` and group `-5-` IDs |
| Private HUF rounding boundary, preview and issuance | `TESZT-2026-4`; quantity 1 × net unit price 1.5 HUF produces net 2 / VAT 0 / gross 2 HUF under gross-based rounding |

Three initial previews plus the rounding-boundary preview and four test invoices succeeded. PDFs were rendered with Poppler and visually inspected; they carry the provider's test/model watermark. Returned and reviewed totals agree. A final dashboard check independently showed all four numbered test invoices while still visibly in Tesztüzem.

The earlier failed calls were previews only; no creation call was replayed after an ambiguous result. The first failed preview reproduced decompression failure. A later EUR preview exposed decimal-comma HTTP headers. Both failures were diagnosed with preview-only requests, reproduced in regressions and fixed before invoice creation.

The synthetic identifiers, test invoice numbers and totals are recorded in [the staging result manifest](szamlazz-staging-results-2026-10-27.json). Generated PDFs remain outside Git in `/tmp/chronosync-szamlazz-provider-smoke/` for this session.

## Defects reproduced and fixed

| Finding | Impact | Fix |
| --- | --- | --- |
| Double decompression | HTTPX `aiter_bytes()` had already decoded the compressed body, but the reconstructed response retained `Content-Encoding`. HTTPX decoded it again and raised `DecodingError`. The real provider preview failed. | Remove the wire encoding and compressed length when rebuilding the bounded response. Gzip and deflate regressions reproduce the old failure and pass with the fix. |
| EUR header representation | A real response sent `szlahu_bruttovegosszeg: 317,5` and XML `317.5`. The strengthened comparison initially rejected the header as an invalid decimal. | Normalize decimal commas in numeric HTTP headers; preserve strict XML numeric parsing. Compare amounts numerically. |
| Contradictory response evidence | Conflicting invoice numbers/totals, error codes, duplicate headers and an error message without a header code could be missed. In particular, XML code 57 plus header code 71 could be treated as a safe rejection and release a reservation despite duplicate evidence. | Validate all known scalar fields and provider header cardinality. Contradictions stay uncertain. Use consistent documented headers when optional XML number/totals are absent. |
| Group recipient validation | A group ID alone, an ordinary `-2-` tax number paired with a group, or a non-domestic group status could pass request validation. | Require domestic status, the member's `-4-` ID and the group's `-5-` ID together. Align frontend readiness and add explanatory labels. |
| Private HUF rounding | The same net-based HUF calculation was applied to businesses and private recipients. | Share calculation between review and XML. For private HUF recipients, derive the gross amount from the saved net price, round gross, calculate/round VAT, then subtract VAT to obtain net. Both offline examples and a real 1.5 HUF boundary case pass. |
| Single-line margin VAT | A persisted `K.AFA` client profile could silently submit the purchase-price line without its required taxable margin line. | Reject margin VAT in this single-line client workflow and mark it incomplete in frontend readiness. The direct provider contract can still represent explicit multi-line requests. |
| Preview error classification | Unconfirmed/invalid responses were described as provider rejections with HTTP 422. | Only explicit rejections map to 422; other provider failures map to 502. |

HTTPX's [automatic response decoding](https://www.python-httpx.org/quickstart/#binary-response-content) explains the transport failure. The official [response contract](https://docs.szamlazz.hu/agent/generating_invoice/response) documents XML v2, optional fields, headers and URL encoding. The decimal-comma observation is additional evidence from an actual EUR response, not an assumption from that documentation.

Recipient checks follow the official [tax-field mapping](https://tudastar.szamlazz.hu/gyik/vevo-adoszama-szamlan). HUF calculations follow [business and private-recipient rounding](https://docs.szamlazz.hu/agent/generating_invoice/settings_and_rules/rounding). The margin limitation follows the documented [two-line K.AFA flow](https://docs.szamlazz.hu/agent/generating_invoice/settings_and_rules/vat-rates).

## Schema and request checks

Downloaded the [current official invoice XSD](https://www.szamlazz.hu/szamla/docs/xsds/agent/xmlszamla.xsd) again. It is byte-for-byte identical to the committed fixture: SHA-256 `90af7504bab00e92bcf84971ed3088d9b7c67dd70219148dabe454e32a3b5498`. Existing tests validate generated XML with `xmllint --nonet --schema` across fifteen language/currency combinations.

The downloadable XSD still omits `csoportazonosito`; the [documentation's inline XSD](https://docs.szamlazz.hu/agent/generating_invoice/xml) and tax-field article include it. Actual group preview and invoice creation accepted the documented field in its documented position. The fixture was not altered to conceal the discrepancy.

The transport method, multipart field, UTF-8 namespace/order, preview flag, explicit email flag, ordinary paper-invoice default, supported currencies/VAT codes and MNB behavior were checked against the official documentation. [Duplicate checking](https://docs.szamlazz.hu/agent/generating_invoice/settings_and_rules/order-number) remains conditional on an account setting; the local ledger remains the primary duplicate guard. Cross-request session reuse remains a performance improvement for this low-volume manual flow.

## Local verification

| Layer | Result |
| --- | --- |
| Full backend regression suite | **183 passed**, including real isolated PostgreSQL tests |
| New provider-contract regressions | **34 passed**, including compressed responses, header conflicts/normalization, group validation, private rounding and real parser-to-issuance state transitions through mocked transport |
| Frontend tests | **48 passed** |
| Frontend production build | Passed |
| ESLint on changed TypeScript/TSX invoice files | Passed |
| Browser component and profile interactions | **14 passed**: confirmation gates, uncertain outcome, no duplicate callback, preview gates, reconciliation errors/recovery, invalid and valid group readiness |
| Skill structure validation | Passed for repository source and installed copy |
| Provider previews / invoices / PDF inspection | Passed as listed above |

The PostgreSQL fixture reproduces the original finalization trigger failure before applying the hardening migration, then tests actual SQL, concurrency, RLS and finalization. It is a focused local reproduction, not a hosted Supabase clone. The browser checks use synthetic local data; the provider checks call the real remote API directly. Neither is a deployed login-to-ledger end-to-end test.

## Hosted readiness and remaining checks

Initial read-only inspection of the hosted ChronoSync database found two companies, both in application preview mode, zero configured provider keys and zero invoice-ledger rows. At that point, `reserve_manual_invoice` lacked the new billing-profile snapshot check, and `check_timesheet_client_active` lacked qualified client-table references.

The user subsequently authorized applying `20261026083353_harden_manual_invoice_consistency.sql` to hosted ChronoSync (`kqihfxhewjgrwoepdsau`). The exact repository SQL was applied successfully, and migration history was aligned to the repository version `20261026083353`. Post-deployment checks confirmed the qualified trigger references, billing-profile and entry snapshot validation, idempotent finalization, enabled reserved-timesheet trigger, restricted RPC permissions and invoice-table RLS. Unauthorized authenticated callers were rejected by the reservation, expiry and finalization functions; anonymous expiry access was denied. These smoke checks ran in a transaction that was rolled back. Ledger rows and active attempts remained zero, and both companies remained in preview mode. No provider calls or invoices were created during migration deployment.

The security advisor no longer reports a mutable search path for `check_timesheet_client_active`. Its only added finding is the expected [authenticated SECURITY DEFINER RPC notice](https://supabase.com/docs/guides/database/database-linter?lint=0029_authenticated_security_definer_function_executable) for the new expiry function, which deliberately requires a signed-in caller and checks company invoice-management authorization. Anonymous execution is revoked. Other pre-existing advisor findings were outside this migration's scope.

Before enabling actual company issuance:

1. Deploy the compatible backend/frontend. The invoice-hardening migration is now applied; the new transport fix is still essential for compressed provider responses, and reservation calls must include the billing-profile snapshot timestamp.
2. Verify persistent backend `ENCRYPTION_MASTER_KEY` secret configuration. The local checkout still has no key configured; hosted backend secrets were not inspected. Preserve any original deployed encryption key needed for existing ciphertext.
3. Connect the dedicated test-account key to a separate staging company in the app and run a full authenticated review → reservation → provider → stored PDF → invoiced-work flow. The key is currently available through the skill's keyring helper, not configured on either hosted company.
4. Exercise a lost creation response and local finalization failure in that staging app, then reconcile using the saved external ID. Do not replay a creation request while an existing outcome is uncertain.
5. If email delivery is required, explicitly authorize a staging email check to the test account contact. This review sent no invoice notifications. Test mode cannot prove real customer delivery, NAV reporting or online payment behavior.
6. Confirm the business choices for fulfillment date, paper/e-invoice mode, exchange-rate policy and VAT treatment. The app uses period end, ordinary paper invoice mode and MNB defaults; provider acceptance is not an accounting-policy approval.

The original [26 October review](szamlazz-validation-2026-10-26.md) retains the earlier concurrency and recovery findings and their release conditions. This follow-up supplies the first actual provider evidence and supersedes its “test-account run pending” status for the direct provider layer only.
