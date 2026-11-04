"""Preflight automatic invoices without reserving work or contacting the provider."""

from fastapi import HTTPException

from app.schemas.invoices import InvoiceBatchReviewItem, InvoiceBatchReviewRequest
from app.services.client_invoice_service import ClientInvoiceService
from app.services.company_service import CompanyCredentialService
from app.services.supabase_service import SupabaseDataService


class InvoiceBatchService:
    @classmethod
    async def review(
        cls, *, user_jwt: str, payload: InvoiceBatchReviewRequest
    ) -> list[InvoiceBatchReviewItem]:
        supabase = await SupabaseDataService.get_authenticated_client(user_jwt)
        try:
            # Authorization errors must fail the entire request, never become skips.
            await SupabaseDataService._assert_invoice_attachment_access_with_client(
                supabase,
                user_jwt=user_jwt,
                company_id=payload.company_id,
                required_module="invoicing",
            )
            configuration_error = None
            try:
                await ClientInvoiceService._assert_live_invoice_mode(
                    user_jwt=user_jwt, company_id=payload.company_id
                )
                # Validate the stored credential locally. Do not call Számlázz.hu.
                async with CompanyCredentialService.decrypted_szamlazz_agent_key(
                    user_jwt, payload.company_id
                ):
                    pass
            except HTTPException as exc:
                if exc.status_code not in {404, 409, 503}:
                    raise
                configuration_error = str(exc.detail)

            results = []
            for client_id in payload.client_ids:
                try:
                    if configuration_error:
                        raise HTTPException(409, configuration_error)
                    overlap = await (
                        supabase.table("invoice_ledger")
                        .select("id")
                        .eq("company_id", str(payload.company_id))
                        .eq("client_id", str(client_id))
                        .in_("status", ["issuing", "issued", "uncertain"])
                        .lte("period_start", str(payload.period_end))
                        .gte("period_end", str(payload.period_start))
                        .limit(1)
                        .execute()
                    )
                    if not isinstance(overlap.data, list):
                        raise HTTPException(
                            502, "Invalid invoice reservation response."
                        )
                    if overlap.data:
                        raise HTTPException(
                            409,
                            "An issued invoice or unresolved invoice attempt overlaps this period. "
                            "Check invoice history before invoicing again.",
                        )
                    review = await ClientInvoiceService.build_invoice_review(
                        user_jwt=user_jwt,
                        company_id=payload.company_id,
                        client_id=client_id,
                        period_start=payload.period_start,
                        period_end=payload.period_end,
                    )
                    results.append(
                        InvoiceBatchReviewItem(client_id=client_id, review=review)
                    )
                except HTTPException as exc:
                    # Only known business ineligibility can be acknowledged as a skip.
                    # Outages and authorization failures must stop the preflight.
                    if exc.status_code not in {404, 409}:
                        raise
                    results.append(
                        InvoiceBatchReviewItem(
                            client_id=client_id, reason=str(exc.detail)
                        )
                    )
            return results
        finally:
            await SupabaseDataService._close_authenticated_client(supabase)
