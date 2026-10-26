import base64
import binascii
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Mapping
from urllib.parse import unquote_plus
from xml.etree.ElementTree import (
    Element,
    ParseError,
    QName,
    SubElement,
    register_namespace,
    tostring,
)

import httpx
from defusedxml.ElementTree import fromstring
from defusedxml.common import DefusedXmlException

from app.schemas.invoices import CreatedInvoice, InvoiceCreateRequest
from app.services.invoice_providers.base import InvoiceProvider


class SzamlazzHuError(RuntimeError):
    """Base error for a failed Számlázz.hu request."""


class SzamlazzHuRequestError(SzamlazzHuError):
    """The Agent endpoint could not be reached successfully."""


class SzamlazzHuResponseError(SzamlazzHuError):
    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class SzamlazzHuRejectedError(SzamlazzHuResponseError):
    """The provider explicitly confirmed that it rejected the invoice."""


class SzamlazzHuProvider(InvoiceProvider):
    """Minimal Számla Agent invoice client using the documented XML v2 response."""

    API_URL = "https://www.szamlazz.hu/szamla/"
    FORM_FIELD = "action-xmlagentxmlfile"
    XML_NAMESPACE = "http://www.szamlazz.hu/xmlszamla"
    RESPONSE_NAMESPACE = "http://www.szamlazz.hu/xmlszamlavalasz"
    XSI_NAMESPACE = "http://www.w3.org/2001/XMLSchema-instance"
    XSD_URL = "https://www.szamlazz.hu/szamla/docs/xsds/agent/xmlszamla.xsd"
    MAX_PDF_BYTES = 25 * 1024 * 1024
    # Base64 expands the PDF by 4/3, plus the XML envelope.
    MAX_RESPONSE_BYTES = 36 * 1024 * 1024

    def __init__(
        self,
        api_key: str,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(api_key.strip().lower())
        self._http_client = http_client

    async def create_invoice(self, invoice: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = InvoiceCreateRequest.model_validate(invoice)
        xml = self.build_invoice_xml(payload)

        if self._http_client is not None:
            response = await self._send(self._http_client, xml)
        else:
            timeout = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await self._send(client, xml)

        result = self.parse_response(response, preview=payload.preview)
        return result.model_dump()

    async def _send(self, client: httpx.AsyncClient, xml: bytes) -> httpx.Response:
        try:
            async with client.stream(
                "POST",
                self.API_URL,
                files={
                    self.FORM_FIELD: (
                        "invoice.xml",
                        xml,
                        "application/xml",
                    )
                },
            ) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > self.MAX_RESPONSE_BYTES:
                        raise SzamlazzHuResponseError("The Számlázz.hu response was too large.")
                    body.extend(chunk)
                return httpx.Response(
                    response.status_code, headers=response.headers,
                    content=bytes(body), request=response.request,
                )
        except httpx.HTTPError as exc:
            raise SzamlazzHuRequestError(
                "Számlázz.hu could not be reached or returned an HTTP error."
            ) from exc

    def build_invoice_xml(self, invoice: InvoiceCreateRequest) -> bytes:
        register_namespace("", self.XML_NAMESPACE)
        register_namespace("xsi", self.XSI_NAMESPACE)

        root = Element(
            QName(self.XML_NAMESPACE, "xmlszamla"),
            {
                QName(self.XSI_NAMESPACE, "schemaLocation"): (
                    f"{self.XML_NAMESPACE} {self.XSD_URL}"
                )
            },
        )

        settings = SubElement(root, QName(self.XML_NAMESPACE, "beallitasok"))
        self._add(settings, "szamlaagentkulcs", self.api_key)
        self._add(settings, "eszamla", invoice.e_invoice)
        self._add(settings, "szamlaLetoltes", invoice.download_pdf)
        self._add(settings, "valaszVerzio", 2)
        if invoice.external_id:
            self._add(settings, "szamlaKulsoAzon", invoice.external_id)

        header = SubElement(root, QName(self.XML_NAMESPACE, "fejlec"))
        self._add(header, "keltDatum", invoice.issue_date.isoformat())
        self._add(header, "teljesitesDatum", invoice.fulfillment_date.isoformat())
        self._add(header, "fizetesiHataridoDatum", invoice.due_date.isoformat())
        self._add(header, "fizmod", invoice.payment_method)
        self._add(header, "penznem", invoice.currency)
        self._add(header, "szamlaNyelve", invoice.language)
        if invoice.comment:
            self._add(header, "megjegyzes", invoice.comment)
        if invoice.currency != "HUF":
            self._add(header, "arfolyamBank", invoice.exchange_rate_bank)
            if invoice.exchange_rate is not None:
                self._add(header, "arfolyam", self._decimal_text(invoice.exchange_rate))
        self._add(header, "rendelesSzam", invoice.order_number)
        if invoice.invoice_prefix:
            self._add(header, "szamlaszamElotag", invoice.invoice_prefix)
        self._add(header, "elonezetpdf", invoice.preview)

        SubElement(root, QName(self.XML_NAMESPACE, "elado"))

        customer = invoice.customer
        buyer = SubElement(root, QName(self.XML_NAMESPACE, "vevo"))
        self._add(buyer, "nev", customer.name)
        if customer.country:
            self._add(buyer, "orszag", customer.country)
        self._add(buyer, "irsz", customer.postal_code)
        self._add(buyer, "telepules", customer.city)
        self._add(buyer, "cim", customer.address)
        if customer.email:
            self._add(buyer, "email", customer.email)
        self._add(buyer, "sendEmail", customer.send_email)
        self._add(buyer, "adoalany", customer.tax_subject)
        if customer.tax_number:
            self._add(buyer, "adoszam", customer.tax_number)
        if customer.group_tax_number:
            self._add(buyer, "csoportazonosito", customer.group_tax_number)
        if customer.eu_tax_number:
            self._add(buyer, "adoszamEU", customer.eu_tax_number)

        items = SubElement(root, QName(self.XML_NAMESPACE, "tetelek"))
        quantum = Decimal("1") if invoice.currency == "HUF" else Decimal("0.01")
        for line in invoice.items:
            net_total = (line.net_unit_price * line.quantity).quantize(
                quantum, rounding=ROUND_HALF_UP
            )
            numeric_vat_rate = (
                Decimal(line.vat_rate)
                if line.vat_rate[0].isdigit()
                else Decimal("0")
            )
            vat_total = (net_total * numeric_vat_rate / Decimal("100")).quantize(
                quantum, rounding=ROUND_HALF_UP
            )
            gross_total = net_total + vat_total

            item = SubElement(items, QName(self.XML_NAMESPACE, "tetel"))
            self._add(item, "megnevezes", line.name)
            self._add(item, "mennyiseg", self._decimal_text(line.quantity))
            self._add(item, "mennyisegiEgyseg", line.unit)
            self._add(
                item,
                "nettoEgysegar",
                self._decimal_text(line.net_unit_price),
            )
            self._add(item, "afakulcs", line.vat_rate)
            self._add(item, "nettoErtek", self._decimal_text(net_total))
            self._add(item, "afaErtek", self._decimal_text(vat_total))
            self._add(item, "bruttoErtek", self._decimal_text(gross_total))
            if line.comment:
                self._add(item, "megjegyzes", line.comment)

        return tostring(root, encoding="utf-8", xml_declaration=True)

    @classmethod
    def parse_response(
        cls, response: httpx.Response, *, preview: bool
    ) -> CreatedInvoice:
        if len(response.content) > cls.MAX_RESPONSE_BYTES:
            raise SzamlazzHuResponseError("The Számlázz.hu response was too large.")

        try:
            root = fromstring(response.content)
        except (ParseError, DefusedXmlException) as exc:
            code = response.headers.get("szlahu_error_code")
            message = response.headers.get("szlahu_error")
            if code and message and not response.headers.get("szlahu_szamlaszam"):
                raise SzamlazzHuRejectedError(
                    unquote_plus(message), code=code
                ) from exc
            raise SzamlazzHuResponseError(
                "Számlázz.hu returned an invalid XML response."
            ) from exc

        if root.tag != f"{{{cls.RESPONSE_NAMESPACE}}}xmlszamlavalasz":
            raise SzamlazzHuResponseError("Számlázz.hu returned an unexpected XML document.")

        def text(name: str) -> str | None:
            nodes = root.findall(f"{{{cls.RESPONSE_NAMESPACE}}}{name}")
            if len(nodes) > 1:
                raise SzamlazzHuResponseError("Számlázz.hu returned duplicate response fields.")
            value = nodes[0].text if nodes else None
            return value.strip() if value and value.strip() else None

        success = text("sikeres")
        code = text("hibakod") or response.headers.get("szlahu_error_code")
        if success in {"false", "0"} and code and not (
            text("szamlaszam") or response.headers.get("szlahu_szamlaszam")
        ):
            raise SzamlazzHuRejectedError(
                text("hibauzenet") or "Számlázz.hu rejected the invoice.",
                code=code,
            )
        if success not in {"true", "1"} or code or text("hibauzenet"):
            raise SzamlazzHuResponseError("The Számlázz.hu outcome could not be confirmed.")

        encoded_pdf = text("pdf")
        pdf: bytes | None = None
        if encoded_pdf:
            try:
                pdf = base64.b64decode("".join(encoded_pdf.split()), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise SzamlazzHuResponseError(
                    "Számlázz.hu returned an invalid PDF payload."
                ) from exc
            if len(pdf) > cls.MAX_PDF_BYTES or not pdf.startswith(b"%PDF"):
                raise SzamlazzHuResponseError("Számlázz.hu returned an invalid PDF payload.")

        return CreatedInvoice(
            invoice_number=text("szamlaszam"),
            net_total=cls._optional_decimal(text("szamlanetto")),
            gross_total=cls._optional_decimal(text("szamlabrutto")),
            outstanding_total=cls._optional_decimal(text("kintlevoseg")),
            customer_account_url=text("vevoifiokurl"),
            pdf=pdf,
            preview=preview,
        )

    @classmethod
    def _add(cls, parent: Element, name: str, value: object) -> None:
        child = SubElement(parent, QName(cls.XML_NAMESPACE, name))
        if isinstance(value, bool):
            child.text = "true" if value else "false"
        else:
            child.text = str(value)

    @staticmethod
    def _decimal_text(value: Decimal) -> str:
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"

    @staticmethod
    def _optional_decimal(value: str | None) -> Decimal | None:
        if value is None:
            return None
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise SzamlazzHuResponseError(
                "Számlázz.hu returned an invalid amount."
            ) from exc
        if not parsed.is_finite():
            raise SzamlazzHuResponseError(
                "Számlázz.hu returned an invalid amount."
            )
        return parsed
