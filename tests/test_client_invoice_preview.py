import asyncio
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import UUID

from app.api.v1.endpoints.invoices import preview_client_invoice
from app.schemas.invoices import ClientInvoicePreviewRequest, InvoiceReviewProjectLine
from app.services.client_invoice_service import ClientInvoiceService
from app.services.company_service import CompanyCredentialService
from app.services.invoice_providers.szamlazz_hu import SzamlazzHuProvider


COMPANY_ID = UUID("10000000-0000-0000-0000-000000000001")
CLIENT_ID = UUID("20000000-0000-0000-0000-000000000002")


class FakeResponse:
    def __init__(self, data):
        self.data = data


class FakeTimesheetQuery:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.requested_limit = len(rows)
        self.cursor_id = None

    def __getattr__(self, method_name):
        def record(*args, **kwargs):
            self.calls.append((method_name, args, kwargs))
            if method_name == "limit":
                self.requested_limit = args[0]
            if method_name == "gt" and args[0] == "id":
                self.cursor_id = str(args[1])
            return self

        return record

    async def execute(self):
        rows = self.rows
        if self.cursor_id is not None:
            rows = [row for row in rows if str(row["id"]) > self.cursor_id]
        return FakeResponse(rows[: self.requested_limit])


class FakeTimesheetClient:
    def __init__(self, query):
        self.query = query

    def table(self, table_name):
        assert table_name == "timesheets"
        return self.query


def billing_profile(**overrides):
    profile = {
        "billing_name": "Minta Kft.",
        "billing_country_code": "HU",
        "billing_postal_code": "1111",
        "billing_city": "Budapest",
        "billing_address": "Teszt utca 1.",
        "tax_subject": 1,
        "tax_number": "12345678-4-41",
        "eu_vat_number": None,
        "group_tax_number": "17781228-5-44",
        "billing_email": "billing@example.test",
        "send_invoice_email": True,
        "invoice_language": "hu",
        "currency": "HUF",
        "payment_method": "Átutalás",
        "payment_term_days": 8,
        "billing_model": "fixed_monthly",
        "net_unit_price": "250000",
        "invoice_unit": "hónap",
        "vat_code": "AAM",
        "invoice_item_name": "Havi szolgáltatási díj",
    }
    profile.update(overrides)
    return profile


async def build_from_profile(profile, quantity=Decimal("1")):
    # These explicit project lines test provider/recipient conversion. Integration
    # tests cover reading authoritative rates and approved project hours.
    line = InvoiceReviewProjectLine(project_id=UUID(int=3), project_name="Monthly service",
        pricing_year=2026, pricing_updated_at="2026-10-28T00:00:00Z", quantity=quantity,
        unit="hó", net_unit_price=Decimal("250000"), vat_code=profile["vat_code"],
        currency="HUF", net_total=Decimal("250000"), vat_total=0, gross_total=Decimal("250000"))
    return ClientInvoiceService._build_invoice_request(company_id=COMPANY_ID, client_id=CLIENT_ID,
        period_start=date(2026,9,1), period_end=date(2026,9,30), client={"client_code":"MINTA"},
        profile=profile, lines=[line], preview=True)


def test_project_preview_preserves_recipient_and_never_emails():
    invoice, client_code = asyncio.run(build_from_profile(billing_profile(net_unit_price="999999")))
    assert client_code == "MINTA"
    assert invoice.customer.name == "Minta Kft."
    assert invoice.customer.group_tax_number == "17781228-5-44"
    assert invoice.customer.send_email is False
    assert invoice.items[0].quantity == Decimal("1")
    assert invoice.items[0].net_unit_price == Decimal("250000")
    assert invoice.items[0].vat_rate == "AAM"
    assert invoice.preview is True and invoice.download_pdf is True


def test_invoice_builder_uses_resolved_project_quantity_instead_of_client_defaults():
    invoice, _ = asyncio.run(build_from_profile(billing_profile(), Decimal("17.75")))
    assert invoice.items[0].quantity == Decimal("17.75")


def test_hourly_quantity_query_is_company_client_period_and_status_scoped():
    query = FakeTimesheetQuery(
        [
            {
                "id": "10000000-0000-0000-0000-000000000001",
                "hours_logged": "1.25",
            },
            {
                "id": "20000000-0000-0000-0000-000000000002",
                "hours_logged": "2.50",
            },
        ]
    )

    with patch.object(ClientInvoiceService, "PAGE_SIZE", 1):
        quantity = asyncio.run(
            ClientInvoiceService._fetch_approved_hours(
                FakeTimesheetClient(query),
                company_id=COMPANY_ID,
                client_id=CLIENT_ID,
                period_start=date(2026, 9, 1),
                period_end=date(2026, 9, 30),
            )
        )

    assert quantity == Decimal("3.75")
    assert ("eq", ("company_id", str(COMPANY_ID)), {}) in query.calls
    assert ("eq", ("client_id", str(CLIENT_ID)), {}) in query.calls
    assert ("eq", ("status", "approved"), {}) in query.calls
    assert ("gte", ("work_date", "2026-09-01"), {}) in query.calls
    assert ("lte", ("work_date", "2026-09-30"), {}) in query.calls
    assert [call[1] for call in query.calls if call[0] == "limit"] == [
        (1,),
        (1,),
        (1,),
    ]


def test_client_preview_endpoint_downloads_pdf_without_issuing_or_emailing():
    request = ClientInvoicePreviewRequest(
        company_id=COMPANY_ID,
        client_id=CLIENT_ID,
        period_start="2026-09-01",
        period_end="2026-09-30",
    )
    invoice, _ = asyncio.run(build_from_profile(billing_profile()))

    @asynccontextmanager
    async def fake_api_key(*_args, **_kwargs):
        yield "agent-key"

    build_preview = AsyncMock(return_value=(invoice, "MINTA-01"))
    create_invoice = AsyncMock(return_value={"pdf": b"%PDF-preview"})

    with (
        patch.object(
            ClientInvoiceService,
            "build_preview_invoice",
            build_preview,
        ),
        patch.object(
            CompanyCredentialService,
            "decrypted_szamlazz_agent_key",
            fake_api_key,
        ),
        patch.object(SzamlazzHuProvider, "create_invoice", create_invoice),
    ):
        response = asyncio.run(preview_client_invoice(request, token="fake-jwt"))

    submitted = create_invoice.await_args.args[0]
    assert submitted["preview"] is True
    assert submitted["download_pdf"] is True
    assert submitted["customer"]["send_email"] is False
    assert response.body == b"%PDF-preview"
    assert response.headers["content-disposition"] == (
        'attachment; filename="invoice-preview-MINTA01-2026-09-01-2026-09-30.pdf"'
    )


def test_invoice_builder_refuses_missing_project_lines():
    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as raised:
        ClientInvoiceService._build_invoice_request(company_id=COMPANY_ID,client_id=CLIENT_ID,
            period_start=date(2026,9,1),period_end=date(2026,9,30),client={"client_code":"MINTA"},
            profile=billing_profile(),lines=[],preview=False)
    assert raised.value.status_code==409
