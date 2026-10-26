import asyncio
import base64
from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from xml.etree import ElementTree

import httpx
import pytest

from app.api.v1.endpoints.invoices import preview_invoice
from app.schemas.invoices import InvoiceCreateRequest, InvoicePreviewRequest
from app.services.company_service import CompanyCredentialService
from app.services.invoice_providers.szamlazz_hu import (
    SzamlazzHuProvider,
    SzamlazzHuRejectedError,
)


def invoice_payload(**overrides):
    payload = {
        "issue_date": "2026-10-22",
        "fulfillment_date": "2026-10-31",
        "due_date": "2026-11-08",
        "payment_method": "Átutalás",
        "currency": "HUF",
        "language": "hu",
        "order_number": "chronosync-2026-10-client-1",
        "external_id": "invoice-attempt-1",
        "preview": True,
        "customer": {
            "name": "Minta Kft.",
            "country": "Magyarország",
            "postal_code": "1111",
            "city": "Budapest",
            "address": "Teszt utca 1.",
            "email": "billing@example.test",
            "send_email": False,
            "tax_subject": 1,
            "tax_number": "12345678-2-41",
        },
        "items": [
            {
                "name": "Software development",
                "quantity": "2.5",
                "unit": "óra",
                "net_unit_price": "10000",
                "vat_rate": "27",
            }
        ],
    }
    payload.update(overrides)
    return payload


def find_text(root, name):
    namespace = {"s": SzamlazzHuProvider.XML_NAMESPACE}
    return root.findtext(f".//s:{name}", namespaces=namespace)


def test_build_invoice_xml_uses_documented_order_values_and_huf_rounding():
    provider = SzamlazzHuProvider("  ABCdef123  ")
    xml = provider.build_invoice_xml(
        InvoiceCreateRequest.model_validate(invoice_payload())
    )
    root = ElementTree.fromstring(xml)

    assert [element.tag.rsplit("}", 1)[-1] for element in root] == [
        "beallitasok",
        "fejlec",
        "elado",
        "vevo",
        "tetelek",
    ]
    assert find_text(root, "szamlaagentkulcs") == "abcdef123"
    assert find_text(root, "valaszVerzio") == "2"
    assert find_text(root, "elonezetpdf") == "true"
    assert find_text(root, "nettoErtek") == "25000"
    assert find_text(root, "afaErtek") == "6750"
    assert find_text(root, "bruttoErtek") == "31750"


def test_build_invoice_xml_supports_group_tax_number_and_exempt_vat_code():
    payload = invoice_payload()
    payload["customer"] = {
        **payload["customer"],
        "group_tax_number": "17781228-5-44",
    }
    payload["items"] = [
        {
            **payload["items"][0],
            "vat_rate": "AAM",
        }
    ]

    provider = SzamlazzHuProvider("agent-key")
    xml = provider.build_invoice_xml(InvoiceCreateRequest.model_validate(payload))
    root = ElementTree.fromstring(xml)

    assert find_text(root, "csoportazonosito") == "17781228-5-44"
    assert find_text(root, "afakulcs") == "AAM"
    assert find_text(root, "afaErtek") == "0"
    assert find_text(root, "bruttoErtek") == "25000"


