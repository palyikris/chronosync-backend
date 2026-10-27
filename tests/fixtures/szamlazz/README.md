Official Számlázz.hu request XSD downloaded on 2026-10-26:
https://www.szamlazz.hu/szamla/docs/xsds/agent/xmlszamla.xsd

SHA-256: `90af7504bab00e92bcf84971ed3088d9b7c67dd70219148dabe454e32a3b5498`

The fixture is unchanged. `xmllint --nonet --schema` validates real generated XML
against it, without network access or provider credentials.

Known upstream discrepancy: this XSD omits `csoportazonosito`, although the official
knowledge base documents that field for group tax IDs:
https://tudastar.szamlazz.hu/gyik/vevo-adoszama-szamlan

The fixture was downloaded again on 2026-10-27 and its hash was unchanged. The
documentation's inline XSD also includes the group field. Real test-account
preview and issuance accepted it on that date, with the member's -4- and group's
-5- IDs appearing in the PDF. See `docs/szamlazz-validation-2026-10-27.md`.
The ordinary-recipient XSD tests alone do not establish group acceptance.

The unchanged PDF-query schema `xmlszamlapdf.xsd` was downloaded on 2026-10-27
from https://www.szamlazz.hu/szamla/docs/xsds/agentpdf/xmlszamlapdf.xsd.
SHA-256: `b9b161d1356bcd10791605f74c390a0b2b347fdc19a4cf074f76f8a91fe3cfdf`.
The recovery transport test validates its generated request with `xmllint --nonet`.
