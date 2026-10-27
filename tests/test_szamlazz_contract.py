"""Regressions for the official Számla Agent contract reviewed on 2026-10-27."""

import asyncio
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.v1.endpoints.invoices import _generate_preview_pdf
from app.schemas.invoices import InvoiceCreateRequest
from app.services.client_invoice_service import ClientInvoiceService
from app.services.invoice_providers.szamlazz_hu import (
    SzamlazzHuProvider,
    SzamlazzHuRejectedError,
    SzamlazzHuResponseError,
)
from test_client_invoice_preview import billing_profile, build_from_profile
from test_szamlazz_hu_provider import find_text, invoice_payload, response_xml


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
def test_streamed_compressed_provider_response_is_decoded_once(encoding):
    import base64
    import gzip
    import zlib

    pdf = b"%PDF-compressed-provider-response"
    xml = response_xml(
        "<sikeres>true</sikeres><szamlaszam>TEST-1</szamlaszam>"
        f"<pdf>{base64.b64encode(pdf).decode()}</pdf>",
    )
    compressed = gzip.compress(xml) if encoding == "gzip" else zlib.compress(xml)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=compressed, headers={"Content-Encoding": encoding}),
        )) as client:
            return await SzamlazzHuProvider("dummy", http_client=client).create_invoice(invoice_payload())

    result = asyncio.run(run())
    assert result["pdf"] == pdf
    assert result["invoice_number"] == "TEST-1"


@pytest.mark.parametrize(
    "fields,headers",
    [
        ("<sikeres>true</sikeres>", {"szlahu_error": "XML+error"}),
        (
            "<sikeres>true</sikeres><szamlaszam>A-1</szamlaszam>",
            {"szlahu_szamlaszam": "B-1"},
        ),
        (
            "<sikeres>true</sikeres><szamlanetto>100</szamlanetto>",
            {"szlahu_nettovegosszeg": "101"},
        ),
        (
            "<sikeres>true</sikeres><szamlabrutto>127</szamlabrutto>",
            {"szlahu_bruttovegosszeg": "128"},
        ),
        (
            "<sikeres>false</sikeres><hibakod>57</hibakod>",
            {"szlahu_error_code": "71"},
        ),
        (
            "<sikeres>false</sikeres><hibakod>57</hibakod><szamlanetto>0</szamlanetto>",
            {},
        ),
        (
            "<sikeres>false</sikeres><hibakod>57</hibakod>",
            {"szlahu_bruttovegosszeg": "127"},
        ),
        (
            "<sikeres>true</sikeres>",
            [("szlahu_szamlaszam", "A-1"), ("szlahu_szamlaszam", "B-1")],
        ),
        ("<sikeres>true<unexpected/></sikeres>", {}),
    ],
)
def test_conflicting_provider_evidence_keeps_the_outcome_uncertain(fields, headers):
    with pytest.raises(SzamlazzHuResponseError) as error:
        SzamlazzHuProvider.parse_response(
            httpx.Response(200, content=response_xml(fields), headers=headers),
            preview=False,
        )
    assert not isinstance(error.value, SzamlazzHuRejectedError)


def test_documented_response_headers_supply_optional_xml_fields():
    result = SzamlazzHuProvider.parse_response(
        httpx.Response(
            200,
            content=response_xml("<sikeres>true</sikeres>"),
            headers={
                "szlahu_szamlaszam": "TEST%2F2026%2F1",
                "szlahu_nettovegosszeg": "100.00",
                "szlahu_bruttovegosszeg": "127",
            },
        ),
        preview=False,
    )
    assert result.invoice_number == "TEST/2026/1"
    assert result.net_total == Decimal("100")
    assert result.gross_total == Decimal("127")


