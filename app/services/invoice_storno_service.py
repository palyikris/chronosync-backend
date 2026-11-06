"""Explicit invoice reversal, with durable reservations and read-only recovery."""
import asyncio
import base64
import logging
from datetime import date, datetime
from typing import Any, Mapping
from uuid import UUID, uuid4

from fastapi import HTTPException
from postgrest.exceptions import APIError

from app.schemas.invoices import InvoiceHistoryItem
from app.services.client_invoice_service import ClientInvoiceService as Invoices
from app.services.company_service import CompanyCredentialService
from app.services.invoice_providers.szamlazz_hu import (
    SzamlazzHuError, SzamlazzHuProvider, SzamlazzHuRejectedError,
)
from app.services.supabase_service import SupabaseDataService

logger = logging.getLogger(__name__)


class InvoiceStornoService:
    @staticmethod
    async def _rpc(user_jwt: str, name: str, params: dict[str, Any]) -> Any:
        client = await SupabaseDataService.get_authenticated_client(user_jwt)
        try:
            return (await client.rpc(name, params).execute()).data
        except APIError as exc:
            if exc.code == "42501":
                raise HTTPException(403, "You are not authorized to reverse this invoice.") from None
            if exc.code in {"PT409", "40001", "23505", "22023", "54000"}:
                raise HTTPException(409, "The storno state changed or is not eligible. Refresh invoice history before continuing.") from None
            raise
        finally:
            await SupabaseDataService._close_authenticated_client(client)

    @staticmethod
    async def _current(*, user_jwt: str, company_id: UUID, invoice_id: UUID,
                       original_invoice_number: str) -> InvoiceHistoryItem:
        current = await Invoices.get_invoice_history_item(
            user_jwt=user_jwt, company_id=company_id, invoice_id=invoice_id)
        if current.status != "issued" or current.invoice_number != original_invoice_number:
            raise HTTPException(409, "Only the confirmed original issued invoice can be reversed.")
        if current.external_id != f"chronosync-{current.id}":
            raise HTTPException(409, "The invoice external ID does not match its ledger record.")
        return current

    @staticmethod
    def _validate_original(current: InvoiceHistoryItem, document: Mapping[str, Any]) -> None:
        if (document.get("invoice_number") != current.invoice_number
                or document.get("net_total") != current.net_total
                or document.get("gross_total") != current.gross_total
                or document.get("currency") != current.currency
                or document.get("document_type") not in {"SZ", "ES", "VS", "HS"}
                or document.get("format") not in {"1", "2", "3"}):
            raise HTTPException(409, "The provider invoice does not match the original ledger. Its reservation remains unchanged.")

    @staticmethod
    def _validate_result(current: InvoiceHistoryItem, result: Mapping[str, Any],
                         expected_number: str | None = None) -> bytes:
        pdf = result.get("pdf")
        number = Invoices._required_text(result.get("invoice_number"))
        if (not number or number == current.invoice_number
                or (expected_number is not None and number != expected_number)
                or Invoices._decimal_or_none(result.get("net_total")) != -current.net_total
                or Invoices._decimal_or_none(result.get("gross_total")) != -current.gross_total
                or not isinstance(pdf, bytes) or not 0 < len(pdf) <= Invoices.MAX_PDF_BYTES
                or not pdf.startswith(b"%PDF")):
            raise HTTPException(502, "The storno confirmation is incomplete or inconsistent. Check Számlázz.hu and reconcile; do not resend.")
        return pdf

    @classmethod
    async def _mark(cls, *, user_jwt: str, current: InvoiceHistoryItem, request_id: UUID,
                    state: str, code: str) -> None:
        try:
            await cls._rpc(user_jwt, "mark_invoice_storno_attempt", {
                "p_invoice_id": str(current.id), "p_request_id": str(request_id),
                "p_status": state, "p_error_code": code,
            })
        except Exception:
            # The durable 'reversing' state still holds the reservation. History
            # moves abandoned requests to 'uncertain', never back to retryable.
            logger.exception("Could not persist storno result for invoice %s", current.id)

    @classmethod
    async def _finalize(cls, *, user_jwt: str, current: InvoiceHistoryItem,
                        request_id: UUID | None, result: Mapping[str, Any],
                        reason: str, issue_date: date, e_invoice: bool) -> None:
        pdf = cls._validate_result(current, result)
        rows = await cls._rpc(user_jwt, "finalize_invoice_storno", {
            "p_invoice_id": str(current.id),
            "p_request_id": str(request_id) if request_id else None,
            "p_invoice_number": result["invoice_number"],
            "p_net_total": str(result["net_total"]), "p_gross_total": str(result["gross_total"]),
            "p_pdf_base64": base64.b64encode(pdf).decode("ascii"),
            "p_reason": reason, "p_issue_date": issue_date.isoformat(), "p_e_invoice": e_invoice,
        })
        if not (isinstance(rows, list) and len(rows) == 1
                and str(rows[0].get("invoice_id")) == str(current.id)
                and rows[0].get("status") == "reversed"):
            raise HTTPException(502, "Storno finalization was not confirmed. Reconcile from invoice history.")

    @classmethod
    async def reverse(cls, *, user_jwt: str, company_id: UUID, invoice_id: UUID,
                      original_invoice_number: str, reason: str) -> InvoiceHistoryItem:
        current = await cls._current(user_jwt=user_jwt, company_id=company_id,
            invoice_id=invoice_id, original_invoice_number=original_invoice_number)
        if current.reversal and current.reversal.status in {"reversing", "uncertain"}:
            raise HTTPException(409, "A storno attempt already exists. Reconcile it before any retry.")
        await Invoices._assert_live_invoice_mode(user_jwt=user_jwt, company_id=company_id)
        request_id = uuid4()
        reserved = False
        provider_started = False
        try:
            async with CompanyCredentialService.decrypted_szamlazz_agent_key(user_jwt, company_id) as key:
                provider = SzamlazzHuProvider(key)
                document = await provider.query_invoice(external_id=current.external_id)
                cls._validate_original(current, document)
                if document.get("reversed") is not False:
                    raise HTTPException(409, "The original is already reversed or its state is unconfirmed. Record the existing storno from invoice history.")
                issue_date = datetime.now(Invoices.BUDAPEST_TIMEZONE).date()
                e_invoice = document["format"] in {"2", "3"}
                rows = await cls._rpc(user_jwt, "reserve_invoice_storno", {
                    "p_invoice_id": str(invoice_id), "p_request_id": str(request_id),
                    "p_original_invoice_number": original_invoice_number, "p_reason": reason,
                    "p_issue_date": issue_date.isoformat(), "p_e_invoice": e_invoice,
                })
                reserved = True
                if not (isinstance(rows, list) and len(rows) == 1
                        and str(rows[0].get("request_id")) == str(request_id)):
                    raise HTTPException(502, "The storno reservation was not confirmed. Refresh invoice history.")
                provider_started = True
                result = await provider.reverse_invoice(invoice_number=original_invoice_number,
                    issue_date=issue_date, reason=reason, e_invoice=e_invoice)
        except asyncio.CancelledError:
            if reserved:
                await asyncio.shield(cls._mark(user_jwt=user_jwt, current=current, request_id=request_id,
                    state="uncertain", code="interrupted"))
            raise
        except Exception as exc:
            if reserved:
                safe_rejection = isinstance(exc, SzamlazzHuRejectedError) and exc.code in {"3", "53", "54", "57"}
                await cls._mark(user_jwt=user_jwt, current=current, request_id=request_id,
                    state="failed" if safe_rejection or not provider_started else "uncertain",
                    code=exc.code if isinstance(exc, SzamlazzHuRejectedError) and exc.code else "provider_unconfirmed")
            if isinstance(exc, HTTPException):
                raise
            if isinstance(exc, SzamlazzHuError):
                raise HTTPException(502, "The storno outcome could not be confirmed. Refresh history and reconcile before retrying.") from None
            raise
        try:
            await cls._finalize(user_jwt=user_jwt, current=current, request_id=request_id,
                result=result, reason=reason, issue_date=issue_date, e_invoice=e_invoice)
        except asyncio.CancelledError:
            await asyncio.shield(cls._mark(user_jwt=user_jwt, current=current, request_id=request_id,
                state="uncertain", code="finalization_interrupted"))
            raise
        except Exception:
            try:
                recovered = await Invoices.get_invoice_history_item(user_jwt=user_jwt,
                    company_id=company_id, invoice_id=invoice_id)
                if (recovered.status == "reversed" and recovered.reversal
                        and recovered.reversal.invoice_number == result.get("invoice_number")
                        and recovered.reversal.net_total == -current.net_total
                        and recovered.reversal.gross_total == -current.gross_total):
                    return recovered
            except Exception:
                logger.exception("Could not check storno finalization for invoice %s", invoice_id)
            await cls._mark(user_jwt=user_jwt, current=current, request_id=request_id,
                state="uncertain", code="finalization_unconfirmed")
            raise HTTPException(502, "Számlázz.hu may have reversed the invoice. Reconcile from history before continuing.") from None
        return await Invoices.get_invoice_history_item(user_jwt=user_jwt,
            company_id=company_id, invoice_id=invoice_id)

    @classmethod
    async def reconcile(cls, *, user_jwt: str, company_id: UUID, invoice_id: UUID,
                        original_invoice_number: str, reason: str, resolution: str,
                        reversal_invoice_number: str | None) -> InvoiceHistoryItem:
        current = await cls._current(user_jwt=user_jwt, company_id=company_id,
            invoice_id=invoice_id, original_invoice_number=original_invoice_number)
        if current.reversal and current.reversal.status == "reversing":
            raise HTTPException(409, "The storno request is still active. Refresh history after it completes or expires.")
        try:
            async with CompanyCredentialService.decrypted_szamlazz_agent_key(user_jwt, company_id) as key:
                provider = SzamlazzHuProvider(key)
                original = await provider.query_invoice(external_id=current.external_id)
                cls._validate_original(current, original)
                if resolution == "not_reversed":
                    if original.get("reversed") is not False or not current.reversal or current.reversal.status != "uncertain":
                        raise HTTPException(409, "The provider has not confirmed this uncertain attempt as not reversed.")
                    await cls._rpc(user_jwt, "resolve_invoice_storno_not_reversed", {
                        "p_invoice_id": str(invoice_id), "p_request_id": str(current.reversal.id)})
                else:
                    document = await provider.query_invoice(invoice_number=reversal_invoice_number)
                    if (original.get("reversed") is not True
                            or document.get("invoice_number") != reversal_invoice_number
                            or document.get("document_type") != "SS"
                            or document.get("original_invoice_number") != current.invoice_number
                            or document.get("currency") != current.currency
                            or document.get("format") not in {"1", "2", "3"}
                            or (document.get("format") in {"2", "3"}) != (original.get("format") in {"2", "3"})
                            or document.get("net_total") != -current.net_total
                            or document.get("gross_total") != -current.gross_total):
                        raise HTTPException(409, "The provider storno must reference this original invoice and exactly reverse its amounts and currency.")
                    try:
                        issue_date = date.fromisoformat(str(document.get("issue_date")))
                    except ValueError:
                        raise HTTPException(409, "The provider storno date is invalid.") from None
                    result = await provider.get_invoice_pdf(invoice_number=reversal_invoice_number)
                    cls._validate_result(current, result, reversal_invoice_number)
                    await cls._finalize(user_jwt=user_jwt, current=current,
                        request_id=current.reversal.id if current.reversal else None,
                        result=result, reason=reason, issue_date=issue_date,
                        e_invoice=document["format"] in {"2", "3"})
        except SzamlazzHuError:
            raise HTTPException(502, "The provider storno could not be verified. Its reservation remains unchanged.") from None
        return await Invoices.get_invoice_history_item(user_jwt=user_jwt,
            company_id=company_id, invoice_id=invoice_id)

    @classmethod
    async def download_pdf(cls, *, user_jwt: str, company_id: UUID,
                           invoice_id: UUID) -> tuple[bytes, str]:
        current = await Invoices.get_invoice_history_item(user_jwt=user_jwt,
            company_id=company_id, invoice_id=invoice_id)
        if current.status != "reversed" or not current.reversal:
            raise HTTPException(404, "A confirmed storno document was not found.")
        client = await SupabaseDataService.get_authenticated_client(user_jwt)
        try:
            response = await client.table("invoice_ledger").select("reversal_pdf_bytes").eq(
                "id", str(invoice_id)).eq("company_id", str(company_id)).limit(1).execute()
            rows = response.data or []
            pdf = Invoices._decode_bytea(rows[0].get("reversal_pdf_bytes")) if rows else None
            if not pdf or not pdf.startswith(b"%PDF"):
                raise HTTPException(404, "The stored storno PDF is unavailable.")
            safe_number = "".join(c for c in (current.reversal.invoice_number or str(invoice_id)) if c.isalnum() or c in "-_")[:64]
            return pdf, f"storno-{safe_number}.pdf"
        finally:
            await SupabaseDataService._close_authenticated_client(client)
