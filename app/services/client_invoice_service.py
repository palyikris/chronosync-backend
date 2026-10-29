import asyncio
import base64
import hashlib
import json
import logging
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from postgrest.exceptions import APIError
from pydantic import ValidationError

from app.schemas.invoices import (
    ClientInvoiceReview,
    InvoiceCreateRequest,
    InvoiceHistoryItem,
    InvoiceReviewEntry,
    InvoiceReviewRecipient,
    InvoiceReviewProjectLine,
)
from app.services.company_service import CompanyCredentialService
from app.services.invoice_providers.amounts import calculate_line_totals
from app.services.invoice_providers.szamlazz_hu import (
    SzamlazzHuError,
    SzamlazzHuProvider,
    SzamlazzHuRejectedError,
)
from app.services.supabase_service import SupabaseDataService
from app.services.project_invoice_pricing import build_project_lines


logger = logging.getLogger(__name__)


class ClientInvoiceService:
    PAGE_SIZE = 1000
    MAX_TIMESHEET_ROWS = 50_000
    MAX_PDF_BYTES = 25 * 1024 * 1024
    BUDAPEST_TIMEZONE = ZoneInfo("Europe/Budapest")
    HISTORY_COLUMNS = (
        "id, company_id, client_id, period_start, period_end, status, provider, "
        "external_id, recipient_name, recipient_email, send_email, quantity, unit, "
        "net_unit_price, vat_code, currency, net_total, vat_total, gross_total, "
        "invoice_number, provider_error_code, provider_error_message, attempt_count, "
        "issued_at, created_at, updated_at, pdf_available, clients(name)"
    )

    @classmethod
    async def build_preview_invoice(
        cls,
        *,
        user_jwt: str,
        company_id: UUID,
        client_id: UUID,
        period_start: date,
        period_end: date,
        project_quantities: dict[UUID, Decimal] | None = None,
    ) -> tuple[InvoiceCreateRequest, str]:
        review, invoice = await cls._build_review_context(
            user_jwt=user_jwt, company_id=company_id, client_id=client_id,
            period_start=period_start, period_end=period_end, project_quantities=project_quantities,
        )
        return invoice.model_copy(update={"preview": True,
            "customer": invoice.customer.model_copy(update={"send_email": False})}), review.client_code

    @classmethod
    async def build_invoice_review(
        cls,
        *,
        user_jwt: str,
        company_id: UUID,
        client_id: UUID,
        period_start: date,
        period_end: date,
        project_quantities: dict[UUID, Decimal] | None = None,
    ) -> ClientInvoiceReview:
        review, _ = await cls._build_review_context(
            user_jwt=user_jwt,
            company_id=company_id,
            client_id=client_id,
            period_start=period_start,
            period_end=period_end,
            project_quantities=project_quantities,
        )
        return review

    @classmethod
    async def _build_review_context(
        cls,
        *,
        user_jwt: str,
        company_id: UUID,
        client_id: UUID,
        period_start: date,
        period_end: date,
        project_quantities: dict[UUID, Decimal] | None = None,
    ) -> tuple[ClientInvoiceReview, InvoiceCreateRequest]:
        supabase = await SupabaseDataService.get_authenticated_client(user_jwt)
        try:
            await SupabaseDataService._assert_invoice_attachment_access_with_client(
                supabase, user_jwt=user_jwt, company_id=company_id
            )
            client, profile = await cls._load_client_and_profile(
                supabase, company_id=company_id, client_id=client_id
            )
            cls._validate_profile(profile)
            entries = await cls._fetch_approved_entries(
                supabase,
                company_id=company_id,
                client_id=client_id,
                period_start=period_start,
                period_end=period_end,
            )
            lines = await build_project_lines(supabase, company_id=company_id, client_id=client_id,
                period_start=period_start, period_end=period_end, entries=entries,
                project_quantities=project_quantities or {}, vat_code=str(profile["vat_code"]),
                tax_subject=int(profile["tax_subject"]))

            invoice, client_code = cls._build_invoice_request(
                company_id=company_id,
                client_id=client_id,
                period_start=period_start,
                period_end=period_end,
                client=client,
                profile=profile,
                lines=lines,
                preview=False,
            )
            net_total, vat_total, gross_total = cls._calculate_totals(invoice)
            recipient = InvoiceReviewRecipient(
                name=invoice.customer.name,
                country=invoice.customer.country,
                postal_code=invoice.customer.postal_code,
                city=invoice.customer.city,
                address=invoice.customer.address,
                email=invoice.customer.email,
                send_email=invoice.customer.send_email,
                tax_subject=invoice.customer.tax_subject,
                tax_number=invoice.customer.tax_number,
                eu_tax_number=invoice.customer.eu_tax_number,
                group_tax_number=invoice.customer.group_tax_number,
            )
            review_data = {
                "company_id": company_id,
                "client_id": client_id,
                "client_code": client_code,
                "client_name": str(client.get("name") or client_code),
                "period_start": period_start,
                "period_end": period_end,
                "issue_date": invoice.issue_date,
                "fulfillment_date": invoice.fulfillment_date,
                "due_date": invoice.due_date,
                "payment_method": invoice.payment_method,
                "language": invoice.language,
                "billing_model": "project",
                "item_name": " / ".join(line.project_name for line in lines),
                "quantity": Decimal("1"),
                "unit": "számla",
                "net_unit_price": net_total,
                "vat_code": invoice.items[0].vat_rate,
                "currency": invoice.currency,
                "exchange_rate_bank": (
                    invoice.exchange_rate_bank if invoice.currency != "HUF" else None
                ),
                "billing_profile_updated_at": profile.get("updated_at"),
                "net_total": net_total,
                "vat_total": vat_total,
                "gross_total": gross_total,
                "recipient": recipient,
                "approved_entries": entries,
                "items": lines,
                "project_quantities": {str(key): str(value) for key, value in (project_quantities or {}).items()},
            }
            review = ClientInvoiceReview(
                **review_data,
                review_fingerprint=cls._review_fingerprint(review_data),
            )
            return review, invoice
        finally:
            await SupabaseDataService._close_authenticated_client(supabase)

    @classmethod
    async def issue_client_invoice(
        cls,
        *,
        user_jwt: str,
        company_id: UUID,
        client_id: UUID,
        period_start: date,
        period_end: date,
        review_fingerprint: str,
        project_quantities: dict[UUID, Decimal] | None = None,
    ) -> InvoiceHistoryItem:
        review, invoice = await cls._build_review_context(
            user_jwt=user_jwt,
            company_id=company_id,
            client_id=client_id,
            period_start=period_start,
            period_end=period_end,
            project_quantities=project_quantities,
        )
        if review.review_fingerprint != review_fingerprint:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "The invoice data changed after review. Review the current "
                    "entries and totals before confirming again."
                ),
            )

        await cls._assert_live_invoice_mode(
            user_jwt=user_jwt,
            company_id=company_id,
        )

        invoice_id = await cls._reserve_invoice(user_jwt=user_jwt, review=review)
        live_invoice = invoice.model_copy(
            update={
                "preview": False,
                "download_pdf": True,
                "external_id": f"chronosync-{invoice_id}",
            }
        )
        provider_started = False
        try:
            async with CompanyCredentialService.decrypted_szamlazz_agent_key(
                user_jwt, company_id
            ) as api_key:
                provider_started = True
                provider_result = await SzamlazzHuProvider(api_key).create_invoice(
                    live_invoice.model_dump()
                )
        except HTTPException as exc:
            await cls._mark_attempt(
                user_jwt=user_jwt,
                invoice_id=invoice_id,
                attempt_status="uncertain" if provider_started else "failed",
                error_code=(
                    "provider_response_uncertain" if provider_started
                    else "provider_configuration_error"
                ),
                error_message=(
                    "The provider outcome is uncertain. Reconcile before retrying."
                    if provider_started else "The invoice provider configuration is not usable."
                ),
            )
            if provider_started:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="The provider outcome is uncertain. Do not retry; reconcile from invoice history.",
                ) from exc
            raise exc
        except SzamlazzHuRejectedError as exc:
            # A duplicate rejection proves an invoice may already exist remotely.
            duplicate = exc.code in {"71", "152"}
            await cls._mark_attempt(
                user_jwt=user_jwt,
                invoice_id=invoice_id,
                attempt_status="uncertain" if duplicate else "failed",
                error_code=exc.code or "provider_rejected",
                error_message=(
                    "Számlázz.hu reports an existing invoice for this order. Reconcile before retrying."
                    if duplicate else "Számlázz.hu rejected the invoice request."
                ),
            )
            detail = "Számlázz.hu rejected the live invoice."
            if exc.code:
                detail = f"{detail} Error code: {exc.code}."
            raise HTTPException(
                status_code=(
                    status.HTTP_502_BAD_GATEWAY if duplicate
                    else status.HTTP_422_UNPROCESSABLE_CONTENT
                ),
                detail=detail,
            ) from exc
        except SzamlazzHuError as exc:
            await cls._mark_attempt(
                user_jwt=user_jwt,
                invoice_id=invoice_id,
                attempt_status="uncertain",
                error_code="provider_response_uncertain",
                error_message=(
                    "The provider response was not confirmed. Verify this external "
                    "id in Számlázz.hu before reconciling."
                ),
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    "The provider response is uncertain. Do not retry. Check invoice "
                    f"external id chronosync-{invoice_id} in Számlázz.hu and reconcile "
                    "it from invoice history."
                ),
            ) from exc
        except asyncio.CancelledError:
            try:
                await asyncio.shield(cls._mark_attempt(
                    user_jwt=user_jwt, invoice_id=invoice_id, attempt_status="uncertain",
                    error_code="issuance_interrupted",
                    error_message="Issuance was interrupted. Verify the external id before reconciling.",
                ))
            except Exception:
                logger.error("Could not persist interrupted invoice state for %s", invoice_id)
            raise
        except Exception as exc:
            logger.exception(
                "Unexpected provider failure for invoice attempt %s", invoice_id
            )
            await cls._mark_attempt(
                user_jwt=user_jwt,
                invoice_id=invoice_id,
                attempt_status="uncertain",
                error_code="unexpected_provider_failure",
                error_message=(
                    "The provider outcome could not be confirmed. Verify the external "
                    "id in Számlázz.hu before reconciling."
                ),
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    "The provider outcome could not be confirmed. Do not retry; "
                    "reconcile the attempt from invoice history."
                ),
            ) from exc

        invoice_number = cls._required_text(provider_result.get("invoice_number"))
        provider_net_total = cls._decimal_or_none(provider_result.get("net_total"))
        provider_gross_total = cls._decimal_or_none(provider_result.get("gross_total"))
        pdf = provider_result.get("pdf")
        response_is_confirmed = (
            invoice_number is not None
            and provider_net_total == review.net_total
            and provider_gross_total == review.gross_total
            and isinstance(pdf, bytes)
            and 0 < len(pdf) <= cls.MAX_PDF_BYTES
            and pdf.startswith(b"%PDF")
        )
        if not response_is_confirmed:
            await cls._mark_attempt(
                user_jwt=user_jwt,
                invoice_id=invoice_id,
                attempt_status="uncertain",
                error_code="provider_confirmation_incomplete",
                error_message=(
                    "Számlázz.hu returned an incomplete or inconsistent success "
                    "response. Verify the external id before reconciling."
                ),
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    "Számlázz.hu returned an incomplete confirmation. Do not retry; "
                    "reconcile the attempt from invoice history."
                ),
            )

        try:
            await cls._finalize_invoice(
                user_jwt=user_jwt,
                invoice_id=invoice_id,
                invoice_number=cast(str, invoice_number),
                net_total=cast(Decimal, provider_net_total),
                gross_total=cast(Decimal, provider_gross_total),
                pdf=cast(bytes, pdf),
            )
        except Exception as exc:
            logger.exception(
                "Invoice provider succeeded but database finalization failed for %s",
                invoice_id,
            )
            try:
                recovered = await cls.get_invoice_history_item(
                    user_jwt=user_jwt, company_id=company_id, invoice_id=invoice_id
                )
                if (
                    recovered.status == "issued"
                    and recovered.invoice_number == invoice_number
                    and recovered.net_total == provider_net_total
                    and recovered.gross_total == provider_gross_total
                ):
                    return recovered
            except Exception:
                pass
            try:
                await cls._mark_attempt(
                    user_jwt=user_jwt,
                    invoice_id=invoice_id,
                    attempt_status="uncertain",
                    error_code="database_finalization_failed",
                    error_message=(
                        "The provider created an invoice but local finalization was "
                        "not confirmed. Reconcile this attempt before any retry."
                    ),
                )
            except Exception:
                logger.exception(
                    "Could not persist uncertain state for invoice %s", invoice_id
                )
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    "The live invoice may have been created, but local finalization "
                    "failed. Do not retry; reconcile it from invoice history."
                ),
            ) from exc

        return await cls.get_invoice_history_item(
            user_jwt=user_jwt, company_id=company_id, invoice_id=invoice_id
        )

    @classmethod
    async def list_invoice_history(
        cls,
        *,
        user_jwt: str,
        company_id: UUID,
        limit: int = 50,
    ) -> list[InvoiceHistoryItem]:
        supabase = await SupabaseDataService.get_authenticated_client(user_jwt)
        try:
            await SupabaseDataService._assert_invoice_attachment_access_with_client(
                supabase, user_jwt=user_jwt, company_id=company_id
            )
            await supabase.rpc(
                "expire_stale_manual_invoice_attempts",
                {"p_company_id": str(company_id)},
            ).execute()
            response = await (
                supabase.table("invoice_ledger")
                .select(cls.HISTORY_COLUMNS)
                .eq("company_id", str(company_id))
                .order("created_at", desc=True)
                .limit(max(1, min(limit, 100)))
                .execute()
            )
            rows = response.data or []
            if not isinstance(rows, list):
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Supabase returned an invalid invoice history response.",
                )
            return [
                cls._history_item(cast(dict[str, Any], row))
                for row in rows
                if isinstance(row, dict)
            ]
        finally:
            await SupabaseDataService._close_authenticated_client(supabase)

    @classmethod
    async def get_invoice_history_item(
        cls,
        *,
        user_jwt: str,
        company_id: UUID,
        invoice_id: UUID,
    ) -> InvoiceHistoryItem:
        supabase = await SupabaseDataService.get_authenticated_client(user_jwt)
        try:
            await SupabaseDataService._assert_invoice_attachment_access_with_client(
                supabase, user_jwt=user_jwt, company_id=company_id
            )
            response = await (
                supabase.table("invoice_ledger")
                .select(cls.HISTORY_COLUMNS)
                .eq("id", str(invoice_id))
                .eq("company_id", str(company_id))
                .limit(1)
                .execute()
            )
            rows = response.data or []
            row = rows[0] if isinstance(rows, list) and rows else None
            if not isinstance(row, dict):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Invoice history item was not found.",
                )
            return cls._history_item(cast(dict[str, Any], row))
        finally:
            await SupabaseDataService._close_authenticated_client(supabase)

    @classmethod
    async def get_invoice_pdf(
        cls,
        *,
        user_jwt: str,
        company_id: UUID,
        invoice_id: UUID,
    ) -> tuple[bytes, str]:
        supabase = await SupabaseDataService.get_authenticated_client(user_jwt)
        try:
            await SupabaseDataService._assert_invoice_attachment_access_with_client(
                supabase, user_jwt=user_jwt, company_id=company_id
            )
            response = await (
                supabase.table("invoice_ledger")
                .select("invoice_number, pdf_bytes")
                .eq("id", str(invoice_id))
                .eq("company_id", str(company_id))
                .eq("status", "issued")
                .limit(1)
                .execute()
            )
            rows = response.data or []
            row = rows[0] if isinstance(rows, list) and rows else None
            if not isinstance(row, dict):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Issued invoice was not found.",
                )
            pdf = cls._decode_bytea(row.get("pdf_bytes"))
            if not pdf or not pdf.startswith(b"%PDF"):
                current = await cls.get_invoice_history_item(
                    user_jwt=user_jwt, company_id=company_id, invoice_id=invoice_id,
                )
                pdf = await cls._query_confirmed_pdf(user_jwt=user_jwt, current=current,
                    invoice_number=current.invoice_number)
                try:
                    await supabase.rpc("store_manual_invoice_pdf", {
                        "p_invoice_id": str(invoice_id), "p_invoice_number": current.invoice_number,
                        "p_net_total": str(current.net_total), "p_gross_total": str(current.gross_total),
                        "p_pdf_base64": base64.b64encode(pdf).decode("ascii"),
                    }).execute()
                except APIError as exc:
                    raise HTTPException(409 if str(exc.code) == "PT409" else 502,
                        "The recovered PDF could not be saved. Retry the PDF download.") from None
            number = cls._required_text(row.get("invoice_number")) or str(invoice_id)
            safe_number = "".join(
                char for char in number if char.isalnum() or char in "-_"
            )[:64]
            return pdf, f"invoice-{safe_number or invoice_id}.pdf"
        finally:
            await SupabaseDataService._close_authenticated_client(supabase)

    @classmethod
    async def reconcile_invoice(
        cls,
        *,
        user_jwt: str,
        company_id: UUID,
        invoice_id: UUID,
        resolution: str,
        invoice_number: str | None,
        net_total: Decimal | None,
        gross_total: Decimal | None,
    ) -> InvoiceHistoryItem:
        current = await cls.get_invoice_history_item(
            user_jwt=user_jwt, company_id=company_id, invoice_id=invoice_id
        )
        if current.status != "uncertain":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Only uncertain invoice attempts can be reconciled.",
            )
        if resolution == "not_issued":
            await cls._resolve_not_issued(user_jwt=user_jwt, invoice_id=invoice_id)
        else:
            if (
                not invoice_number
                or net_total != current.net_total
                or gross_total != current.gross_total
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "The confirmed provider invoice number and totals must match "
                        "the reviewed ledger values."
                    ),
                )
            pdf = await cls._query_confirmed_pdf(
                user_jwt=user_jwt, current=current, invoice_number=invoice_number,
            )
            await cls._finalize_invoice(
                user_jwt=user_jwt,
                invoice_id=invoice_id,
                invoice_number=invoice_number,
                net_total=cast(Decimal, net_total),
                gross_total=cast(Decimal, gross_total),
                pdf=pdf,
            )
        return await cls.get_invoice_history_item(
            user_jwt=user_jwt, company_id=company_id, invoice_id=invoice_id
        )

    @classmethod
    async def _query_confirmed_pdf(
        cls, *, user_jwt: str, current: InvoiceHistoryItem, invoice_number: str | None,
    ) -> bytes:
        if current.provider != "szamlazz_hu" or current.status not in {"uncertain", "issued"}:
            raise HTTPException(409, "This invoice cannot be recovered from Számlázz.hu.")
        if current.external_id != f"chronosync-{current.id}":
            raise HTTPException(409, "The invoice external ID does not match its ledger record.")
        try:
            async with CompanyCredentialService.decrypted_szamlazz_agent_key(
                user_jwt=user_jwt, company_id=current.company_id,
            ) as agent_key:
                result = await SzamlazzHuProvider(agent_key).get_invoice_pdf(external_id=current.external_id)
        except SzamlazzHuRejectedError:
            raise HTTPException(409, "The provider could not return this invoice. Check its external ID and company credential before reconciling.") from None
        except SzamlazzHuError:
            raise HTTPException(502, "The existing invoice PDF could not be retrieved. Its reservation remains unchanged.") from None
        pdf = result.get("pdf")
        if (not invoice_number or cls._required_text(result.get("invoice_number")) != invoice_number
                or cls._decimal_or_none(result.get("net_total")) != current.net_total
                or cls._decimal_or_none(result.get("gross_total")) != current.gross_total
                or not isinstance(pdf, bytes) or not 0 < len(pdf) <= cls.MAX_PDF_BYTES or not pdf.startswith(b"%PDF")):
            raise HTTPException(409, "The provider invoice number, totals, and PDF must match the ledger before recovery.")
        return pdf

    @classmethod
    async def _load_client_and_profile(
        cls,
        supabase: Any,
        *,
        company_id: UUID,
        client_id: UUID,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        client_response = await (
            supabase.table("clients")
            .select("id, company_id, client_code, name, is_active")
            .eq("id", str(client_id))
            .eq("company_id", str(company_id))
            .limit(1)
            .execute()
        )
        client_rows = client_response.data or []
        client = (
            cast(dict[str, Any], client_rows[0])
            if isinstance(client_rows, list)
            and client_rows
            and isinstance(client_rows[0], dict)
            else None
        )
        if not client or client.get("is_active") is not True:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="The selected client is missing or inactive.",
            )

        profile_response = await (
            supabase.table("client_billing_profiles")
            .select(
                "client_id, company_id, billing_name, billing_country_code, "
                "billing_postal_code, billing_city, billing_address, tax_subject, "
                "tax_number, eu_vat_number, group_tax_number, billing_email, "
                "send_invoice_email, invoice_language, payment_method, "
                "payment_term_days, vat_code, updated_at"
            )
            .eq("client_id", str(client_id))
            .eq("company_id", str(company_id))
            .limit(1)
            .execute()
        )
        profile_rows = profile_response.data or []
        profile = (
            cast(dict[str, Any], profile_rows[0])
            if isinstance(profile_rows, list)
            and profile_rows
            and isinstance(profile_rows[0], dict)
            else None
        )
        if not profile:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Complete the client's billing profile before invoicing.",
            )
        return client, profile

    @classmethod
    async def _assert_live_invoice_mode(
        cls,
        *,
        user_jwt: str,
        company_id: UUID,
    ) -> None:
        supabase = await SupabaseDataService.get_authenticated_client(user_jwt)
        try:
            await SupabaseDataService._assert_invoice_attachment_access_with_client(
                supabase, user_jwt=user_jwt, company_id=company_id
            )
            response = await (
                supabase.table("companies")
                .select("id, szamlazz_test_mode, is_active, is_deleted")
                .eq("id", str(company_id))
                .limit(1)
                .execute()
            )
            rows = response.data or []
            company = rows[0] if isinstance(rows, list) and rows else None
            if not isinstance(company, dict):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="The company was not found.",
                )
            if (
                company.get("is_active") is not True
                or company.get("is_deleted") is True
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="The company is not active for invoice issuance.",
                )
            if company.get("szamlazz_test_mode") is not False:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Live invoice issuance is disabled while the company is in "
                        "preview mode."
                    ),
                )
        finally:
            await SupabaseDataService._close_authenticated_client(supabase)

    @classmethod
    def _build_invoice_request(
        cls, *, company_id: UUID, client_id: UUID, period_start: date,
        period_end: date, client: dict[str, Any], profile: dict[str, Any],
        lines: list[InvoiceReviewProjectLine], preview: bool,
    ) -> tuple[InvoiceCreateRequest, str]:
        if str(profile.get("vat_code") or "").strip().upper() == "K.AFA":
            raise HTTPException(409, "Margin VAT requires separately configured purchase-price and margin items.")
        if not lines:
            raise HTTPException(409, "No billable project lines were selected.")
        issue_date = datetime.now(cls.BUDAPEST_TIMEZONE).date()
        due_date = issue_date + timedelta(days=int(profile["payment_term_days"]))
        order_number = f"cs-{company_id.hex}-{client_id.hex}-{period_start:%Y%m%d}-{period_end:%Y%m%d}"
        try:
            invoice = InvoiceCreateRequest.model_validate({
                "issue_date": issue_date, "fulfillment_date": period_end,
                "due_date": due_date, "payment_method": profile["payment_method"],
                "currency": lines[0].currency, "language": profile["invoice_language"],
                "order_number": order_number, "external_id": order_number,
                "preview": preview, "download_pdf": True,
                "customer": {
                    "name": profile["billing_name"], "country": profile["billing_country_code"],
                    "postal_code": profile["billing_postal_code"], "city": profile["billing_city"],
                    "address": profile["billing_address"], "email": profile.get("billing_email"),
                    "send_email": False if preview else profile.get("send_invoice_email") is True,
                    "tax_subject": profile["tax_subject"], "tax_number": profile.get("tax_number"),
                    "eu_tax_number": profile.get("eu_vat_number"), "group_tax_number": profile.get("group_tax_number"),
                },
                "items": [{"name": line.project_name, "quantity": line.quantity,
                    "unit": line.unit, "net_unit_price": line.net_unit_price,
                    "vat_rate": line.vat_code} for line in lines],
            })
        except ValidationError as exc:
            raise HTTPException(409, "The billing profile or project prices cannot produce a valid invoice. Review the saved settings.") from exc
        return invoice, str(client.get("client_code") or "client").strip().upper()

    @classmethod
    async def _fetch_approved_entries(
        cls,
        supabase: Any,
        *,
        company_id: UUID,
        client_id: UUID,
        period_start: date,
        period_end: date,
    ) -> list[InvoiceReviewEntry]:
        entries: list[InvoiceReviewEntry] = []
        cursor_id: str | None = None
        while True:
            query = (
                supabase.table("timesheets")
                .select("id, project_id, work_date, hours_logged, description, projects(name)")
                .eq("company_id", str(company_id))
                .eq("client_id", str(client_id))
                .eq("status", "approved")
                .gte("work_date", str(period_start))
                .lte("work_date", str(period_end))
                .order("id")
                .limit(cls.PAGE_SIZE)
            )
            if cursor_id is not None:
                query = query.gt("id", cursor_id)
            response = await query.execute()
            rows = response.data or []
            if not isinstance(rows, list):
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Supabase returned an invalid timesheet response.",
                )
            if len(entries) + len(rows) > cls.MAX_TIMESHEET_ROWS:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="The selected invoice period contains too many entries.",
                )
            for row in rows:
                if not isinstance(row, dict):
                    continue
                hours = cls._decimal_or_none(row.get("hours_logged"))
                if hours is None or hours <= 0:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            "An approved time entry contains invalid hours. Correct "
                            "the entry before reviewing the invoice."
                        ),
                    )
                project = cls._unwrap_relation(row.get("projects")) or {}
                entries.append(
                    InvoiceReviewEntry(
                        id=row.get("id"),
                        project_id=row.get("project_id"),
                        work_date=row.get("work_date"),
                        hours_logged=hours.quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        ),
                        description=str(row.get("description") or ""),
                        project_name=str(project.get("name") or "General Task"),
                    )
                )
            if not rows:
                break
            last_row = rows[-1]
            next_cursor = (
                str(last_row.get("id") or "")
                if isinstance(last_row, dict)
                else ""
            )
            if not next_cursor or next_cursor == cursor_id:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Supabase returned an invalid timesheet page cursor.",
                )
            cursor_id = next_cursor
        entries.sort(key=lambda item: (item.work_date, str(item.id)))
        return entries

    @classmethod
    async def _fetch_approved_hours(
        cls,
        supabase: Any,
        *,
        company_id: UUID,
        client_id: UUID,
        period_start: date,
        period_end: date,
    ) -> Decimal:
        total = Decimal("0")
        cursor_id: str | None = None
        row_count = 0
        while True:
            query = (
                supabase.table("timesheets")
                .select("id, hours_logged")
                .eq("company_id", str(company_id))
                .eq("client_id", str(client_id))
                .eq("status", "approved")
                .gte("work_date", str(period_start))
                .lte("work_date", str(period_end))
                .order("id")
                .limit(cls.PAGE_SIZE)
            )
            if cursor_id is not None:
                query = query.gt("id", cursor_id)
            response = await query.execute()
            rows = response.data or []
            if not isinstance(rows, list):
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Supabase returned an invalid timesheet response.",
                )
            row_count += len(rows)
            if row_count > cls.MAX_TIMESHEET_ROWS:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="The selected invoice period contains too many entries.",
                )
            for row in rows:
                if not isinstance(row, dict):
                    continue
                hours = cls._decimal_or_none(row.get("hours_logged"))
                if hours is None or hours <= 0:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            "An approved time entry contains invalid hours. Correct "
                            "the entry before previewing the invoice."
                        ),
                    )
                total += hours
            if not rows:
                break
            last_row = rows[-1]
            next_cursor = (
                str(last_row.get("id") or "")
                if isinstance(last_row, dict)
                else ""
            )
            if not next_cursor or next_cursor == cursor_id:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Supabase returned an invalid timesheet page cursor.",
                )
            cursor_id = next_cursor
        return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @classmethod
    async def _reserve_invoice(
        cls,
        *,
        user_jwt: str,
        review: ClientInvoiceReview,
    ) -> UUID:
        requested_id = uuid4()
        supabase = await SupabaseDataService.get_authenticated_client(user_jwt)
        try:
            response = await supabase.rpc(
                "reserve_manual_invoice",
                {
                    "p_invoice_id": str(requested_id),
                    "p_company_id": str(review.company_id),
                    "p_client_id": str(review.client_id),
                    "p_period_start": str(review.period_start),
                    "p_period_end": str(review.period_end),
                    "p_review_fingerprint": review.review_fingerprint,
                    "p_recipient_name": review.recipient.name,
                    "p_recipient_email": review.recipient.email,
                    "p_send_email": review.recipient.send_email,
                    "p_billing_model": review.billing_model,
                    "p_quantity": str(review.quantity),
                    "p_unit": review.unit,
                    "p_net_unit_price": str(review.net_unit_price),
                    "p_vat_code": review.vat_code,
                    "p_currency": review.currency,
                    "p_net_total": str(review.net_total),
                    "p_vat_total": str(review.vat_total),
                    "p_gross_total": str(review.gross_total),
                    "p_invoice_snapshot": review.model_dump(mode="json"),
                    "p_timesheet_ids": [
                        str(entry.id) for entry in review.approved_entries
                    ],
                },
            ).execute()
        except APIError as exc:
            if exc.code == "23505":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "An invoice attempt already exists for this client and period. "
                        "Use invoice history instead of issuing a duplicate."
                    ),
                ) from exc
            if exc.code == "54000":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="This invoice reached the five-attempt limit. Resolve the provider issue before contacting support.",
                ) from exc
            if exc.code in {"22023", "40001", "PT409"}:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "The approved entries changed after review. Refresh and review "
                        "the invoice again."
                    ),
                ) from exc
            if exc.code == "42501":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You are not authorized to issue this invoice.",
                ) from exc
            raise
        finally:
            await SupabaseDataService._close_authenticated_client(supabase)
        rows = response.data or []
        row = rows[0] if isinstance(rows, list) and rows else None
        try:
            reserved_id = (
                UUID(str(row.get("invoice_id")))
                if isinstance(row, dict)
                else None
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Supabase returned an invalid invoice reservation.",
            ) from exc
        if reserved_id is None:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="The invoice reservation was not confirmed. Check invoice history before retrying.",
            )
        return reserved_id

    @classmethod
    async def _mark_attempt(
        cls,
        *,
        user_jwt: str,
        invoice_id: UUID,
        attempt_status: str,
        error_code: str,
        error_message: str,
    ) -> None:
        supabase = await SupabaseDataService.get_authenticated_client(user_jwt)
        try:
            await supabase.rpc(
                "mark_manual_invoice_attempt",
                {
                    "p_invoice_id": str(invoice_id),
                    "p_status": attempt_status,
                    "p_error_code": error_code,
                    "p_error_message": error_message,
                },
            ).execute()
        finally:
            await SupabaseDataService._close_authenticated_client(supabase)

    @classmethod
    async def _finalize_invoice(
        cls,
        *,
        user_jwt: str,
        invoice_id: UUID,
        invoice_number: str,
        net_total: Decimal,
        gross_total: Decimal,
        pdf: bytes | None,
    ) -> None:
        supabase = await SupabaseDataService.get_authenticated_client(user_jwt)
        try:
            try:
                response = await supabase.rpc(
                    "finalize_manual_invoice",
                    {
                        "p_invoice_id": str(invoice_id),
                        "p_invoice_number": invoice_number,
                        "p_net_total": str(net_total),
                        "p_gross_total": str(gross_total),
                        "p_pdf_base64": (
                            base64.b64encode(pdf).decode("ascii")
                            if pdf is not None
                            else None
                        ),
                    },
                ).execute()
                rows = response.data
                if not (
                    isinstance(rows, list) and len(rows) == 1
                    and isinstance(rows[0], dict)
                    and str(rows[0].get("invoice_id")) == str(invoice_id)
                    and rows[0].get("status") == "issued"
                    and rows[0].get("invoice_number") == invoice_number.strip()
                ):
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail="Invoice finalization was not confirmed. Reconcile from invoice history before retrying.",
                    )
            except APIError as exc:
                if exc.code in {"22023", "40001", "PT409", "23505"}:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            "The invoice could not be finalized because its ledger or "
                            "time entries changed. Reconcile the attempt before retrying."
                        ),
                    ) from exc
                if exc.code == "42501":
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="You are not authorized to finalize this invoice.",
                    ) from exc
                raise
        finally:
            await SupabaseDataService._close_authenticated_client(supabase)

    @classmethod
    async def _resolve_not_issued(
        cls,
        *,
        user_jwt: str,
        invoice_id: UUID,
    ) -> None:
        supabase = await SupabaseDataService.get_authenticated_client(user_jwt)
        try:
            await supabase.rpc(
                "resolve_manual_invoice_not_issued",
                {"p_invoice_id": str(invoice_id)},
            ).execute()
        finally:
            await SupabaseDataService._close_authenticated_client(supabase)

    @classmethod
    def _history_item(cls, row: dict[str, Any]) -> InvoiceHistoryItem:
        client = cls._unwrap_relation(row.get("clients")) or {}
        return InvoiceHistoryItem(
            id=row.get("id"),
            company_id=row.get("company_id"),
            client_id=row.get("client_id"),
            client_name=str(client.get("name") or row.get("recipient_name") or ""),
            period_start=row.get("period_start"),
            period_end=row.get("period_end"),
            status=row.get("status"),
            provider=row.get("provider"),
            external_id=str(row.get("external_id") or ""),
            recipient_name=str(row.get("recipient_name") or ""),
            recipient_email=row.get("recipient_email"),
            send_email=row.get("send_email") is True,
            quantity=row.get("quantity"),
            unit=str(row.get("unit") or ""),
            net_unit_price=row.get("net_unit_price"),
            vat_code=str(row.get("vat_code") or ""),
            currency=str(row.get("currency") or ""),
            net_total=row.get("net_total"),
            vat_total=row.get("vat_total"),
            gross_total=row.get("gross_total"),
            invoice_number=row.get("invoice_number"),
            pdf_available=row.get("pdf_available") is True,
            provider_error_code=row.get("provider_error_code"),
            provider_error_message=row.get("provider_error_message"),
            attempt_count=int(row.get("attempt_count") or 1),
            issued_at=row.get("issued_at"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    @classmethod
    def _calculate_totals(
        cls, invoice: InvoiceCreateRequest
    ) -> tuple[Decimal, Decimal, Decimal]:
        net_total = Decimal("0")
        vat_total = Decimal("0")
        for item in invoice.items:
            line_net, line_vat, _ = calculate_line_totals(
                item, currency=invoice.currency, tax_subject=invoice.customer.tax_subject
            )
            net_total += line_net
            vat_total += line_vat
        return net_total, vat_total, net_total + vat_total

    @staticmethod
    def _review_fingerprint(review_data: dict[str, Any]) -> str:
        serializable: dict[str, Any] = {}
        for key, value in review_data.items():
            if hasattr(value, "model_dump"):
                serializable[key] = value.model_dump(mode="json")
            elif isinstance(value, list):
                serializable[key] = [
                    item.model_dump(mode="json")
                    if hasattr(item, "model_dump")
                    else item
                    for item in value
                ]
            elif isinstance(value, (UUID, date, Decimal)):
                serializable[key] = str(value)
            else:
                serializable[key] = value
        encoded = json.dumps(
            serializable,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _decode_bytea(value: Any) -> bytes | None:
        if isinstance(value, bytes):
            return value
        if not isinstance(value, str) or not value:
            return None
        try:
            if value.startswith("\\x"):
                return bytes.fromhex(value[2:])
            return base64.b64decode(value, validate=True)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _unwrap_relation(value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            return value
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value[0]
        return None

    @staticmethod
    def _required_text(value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @staticmethod
    def _validate_profile(profile: dict[str, Any]) -> None:
        required_text_fields = (
            "billing_name",
            "billing_country_code",
            "billing_postal_code",
            "billing_city",
            "billing_address",
            "payment_method",
            "invoice_language",
            "vat_code",
        )
        missing = [
            field
            for field in required_text_fields
            if not str(profile.get(field) or "").strip()
        ]
        tax_subject = profile.get("tax_subject")
        if tax_subject is None:
            missing.append("tax_subject")
        if tax_subject in {1, 7} and not str(profile.get("tax_number") or "").strip():
            missing.append("tax_number")
        if tax_subject == 6 and not str(profile.get("eu_vat_number") or "").strip():
            missing.append("eu_vat_number")
        if profile.get("send_invoice_email") is True and not str(
            profile.get("billing_email") or ""
        ).strip():
            missing.append("billing_email")
        if missing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Complete the client's billing profile before invoicing. "
                    f"Missing or invalid: {', '.join(sorted(set(missing)))}."
                ),
            )

    @staticmethod
    def _decimal_or_none(value: Any) -> Decimal | None:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
        return result if result.is_finite() else None