def test_provider_decimal_comma_headers_match_xml_decimal_point_amounts():
    result = SzamlazzHuProvider.parse_response(
        httpx.Response(200, content=response_xml(
            "<sikeres>true</sikeres><szamlanetto>250.00</szamlanetto>"
            "<szamlabrutto>317.5</szamlabrutto>",
        ), headers={"szlahu_nettovegosszeg": "250", "szlahu_bruttovegosszeg": "317,5"}),
        preview=True,
    )
    assert result.net_total == Decimal("250")
    assert result.gross_total == Decimal("317.5")


@pytest.mark.parametrize("body", [b"<broken", b"[ERR] XML error"])
def test_non_xml_failure_with_success_headers_is_not_a_safe_rejection(body):
    with pytest.raises(SzamlazzHuResponseError) as error:
        SzamlazzHuProvider.parse_response(
            httpx.Response(200, content=body, headers={
                "szlahu_error_code": "57", "szlahu_error": "XML+error",
                "szlahu_nettovegosszeg": "0",
            }),
            preview=False,
        )
    assert not isinstance(error.value, SzamlazzHuRejectedError)


@pytest.mark.parametrize("code", ["57", "71", "152"])
def test_plain_text_error_headers_retain_provider_code(code):
    with pytest.raises(SzamlazzHuRejectedError) as error:
        SzamlazzHuProvider.parse_response(
            httpx.Response(200, content=b"[ERR] failure", headers={
                "szlahu_error_code": code, "szlahu_error": "XML+beolvas%C3%A1si+hiba.",
            }),
            preview=False,
        )
    assert error.value.code == code
    assert str(error.value) == "XML beolvasási hiba."


@pytest.mark.parametrize("overrides", [
    {"tax_number": None},
    {"tax_number": "12345678-2-41"},
    {"group_tax_number": "17781228-4-44"},
    {"tax_subject": 7},
])
def test_group_tax_recipient_requires_domestic_member_and_group_identifiers(overrides):
    payload = invoice_payload()
    payload["customer"].update({
        "tax_number": "12345678-4-41",
        "group_tax_number": "17781228-5-44",
        **overrides,
    })
    with pytest.raises(ValidationError):
        InvoiceCreateRequest.model_validate(payload)


@pytest.mark.parametrize("quantity,price,vat,expected", [
    ("1", "1.5", "27", ("2", "0", "2")),
    ("1", "2.5", "27", ("2", "1", "3")),
    ("3", "393.66", "27", ("1181", "319", "1500")),
    ("0.01", "50", "27", ("1", "0", "1")),
    ("2.5", "0.5", "AAM", ("1", "0", "1")),
])
def test_huf_private_customer_rounds_from_gross_in_review_and_xml(quantity, price, vat, expected):
    from xml.etree import ElementTree

    payload = invoice_payload()
    payload["customer"].update(tax_subject=-1, tax_number=None)
    payload["items"][0].update(quantity=quantity, net_unit_price=price, vat_rate=vat)
    invoice = InvoiceCreateRequest.model_validate(payload)
    expected = tuple(Decimal(value) for value in expected)
    assert ClientInvoiceService._calculate_totals(invoice) == expected
    root = ElementTree.fromstring(SzamlazzHuProvider("dummy").build_invoice_xml(invoice))
    assert tuple(Decimal(find_text(root, name)) for name in (
        "nettoErtek", "afaErtek", "bruttoErtek",
    )) == expected


def test_single_line_client_profile_cannot_silently_omit_margin_vat():
    with pytest.raises(HTTPException) as error:
        asyncio.run(build_from_profile(billing_profile(vat_code="K.AFA")))
    assert error.value.status_code == 409
    assert "margin" in error.value.detail.lower()


def test_unconfirmed_preview_response_is_a_gateway_failure():
    from unittest.mock import AsyncMock, patch

    from app.services.company_service import CompanyCredentialService
    from test_manual_invoice_issue import fake_agent_key

    with (
        patch.object(CompanyCredentialService, "decrypted_szamlazz_agent_key", fake_agent_key),
        patch.object(SzamlazzHuProvider, "create_invoice", AsyncMock(
            side_effect=SzamlazzHuResponseError("Invalid XML"),
        )),
        pytest.raises(HTTPException) as error,
    ):
        asyncio.run(_generate_preview_pdf(
            token="dummy", company_id="10000000-0000-0000-0000-000000000001",
            invoice=InvoiceCreateRequest.model_validate(invoice_payload()),
        ))
    assert error.value.status_code == 502


