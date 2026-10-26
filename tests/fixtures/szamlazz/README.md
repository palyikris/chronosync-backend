Official Számlázz.hu request XSD downloaded on 2026-10-26:
https://www.szamlazz.hu/szamla/docs/xsds/agent/xmlszamla.xsd

SHA-256: `90af7504bab00e92bcf84971ed3088d9b7c67dd70219148dabe454e32a3b5498`

The fixture is unchanged. `xmllint --nonet --schema` validates real generated XML
against it, without network access or provider credentials.

Known upstream discrepancy: this XSD omits `csoportazonosito`, although the official
knowledge base documents that field for group tax IDs:
https://tudastar.szamlazz.hu/gyik/vevo-adoszama-szamlan

Group-tax requests therefore need a provider test-account check; do not treat the
XSD tests for ordinary recipients as proof of group-tax request acceptance.
