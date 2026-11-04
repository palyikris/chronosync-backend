# Test Inc invoicing demo — 4 November 2026

Ready in **Test Inc**, under the existing company administrator **krizsamizsa@gmail.com**.
The invoicing module and its required invoice-attachment module are enabled. The
saved **Teszt cég** staging Számlázz.hu key was encrypted by the deployed backend;
manual issuance is enabled. The seller on returned previews is **TESZT - Teszt cég**.

## First invoice to try

Select **Hídpont Consulting Kft. (teszt)**, code **QD6**, and the period
**1–31 October 2026**. All its projects are hourly, so quantities come from approved
time. The expected net amount is **3,101,350 HUF**, VAT **837,365 HUF**, and gross
**3,938,715 HUF**. This also exercises payroll time multipliers.

For the other clients, select the same full October period, enter **1** for
**Könyvelés és havi zárás**, and enter the payroll quantity from this table for
**Bérszámfejtés és TB ügyintézés**. Advisory hours are automatic.

| Code | Fictional client | Monthly quantity | Payroll quantity | Expected net | Expected gross |
| --- | --- | ---: | ---: | ---: | ---: |
| QD1 | DunaPart Digital Kft. (teszt) | 1 month | 18 people | 1,376,600 HUF | 1,748,282 HUF |
| QD2 | Pannon Műhely Kft. (teszt) | 1 month | 42 people | 1,798,600 HUF | 2,284,222 HUF |
| QD3 | ZöldLiget Webshop Kft. (teszt) | 1 month | 12 people | 1,590,400 HUF | 2,019,808 HUF |
| QD4 | Körút Gasztro Kft. (teszt) | 1 month | 9 people | 1,132,000 HUF | 1,437,640 HUF |
| QD5 | Danube Ventures GmbH (teszt) | 1 month | 26 people | 5,270.50 EUR | 5,270.50 EUR |

QD5 is an English-language, fictional EU business scenario using the EUFAD37 tax
code. These sample tax and pricing settings apply only to the demo.

## Added sample data

- 8 fictional regular employees, including 30-hour and 20-hour contracts.
- 6 fictional clients with recipient addresses, synthetic tax identifiers,
  payment terms, billing language and billing email addresses at example.com.
- 18 projects, each with complete 2026 project pricing and a sample purchase order.
- 576 entries spanning 1 October–4 November 2026, totaling 1,392 logged hours.
- 501 approved October entries totaling 1,213.5 hours, ready for invoicing.
- 50 submitted, 24 draft and 1 rejected entry for approval-flow testing.

Bookkeeping, payroll, tax advisory, client reconciliations and monthly reporting
model an Ecovis-style accounting practice. Everything added is marked as test
data. The existing 2 people, 4 clients, 10 projects and 364 time entries were kept.
No changes were made to the real Ecovis tenant.

The fictional employee profiles appear in the team and time reports, but their
authentication accounts are blocked and have no usable passwords. The temporary
verification administrator role and session were removed. The existing real
administrator's login was not changed.

## Verified

All six clients passed deployed invoice review, approved-entry selection, expected
amount checks and actual Számlázz.hu PDF preview generation. The HUF and EUR PDFs
were visually inspected. The final database check confirmed the saved key,
enabled modules, manual issuance gate and disabled demo-account access.

No invoice was issued, no ledger rows were fabricated, and no email was sent.
Automatic invoicing and invoice emails are disabled on all six sample clients.
The application preview gate is separate from the provider account's Tesztüzem
setting; this setup uses the saved dedicated staging account key.

Machine-readable evidence: [test-inc-invoicing-demo-2026-11-04.json](test-inc-invoicing-demo-2026-11-04.json).
Provider preview PDFs: [test-inc-invoicing-demo-previews](test-inc-invoicing-demo-previews).
The fixture script is guarded to this exact company and administrator and uses
deterministic IDs so interrupted setup does not duplicate the sample records.