@pytest.mark.parametrize("scenario", ["success", "conflicting_codes", "timeout", "duplicate", "rejected"])
def test_issue_flow_runs_real_xml_transport_and_parser_without_replaying(scenario):
    import base64
    from unittest.mock import AsyncMock, patch

    from app.services.company_service import CompanyCredentialService
    from test_manual_invoice_issue import (
        CLIENT_ID, COMPANY_ID, INVOICE_ID, fake_agent_key,
        history_item, invoice_request, invoice_review,
    )

    requests = []
    pdf = b"%PDF-live"

    def handler(request):
        requests.append(request)
        assert b"action-xmlagentxmlfile" in request.content
        assert b"<elonezetpdf>false</elonezetpdf>" in request.content
        assert f"chronosync-{INVOICE_ID}".encode() in request.content
        if scenario == "timeout":
            raise httpx.ReadTimeout("Simulated lost response", request=request)
        if scenario == "success":
            return httpx.Response(200, content=response_xml(
                "<sikeres>true</sikeres><szamlaszam>E-2026-1</szamlaszam>"
                "<szamlanetto>25000</szamlanetto><szamlabrutto>31750</szamlabrutto>"
                f"<pdf>{base64.b64encode(pdf).decode()}</pdf>",
            ), headers={"szlahu_szamlaszam": "E-2026-1", "szlahu_bruttovegosszeg": "31750.00"})
        code = "152" if scenario == "duplicate" else "57"
        return httpx.Response(200, content=response_xml(
            f"<sikeres>false</sikeres><hibakod>{code}</hibakod><hibauzenet>Rejected</hibauzenet>",
        ), headers={"szlahu_error_code": "71"} if scenario == "conflicting_codes" else {})

    finalize = AsyncMock()
    mark_attempt = AsyncMock()
    review = invoice_review()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with (
        patch.object(ClientInvoiceService, "_build_review_context", AsyncMock(return_value=(review, invoice_request()))),
        patch.object(ClientInvoiceService, "_assert_live_invoice_mode", AsyncMock()),
        patch.object(ClientInvoiceService, "_reserve_invoice", AsyncMock(return_value=INVOICE_ID)),
        patch.object(CompanyCredentialService, "decrypted_szamlazz_agent_key", fake_agent_key),
        patch("app.services.invoice_providers.szamlazz_hu.httpx.AsyncClient", return_value=client),
        patch.object(ClientInvoiceService, "_finalize_invoice", finalize),
        patch.object(ClientInvoiceService, "_mark_attempt", mark_attempt),
        patch.object(ClientInvoiceService, "get_invoice_history_item", AsyncMock(return_value=history_item())),
    ):
        issue = ClientInvoiceService.issue_client_invoice(
            user_jwt="dummy", company_id=COMPANY_ID, client_id=CLIENT_ID,
            period_start=review.period_start, period_end=review.period_end,
            review_fingerprint=review.review_fingerprint,
        )
        if scenario == "success":
            result = asyncio.run(issue)
            assert result.status == "issued"
            finalize.assert_awaited_once()
            assert finalize.await_args.kwargs["pdf"] == pdf
            assert finalize.await_args.kwargs["net_total"] == Decimal("25000")
            mark_attempt.assert_not_awaited()
        else:
            with pytest.raises(HTTPException) as error:
                asyncio.run(issue)
            assert error.value.status_code == (422 if scenario == "rejected" else 502)
            finalize.assert_not_awaited()
            mark_attempt.assert_awaited_once()
            assert mark_attempt.await_args.kwargs["attempt_status"] == (
                "failed" if scenario == "rejected" else "uncertain"
            )
    assert len(requests) == 1
