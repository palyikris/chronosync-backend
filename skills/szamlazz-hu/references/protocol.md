# Protocol facts and official sources

Reviewed 2026-10-27. Recheck the relevant pages before future implementation; provider behavior and account settings can change.

| Area | Contract / implication | Primary source |
| --- | --- | --- |
| Transport | HTTPS POST to `https://www.szamlazz.hu/szamla/`, multipart file field `action-xmlagentxmlfile`, one XML per document. Other operations use different action fields. | [Requests](https://docs.szamlazz.hu/agent/basics/sending-requests), [invoice request](https://docs.szamlazz.hu/agent/generating_invoice/request) |
| Authentication | Agent key in `szamlaagentkulcs`, lowercase. Keys grant the same account-wide permissions and remain valid until deleted. | [Authentication](https://docs.szamlazz.hu/agent/basics/authentication) |
| Request XML | Namespace `http://www.szamlazz.hu/xmlszamla`; element order is fixed. `elonezetpdf` belongs in `fejlec`; `szamlaKulsoAzon` belongs in `beallitasok`. Request `valaszVerzio=2` and `szamlaLetoltes=true` for XML plus base64 PDF. | [XML and XSD](https://docs.szamlazz.hu/agent/generating_invoice/xml), [downloadable XSD](https://www.szamlazz.hu/szamla/docs/xsds/agent/xmlszamla.xsd) |
| Response | Namespace `http://www.szamlazz.hu/xmlszamlavalasz`; status `sikeres`; optional `hibakod`, `hibauzenet`, `szamlaszam`, `szamlanetto`, `szamlabrutto`, `kintlevoseg`, `pdf`. Optional data also appears in `szlahu_*` headers. Header invoice numbers and error messages are URL encoded; amounts are numeric. Compare equivalent decimals, not formatting. | [Response](https://docs.szamlazz.hu/agent/generating_invoice/response) |
| Errors | Explicit XML failure plus error evidence, or the documented error headers, can establish rejection only without contradictory success evidence. Codes 71/152 concern an existing order. Do not endlessly retry; the docs specify at most five attempts at resolving the same issue. | [Errors](https://docs.szamlazz.hu/agent/basics/error-handling) |
| Duplicates | Account-level order-number repetition setting controls protection. With it enabled, a recent matching request can return the existing invoice; otherwise 71/152. External ID alone does not prevent duplicate creation. | [Order-number rules](https://docs.szamlazz.hu/agent/generating_invoice/settings_and_rules/order-number) |
| HUF rounding | B2B: round line net, compute/round VAT, add. B2C: round line gross, compute/round VAT as `gross * rate / (100 + rate)`, subtract for net. Net unit price can remain fractional. Other-currency amounts may be fractional. | [Rounding](https://docs.szamlazz.hu/agent/generating_invoice/settings_and_rules/rounding) |
| Currency | Use a supported code, plus bank/rate for foreign currency. The downloadable XSD explicitly documents that `arfolyamBank=MNB` with omitted `arfolyam` selects the current MNB rate. Verify this on a test account. | [Currencies](https://docs.szamlazz.hu/agent/generating_invoice/settings_and_rules/currencies), [XSD](https://www.szamlazz.hu/szamla/docs/xsds/agent/xmlszamla.xsd) |
| Recipient | `adoalany`: 1 Hungarian tax number, 6 EU business, 7 non-EU business, 0 unknown, -1 no tax number. Group recipients need both the member and group IDs. | [Recipient tax fields](https://tudastar.szamlazz.hu/gyik/vevo-adoszama-szamlan) |
| VAT | Symbolic exemptions/reverse charge are different from numeric rates. Margin VAT uses `K.AFA` purchase-price and 27% margin lines, with category keywords. The legacy single-line margin-base method ends 2027-02-15. `eusAfa` requires appropriate seller/account settings and is not a substitute for item VAT codes. | [VAT rules](https://docs.szamlazz.hu/agent/generating_invoice/settings_and_rules/vat-rates) |
| Email | Always set `sendEmail` explicitly; if omitted with an email address, notification defaults on. Up to five multipart attachments, 2 MB each. Test mode redirects delivery to the account contact. | [Email](https://docs.szamlazz.hu/agent/generating_invoice/settings_and_rules/email-notification) |
| Document type | Ordinary invoice is the default. `eszamla=false` means paper invoice even if a PDF is emailed; e-invoice is a separate choice. Storno uses a separate operation. | [Document types](https://docs.szamlazz.hu/agent/generating_invoice/settings_and_rules/document-types) |
| Cookies | Reuse sessions per credential/account at volume, never share a session jar across tenants. Account-detail changes may need a new session. | [Session cookies](https://docs.szamlazz.hu/agent/basics/session-cookie) |
| Testing | Register a separate empty billing account and enable Tesztüzem before the first invoice. An account already used for real invoices stays separate. Test behavior cannot prove NAV submission or customer email delivery. | [Test setup](https://tudastar.szamlazz.hu/gyik/teszt-api-hozzaferes) |

## Known schema discrepancy

On 2026-10-27, the downloadable invoice XSD still omitted `csoportazonosito` (SHA-256 `90af7504bab00e92bcf84971ed3088d9b7c67dd70219148dabe454e32a3b5498`). The official documentation's inline XSD and recipient-tax knowledge base include it, immediately after `adoszam`. Preserve both observations; ordinary-recipient XSD tests do not prove group acceptance. Check the current sources and the provider's test account for group requests.

Actual preview and invoice creation in the dedicated Teszt cég account accepted that field on 2026-10-27; the PDF showed both identifiers. This closes the provider-acceptance question for the tested mapping without changing the downloadable schema fixture.

## Observed transport details

Actual test-account responses on 2026-10-27 were compressed. HTTPX `aiter_bytes()` already decompresses them, as documented in [HTTPX response handling](https://www.python-httpx.org/quickstart/#binary-response-content). When rebuilding a bounded response, remove the wire `Content-Encoding` and `Content-Length` to avoid a second decompression and preserve all provider header instances.

EUR preview HTTP headers used a decimal comma (`szlahu_bruttovegosszeg: 317,5`) while XML used `317.5`. Normalize the header representation before comparing amounts; this is a live observation in addition to the provider's general “numeric” header documentation.
