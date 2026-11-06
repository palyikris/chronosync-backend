import base64
import binascii
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence
from urllib.parse import unquote_plus
from uuid import uuid4
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
from app.services.invoice_providers.amounts import calculate_line_totals
from app.services.invoice_providers.base import InvoiceProvider, InvoiceEmailAttachment


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
    PDF_FORM_FIELD = "action-szamla_agent_pdf"
    STORNO_FORM_FIELD = "action-szamla_agent_st"
    STORNO_NAMESPACE = "http://www.szamlazz.hu/xmlszamlast"
    QUERY_FORM_FIELD = "action-szamla_agent_xml"
    QUERY_NAMESPACE = "http://www.szamlazz.hu/xmlszamlaxml"
    DOCUMENT_NAMESPACE = "http://www.szamlazz.hu/szamla"
    PDF_NAMESPACE = "http://www.szamlazz.hu/xmlszamlapdf"
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

    async def create_invoice(self, invoice: Mapping[str, Any], *,
                             attachments: Sequence[InvoiceEmailAttachment] = ()) -> Mapping[str, Any]:
        payload = InvoiceCreateRequest.model_validate(invoice)
        if len(attachments) > 5 or any(not isinstance(item, InvoiceEmailAttachment) for item in attachments):
            raise ValueError("Invalid invoice email attachments.")
        if attachments and (payload.preview or not payload.customer.send_email):
            raise ValueError("Email attachments require live invoice notification.")
        xml = self.build_invoice_xml(payload)

        if self._http_client is not None:
            response = await self._send(self._http_client, xml, attachments=attachments)
        else:
            timeout = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await self._send(client, xml, attachments=attachments)

        result = self.parse_response(response, preview=payload.preview)
        return result.model_dump()

    async def get_invoice_pdf(self, *, external_id: str | None = None,
                              invoice_number: str | None = None) -> Mapping[str, Any]:
        """Look up an existing invoice. This operation cannot create an invoice."""
        if bool(external_id) == bool(invoice_number):
            raise ValueError("Exactly one invoice PDF identifier is required.")
        identifier = external_id or invoice_number or ""
        if not identifier.strip() or len(identifier) > 255:
            raise ValueError("An invoice identifier is required for PDF recovery.")
        root = Element(QName(self.PDF_NAMESPACE, "xmlszamlapdf"))
        fields = [("szamlaagentkulcs", self.api_key)]
        if invoice_number:
            fields.append(("szamlaszam", invoice_number))
        fields.append(("valaszVerzio", "2"))
        if external_id:
            fields.append(("szamlaKulsoAzon", external_id))
        for name, value in fields:
            SubElement(root, QName(self.PDF_NAMESPACE, name)).text = value
        xml = tostring(root, encoding="utf-8", xml_declaration=True)
        if self._http_client is not None:
            response = await self._send(self._http_client, xml, form_field=self.PDF_FORM_FIELD)
        else:
            timeout = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await self._send(client, xml, form_field=self.PDF_FORM_FIELD)
        return self.parse_response(response, preview=False).model_dump()

    async def test_connection(self) -> None:
        """Authenticate using a read-only lookup, even for an empty account.

        The PDF query contract documents code 7 for an unknown external ID:
        https://docs.szamlazz.hu/agent/querying_pdf/response
        Only this specific rejection (or a complete lookup success) proves
        access; HTTP 200 alone does not establish valid credentials.
        """
        async def check(provider: "SzamlazzHuProvider") -> None:
            try:
                result = await provider.get_invoice_pdf(
                    external_id=f"chronosync-connection-test-{uuid4()}"
                )
            except SzamlazzHuRejectedError as exc:
                if exc.code == "7":
                    return
                raise
            if not result.get("invoice_number") or not result.get("pdf"):
                raise SzamlazzHuResponseError(
                    "The Számlázz.hu connection could not be confirmed."
                )

        if self._http_client is not None:
            await check(self)
        else:
            timeout = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                await check(SzamlazzHuProvider(self.api_key, http_client=client))

    async def _request_xml(self, xml: bytes, *, form_field: str) -> httpx.Response:
        if self._http_client is not None:
            return await self._send(self._http_client, xml, form_field=form_field)
        timeout = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await self._send(client, xml, form_field=form_field)

    def build_storno_xml(self, *, invoice_number: str, issue_date: date,
                         reason: str, e_invoice: bool) -> bytes:
        # The original's external ID identifies the original; it must never be
        # reused as an identifier for the newly created reversal document.
        ns = self.STORNO_NAMESPACE
        root = Element(QName(ns, "xmlszamlast"))
        settings = SubElement(root, QName(ns, "beallitasok"))
        for name, value in (("szamlaagentkulcs", self.api_key),
                            ("eszamla", "true" if e_invoice else "false"),
                            ("szamlaLetoltes", "true"), ("valaszVerzio", "2")):
            SubElement(settings, QName(ns, name)).text = value
        header = SubElement(root, QName(ns, "fejlec"))
        for name, value in (("szamlaszam", invoice_number),
                            ("keltDatum", issue_date.isoformat()),
                            ("megjegyzes", reason), ("tipus", "SS")):
            SubElement(header, QName(ns, name)).text = value
        # Email is opt-out: no buyer email is supplied for this operation.
        SubElement(SubElement(root, QName(ns, "vevo")), QName(ns, "email")).text = ""
        return tostring(root, encoding="utf-8", xml_declaration=True)

    async def reverse_invoice(self, *, invoice_number: str, issue_date: date,
                              reason: str, e_invoice: bool) -> Mapping[str, Any]:
        response = await self._request_xml(self.build_storno_xml(
            invoice_number=invoice_number, issue_date=issue_date, reason=reason,
            e_invoice=e_invoice), form_field=self.STORNO_FORM_FIELD)
        return self.parse_response(response, preview=False).model_dump()

    async def query_invoice(self, *, invoice_number: str | None = None,
                            external_id: str | None = None) -> Mapping[str, Any]:
        """Read authoritative type, reference, format and cancellation state."""
        if bool(invoice_number) == bool(external_id):
            raise ValueError("Exactly one invoice query identifier is required.")
        ns = self.QUERY_NAMESPACE
        root = Element(QName(ns, "xmlszamlaxml"))
        SubElement(root, QName(ns, "szamlaagentkulcs")).text = self.api_key
        if invoice_number:
            SubElement(root, QName(ns, "szamlaszam")).text = invoice_number
        SubElement(root, QName(ns, "pdf")).text = "false"
        if external_id:
            SubElement(root, QName(ns, "szamlaKulsoAzon")).text = external_id
        response = await self._request_xml(tostring(root, encoding="utf-8", xml_declaration=True),
                                           form_field=self.QUERY_FORM_FIELD)
        return self.parse_invoice_document(response)

    @classmethod
    def parse_invoice_document(cls, response: httpx.Response) -> Mapping[str, Any]:
        if len(response.content) > cls.MAX_RESPONSE_BYTES:
            raise SzamlazzHuResponseError("The invoice XML is too large.")
        try:
            root = fromstring(response.content)
        except (ParseError, DefusedXmlException) as exc:
            raise SzamlazzHuResponseError("The invoice XML is invalid.") from exc
        if root.tag == f"{{{cls.RESPONSE_NAMESPACE}}}xmlszamlavalasz":
            cls.parse_response(response, preview=False)
            raise SzamlazzHuResponseError("The provider did not return invoice data.")
        if root.tag != f"{{{cls.DOCUMENT_NAMESPACE}}}szamla":
            raise SzamlazzHuResponseError("The provider returned an unexpected invoice document.")
        if any(response.headers.get(key) for key in ("szlahu_hibakod", "szlahu_hibauzenet")):
            raise SzamlazzHuResponseError("The provider returned contradictory invoice data.")
        for path in ("alap", "osszegek", "osszegek/totalossz"):
            nodes = root.findall("/".join(f"{{{cls.DOCUMENT_NAMESPACE}}}{part}" for part in path.split("/")))
            if len(nodes) != 1:
                raise SzamlazzHuResponseError("The invoice XML has missing or duplicate blocks.")
        def field(path: str, *, required: bool = True) -> str | None:
            nodes = root.findall("/".join(f"{{{cls.DOCUMENT_NAMESPACE}}}{part}" for part in path.split("/")))
            if len(nodes) > 1 or (nodes and len(nodes[0])):
                raise SzamlazzHuResponseError("The invoice XML has duplicate or non-scalar fields.")
            value = (nodes[0].text or "").strip() if nodes else None
            if required and not value:
                raise SzamlazzHuResponseError("The invoice XML is incomplete.")
            return value or None
        reversed_value = field("alap/sztornozott", required=False)
        if reversed_value not in {None, "true", "false", "1", "0"}:
            raise SzamlazzHuResponseError("The invoice cancellation flag is invalid.")
        number = field("alap/szamlaszam")
        if response.headers.get("szlahu_szamlaszam") and unquote_plus(response.headers["szlahu_szamlaszam"]) != number:
            raise SzamlazzHuResponseError("The invoice numbers do not match.")
        result = {
            "invoice_number": number,
            "document_type": field("alap/tipus"),
            "original_invoice_number": field("alap/hivszamlaszam", required=False),
            "format": field("alap/eszamla"),
            "issue_date": field("alap/kelt"),
            "currency": field("alap/devizanem"),
            # The optional flag is omitted on active invoices (verified against
            # the dedicated test account); cancelled originals carry true.
            "reversed": reversed_value in {"true", "1"},
            "net_total": cls._optional_decimal(field("osszegek/totalossz/netto")),
            "gross_total": cls._optional_decimal(field("osszegek/totalossz/brutto")),
        }
        for header_name, key in (("szlahu_nettovegosszeg", "net_total"),
                                 ("szlahu_bruttovegosszeg", "gross_total")):
            value = response.headers.get(header_name)
            if value is not None and cls._optional_decimal(value.replace(",", ".")) != result[key]:
                raise SzamlazzHuResponseError("The provider returned conflicting invoice totals.")
        return result

    async def _send(self, client: httpx.AsyncClient, xml: bytes, *, form_field: str | None = None,
                    attachments: Sequence[InvoiceEmailAttachment] = ()) -> httpx.Response:
        files = {(form_field or self.FORM_FIELD): ("invoice.xml", xml, "application/xml")}
        for index, attachment in enumerate(attachments, 1):
            files[f"attachfile{index}"] = (attachment.filename, attachment.content, attachment.media_type)
        try:
            async with client.stream(
                "POST",
                self.API_URL,
                files=files,
            ) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > self.MAX_RESPONSE_BYTES:
                        raise SzamlazzHuResponseError("The Számlázz.hu response was too large.")
                    body.extend(chunk)
                # aiter_bytes() already decompresses the response. Carrying the
                # wire encoding into a new Response would decode that body twice.
                headers = [
                    (name, value) for name, value in response.headers.multi_items()
                    if name.lower() not in {"content-encoding", "content-length"}
                ]
                return httpx.Response(
                    response.status_code, headers=headers,
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

        seller = SubElement(root, QName(self.XML_NAMESPACE, "elado"))
        if invoice.seller_bank_name:
            self._add(seller, "bank", invoice.seller_bank_name)
        if invoice.seller_bank_account:
            self._add(seller, "bankszamlaszam", invoice.seller_bank_account)

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
        for line in invoice.items:
            net_total, vat_total, gross_total = calculate_line_totals(
                line, currency=invoice.currency, tax_subject=customer.tax_subject
            )

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

        def header(name: str) -> str | None:
            values = response.headers.get_list(name)
            if len(values) > 1:
                raise SzamlazzHuResponseError("Számlázz.hu returned duplicate response headers.")
            if not values:
                return None
            return values[0].strip() or None

        header_code = header("szlahu_error_code")
        header_error = header("szlahu_error")
        header_number = header("szlahu_szamlaszam")
        if header_number:
            header_number = unquote_plus(header_number)
        header_net = header("szlahu_nettovegosszeg")
        header_gross = header("szlahu_bruttovegosszeg")
        header_success = any(value is not None for value in (
            header_number, header_net, header_gross,
        ))

        try:
            root = fromstring(response.content)
        except DefusedXmlException as exc:
            raise SzamlazzHuResponseError("Számlázz.hu returned unsafe XML.") from exc
        except ParseError as exc:
            if header_code and header_error and not header_success:
                raise SzamlazzHuRejectedError(
                    unquote_plus(header_error), code=header_code
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
            if nodes and len(nodes[0]):
                raise SzamlazzHuResponseError("Számlázz.hu returned a non-scalar response field.")
            value = nodes[0].text if nodes else None
            return value.strip() if value and value.strip() else None

        fields = {name: text(name) for name in (
            "sikeres", "hibakod", "hibauzenet", "szamlaszam", "szamlanetto",
            "szamlabrutto", "kintlevoseg", "vevoifiokurl", "pdf",
        )}
        success = fields["sikeres"]
        body_code = fields["hibakod"]
        if body_code and header_code and body_code != header_code:
            raise SzamlazzHuResponseError("Számlázz.hu returned conflicting error codes.")
        code = body_code or header_code
        body_success = any(fields[name] is not None for name in (
            "szamlaszam", "szamlanetto", "szamlabrutto", "kintlevoseg", "pdf",
        ))
        if success in {"false", "0"} and code and not (body_success or header_success):
            raise SzamlazzHuRejectedError(
                fields["hibauzenet"] or (
                    unquote_plus(header_error) if header_error
                    else "Számlázz.hu rejected the invoice."
                ),
                code=code,
            )
        if success not in {"true", "1"} or code or fields["hibauzenet"] or header_error:
            raise SzamlazzHuResponseError("The Számlázz.hu outcome could not be confirmed.")

        body_number = fields["szamlaszam"]
        if body_number and header_number and body_number != header_number:
            raise SzamlazzHuResponseError("Számlázz.hu returned conflicting invoice numbers.")

        def amount(name: str, header_value: str | None) -> Decimal | None:
            body_amount = cls._optional_decimal(fields[name])
            # Real Agent responses use a Hungarian decimal comma in HTTP
            # headers, while the XML double uses a decimal point.
            header_amount = cls._optional_decimal(
                header_value.replace(",", ".") if header_value is not None else None
            )
            if (
                body_amount is not None and header_amount is not None
                and body_amount != header_amount
            ):
                raise SzamlazzHuResponseError("Számlázz.hu returned conflicting invoice amounts.")
            return body_amount if body_amount is not None else header_amount

        net_total = amount("szamlanetto", header_net)
        gross_total = amount("szamlabrutto", header_gross)
        encoded_pdf = fields["pdf"]
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
            invoice_number=body_number or header_number,
            net_total=net_total,
            gross_total=gross_total,
            outstanding_total=cls._optional_decimal(fields["kintlevoseg"]),
            customer_account_url=fields["vevoifiokurl"],
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