def test_create_invoice_posts_one_multipart_request_and_parses_xml_v2():
    pdf = b"%PDF-test"
    response_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <xmlszamlavalasz xmlns="{SzamlazzHuProvider.RESPONSE_NAMESPACE}">
      <sikeres>true</sikeres>
      <szamlaszam>PREVIEW-1</szamlaszam>
      <szamlanetto>25000</szamlanetto>
      <szamlabrutto>31750</szamlabrutto>
      <kintlevoseg>31750</kintlevoseg>
      <pdf>{base64.b64encode(pdf).decode()}</pdf>
    </xmlszamlavalasz>""".encode()
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url == httpx.URL(SzamlazzHuProvider.API_URL)
        assert SzamlazzHuProvider.FORM_FIELD.encode() in request.content
        assert b"invoice.xml" in request.content
        return httpx.Response(200, content=response_xml)

    async def run_request():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            provider = SzamlazzHuProvider("agent-key", http_client=client)
            return await provider.create_invoice(invoice_payload())

    result = asyncio.run(run_request())

    assert len(requests) == 1
    assert result["invoice_number"] == "PREVIEW-1"
    assert result["net_total"] == Decimal("25000")
    assert result["gross_total"] == Decimal("31750")
    assert result["pdf"] == pdf
    assert result["preview"] is True


def test_provider_surfaces_agent_error_code_without_retrying():
    response_xml = f"""<xmlszamlavalasz xmlns="{SzamlazzHuProvider.RESPONSE_NAMESPACE}">
      <sikeres>false</sikeres>
      <hibakod>57</hibakod>
      <hibauzenet>XML beolvasási hiba.</hibauzenet>
    </xmlszamlavalasz>""".encode()
    call_count = 0

    def handler(_request):
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, content=response_xml)

    async def run_request():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            provider = SzamlazzHuProvider("agent-key", http_client=client)
            return await provider.create_invoice(invoice_payload())

    with pytest.raises(SzamlazzHuRejectedError) as exc_info:
        asyncio.run(run_request())

    assert exc_info.value.code == "57"
    assert "XML beolvasási hiba" in str(exc_info.value)
    assert call_count == 1


def test_preview_endpoint_forces_safe_preview_and_disables_customer_email():
    @asynccontextmanager
    async def fake_api_key(*_args, **_kwargs):
        yield "agent-key"

    create_invoice = AsyncMock(return_value={"pdf": b"%PDF-preview"})
    request = InvoicePreviewRequest(
        company_id="10000000-0000-0000-0000-000000000001",
        invoice=invoice_payload(
            preview=False,
            customer={
                **invoice_payload()["customer"],
                "send_email": True,
            },
        ),
    )

    with (
        patch.object(
            CompanyCredentialService,
            "decrypted_szamlazz_agent_key",
            fake_api_key,
        ),
        patch.object(SzamlazzHuProvider, "create_invoice", create_invoice),
    ):
        response = asyncio.run(preview_invoice(request, token="fake-jwt"))

    submitted = create_invoice.await_args.args[0]
    assert submitted["preview"] is True
    assert submitted["download_pdf"] is True
    assert submitted["customer"]["send_email"] is False
    assert response.body == b"%PDF-preview"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize('body', [
    b'<html><body>Gateway error</body></html>',
    b'<xmlszamlavalasz><sikeres>false</sikeres><hibakod>57</hibakod></xmlszamlavalasz>',
    f'<xmlszamlavalasz xmlns="{SzamlazzHuProvider.RESPONSE_NAMESPACE}"/>'.encode(),
    f'<xmlszamlavalasz xmlns="{SzamlazzHuProvider.RESPONSE_NAMESPACE}"><sikeres>maybe</sikeres></xmlszamlavalasz>'.encode(),
    f'<xmlszamlavalasz xmlns="{SzamlazzHuProvider.RESPONSE_NAMESPACE}"><sikeres>false</sikeres></xmlszamlavalasz>'.encode(),
    f'<xmlszamlavalasz xmlns="{SzamlazzHuProvider.RESPONSE_NAMESPACE}"><sikeres>true</sikeres><sikeres>false</sikeres></xmlszamlavalasz>'.encode(),
    f'<xmlszamlavalasz xmlns="{SzamlazzHuProvider.RESPONSE_NAMESPACE}"><sikeres>false</sikeres><hibakod>57</hibakod><szamlaszam>EXISTS-1</szamlaszam></xmlszamlavalasz>'.encode(),
    b'<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///etc/passwd">]><x>&secret;</x>',
    b'<broken', b'',
])
def test_unconfirmed_responses_are_never_safe_rejections(body):
    from app.services.invoice_providers.szamlazz_hu import SzamlazzHuResponseError
    with pytest.raises(SzamlazzHuResponseError) as error:
        SzamlazzHuProvider.parse_response(httpx.Response(200,content=body),preview=False)
    assert not isinstance(error.value,SzamlazzHuRejectedError)


def response_xml(fields):
    return f'<xmlszamlavalasz xmlns="{SzamlazzHuProvider.RESPONSE_NAMESPACE}">{fields}</xmlszamlavalasz>'.encode()


@pytest.mark.parametrize('success',['true','1'])
def test_xsd_boolean_success_values_are_supported(success):
    result=SzamlazzHuProvider.parse_response(httpx.Response(200,content=response_xml(f'<sikeres>{success}</sikeres>')),preview=True)
    assert result.preview is True


@pytest.mark.parametrize('amount',['NaN','Infinity','-Infinity','garbage'])
def test_nonfinite_or_invalid_provider_amounts_are_uncertain(amount):
    from app.services.invoice_providers.szamlazz_hu import SzamlazzHuResponseError
    with pytest.raises(SzamlazzHuResponseError):
        SzamlazzHuProvider.parse_response(httpx.Response(200,content=response_xml(f'<sikeres>true</sikeres><szamlanetto>{amount}</szamlanetto>')),preview=False)


@pytest.mark.parametrize('pdf',['@@@','bm90IHBkZg=='])
def test_corrupt_base64_or_non_pdf_response_is_uncertain(pdf):
    from app.services.invoice_providers.szamlazz_hu import SzamlazzHuResponseError
    with pytest.raises(SzamlazzHuResponseError):
        SzamlazzHuProvider.parse_response(httpx.Response(200,content=response_xml(f'<sikeres>true</sikeres><pdf>{pdf}</pdf>')),preview=False)


def test_error_header_cannot_contradict_a_success_body():
    from app.services.invoice_providers.szamlazz_hu import SzamlazzHuResponseError
    with pytest.raises(SzamlazzHuResponseError) as error:
        SzamlazzHuProvider.parse_response(httpx.Response(200,headers={'szlahu_error_code':'57'},content=response_xml('<sikeres>true</sikeres><szamlaszam>EXISTS-1</szamlaszam>')),preview=False)
    assert not isinstance(error.value,SzamlazzHuRejectedError)


@pytest.mark.parametrize('code',[400,401,429,500,502,503])
def test_http_failures_are_not_retried(code):
    from app.services.invoice_providers.szamlazz_hu import SzamlazzHuRequestError
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(code,content=b'upstream failure')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await SzamlazzHuProvider('dummy',http_client=client).create_invoice(invoice_payload())
    with pytest.raises(SzamlazzHuRequestError):
        asyncio.run(run())
    assert len(calls)==1


def test_stream_response_size_is_bounded():
    from app.services.invoice_providers.szamlazz_hu import SzamlazzHuResponseError
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,content=b'x'*33))) as client:
            with patch.object(SzamlazzHuProvider,'MAX_RESPONSE_BYTES',32):
                await SzamlazzHuProvider('dummy',http_client=client).create_invoice(invoice_payload())
    with pytest.raises(SzamlazzHuResponseError):
        asyncio.run(run())


@pytest.mark.parametrize('currency',['HUF','EUR','USD'])
@pytest.mark.parametrize('language',['hu','en','de','si','cz'])
def test_generated_request_validates_against_downloaded_official_xsd(currency,language):
    import shutil
    import subprocess
    from pathlib import Path
    if not shutil.which('xmllint'):
        pytest.skip('xmllint is required for official XSD validation')
    payload=invoice_payload(currency=currency,language=language)
    payload['customer']['name']='Árvíztűrő & <Partner> "Kft."'
    schema=Path(__file__).parent/'fixtures/szamlazz/xmlszamla.xsd'
    xml=SzamlazzHuProvider('dummy-key').build_invoice_xml(InvoiceCreateRequest.model_validate(payload))
    result=subprocess.run(['xmllint','--noout','--nonet','--schema',str(schema),'-'],input=xml,capture_output=True)
    assert result.returncode==0,result.stderr.decode()
    root=ElementTree.fromstring(xml)
    assert find_text(root,'nev')==payload['customer']['name']
    assert find_text(root,'arfolyamBank')==('MNB' if currency!='HUF' else None)


@pytest.mark.parametrize('field,value', [('currency','XYZ'),('order_number','   '),('comment','bad\x00value'),('exchange_rate','NaN')])
def test_invalid_invoice_data_fails_before_sending(field,value):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        InvoiceCreateRequest.model_validate(invoice_payload(**{field:value}))


@pytest.mark.parametrize('subject,overrides',[(1,{'tax_number':None}),(6,{'eu_tax_number':None}),(7,{'tax_number':None}),(-1,{'tax_number':'12345678-2-41'})])
def test_recipient_tax_type_requires_consistent_identifiers(subject,overrides):
    from pydantic import ValidationError
    payload=invoice_payload()
    payload['customer']={**payload['customer'],'tax_subject':subject,**overrides}
    with pytest.raises(ValidationError):
        InvoiceCreateRequest.model_validate(payload)


@pytest.mark.parametrize('currency,quantity,price,vat,expected',[
    ('HUF','0.01','50','27',('1','0','1')),
    ('HUF','1','1.5','27',('2','1','3')),
    ('HUF','2.5','1234.5678','27',('3086','833','3919')),
    ('EUR','2.5','19.995','27',('49.99','13.50','63.49')),
    ('HUF','1','100','25.5',('100','26','126')),
    ('HUF','2.5','0.5','AAM',('1','0','1')),
])
def test_review_and_xml_totals_match_independent_rounding_examples(currency,quantity,price,vat,expected):
    from app.services.client_invoice_service import ClientInvoiceService
    payload=invoice_payload(currency=currency)
    payload['items']=[{**payload['items'][0],'quantity':quantity,'net_unit_price':price,'vat_rate':vat}]
    invoice=InvoiceCreateRequest.model_validate(payload)
    expected=tuple(Decimal(value) for value in expected)
    assert ClientInvoiceService._calculate_totals(invoice)==expected
    root=ElementTree.fromstring(SzamlazzHuProvider('dummy-key').build_invoice_xml(invoice))
    actual=tuple(Decimal(find_text(root,name)) for name in ['nettoErtek','afaErtek','bruttoErtek'])
    assert actual==expected
