from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.api.v1.endpoints.reports import extract_bearer_token
from app.schemas.invoices import (
    ClientInvoiceIssueRequest,
    ClientInvoicePreviewRequest,
    ClientInvoiceReview,
    ClientInvoiceReviewRequest,
    InvoiceCreateRequest,
    InvoiceHistoryItem,
    InvoicePreviewRequest,
    InvoiceReconciliationRequest,
)
from app.services.client_invoice_service import ClientInvoiceService
from app.services.company_service import CompanyCredentialService
from app.services.invoice_providers.szamlazz_hu import (
    SzamlazzHuError,
    SzamlazzHuProvider,
    SzamlazzHuResponseError,
)


router = APIRouter()


async def _generate_preview_pdf(
    *, token: str, company_id: UUID, invoice: InvoiceCreateRequest
) -> bytes:
    customer = invoice.customer.model_copy(update={"send_email": False})
    safe_invoice = invoice.model_copy(
        update={"preview": True, "download_pdf": True, "customer": customer}
    )

    try:
        async with CompanyCredentialService.decrypted_szamlazz_agent_key(
            token, company_id
        ) as api_key:
            result = await SzamlazzHuProvider(api_key).create_invoice(
                safe_invoice.model_dump()
            )
    except SzamlazzHuResponseError as exc:
        detail = "Számlázz.hu rejected the invoice preview."
        if exc.code:
            detail = f"{detail} Error code: {exc.code}."
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=detail,
        ) from exc
    except SzamlazzHuError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not generate the Számlázz.hu invoice preview.",
        ) from exc

    pdf = result.get("pdf")
    if not isinstance(pdf, bytes) or not pdf.startswith(b"%PDF"):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Számlázz.hu did not return a valid invoice preview PDF.",
        )
    return pdf


def _pdf_response(pdf: bytes, filename: str) -> Response:
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post(
    "/preview",
    response_class=Response,
    response_model=None,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def preview_invoice(
    payload: InvoicePreviewRequest,
    token: str = Depends(extract_bearer_token),
) -> Response:
    """Generate a PDF preview without creating a live invoice."""
    pdf = await _generate_preview_pdf(
        token=token,
        company_id=payload.company_id,
        invoice=payload.invoice,
    )
    return _pdf_response(pdf, "invoice-preview.pdf")


@router.post(
    "/client-preview",
    response_class=Response,
    response_model=None,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def preview_client_invoice(
    payload: ClientInvoicePreviewRequest,
    token: str = Depends(extract_bearer_token),
) -> Response:
    """Build a client invoice from its saved profile and approved time entries."""
    invoice, client_code = await ClientInvoiceService.build_preview_invoice(
        user_jwt=token,
        company_id=payload.company_id,
        client_id=payload.client_id,
        period_start=payload.period_start,
        period_end=payload.period_end,
    )
    pdf = await _generate_preview_pdf(
        token=token,
        company_id=payload.company_id,
        invoice=invoice,
    )
    safe_client_code = "".join(
        character for character in client_code if character.isalnum()
    )[:16] or "client"
    return _pdf_response(
        pdf,
        f"invoice-preview-{safe_client_code}-{payload.period_start}-{payload.period_end}.pdf",
    )


@router.post("/client-review", response_model=ClientInvoiceReview)
async def review_client_invoice(
    payload: ClientInvoiceReviewRequest,
    token: str = Depends(extract_bearer_token),
) -> ClientInvoiceReview:
    """Return the exact recipient, totals, tax and approved-entry issue snapshot."""
    return await ClientInvoiceService.build_invoice_review(
        user_jwt=token,
        company_id=payload.company_id,
        client_id=payload.client_id,
        period_start=payload.period_start,
        period_end=payload.period_end,
    )


@router.post("/client-issue", response_model=InvoiceHistoryItem)
async def issue_client_invoice(
    payload: ClientInvoiceIssueRequest,
    token: str = Depends(extract_bearer_token),
) -> InvoiceHistoryItem:
    """Issue only after an explicit confirmation of an unchanged review snapshot."""
    return await ClientInvoiceService.issue_client_invoice(
        user_jwt=token,
        company_id=payload.company_id,
        client_id=payload.client_id,
        period_start=payload.period_start,
        period_end=payload.period_end,
        review_fingerprint=payload.review_fingerprint,
    )


@router.get("/history", response_model=list[InvoiceHistoryItem])
async def invoice_history(
    company_id: UUID,
    limit: int = 50,
    token: str = Depends(extract_bearer_token),
) -> list[InvoiceHistoryItem]:
    return await ClientInvoiceService.list_invoice_history(
        user_jwt=token,
        company_id=company_id,
        limit=limit,
    )


@router.get(
    "/{invoice_id}/pdf",
    response_class=Response,
    response_model=None,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def download_invoice_pdf(
    invoice_id: UUID,
    company_id: UUID,
    token: str = Depends(extract_bearer_token),
) -> Response:
    pdf, filename = await ClientInvoiceService.get_invoice_pdf(
        user_jwt=token,
        company_id=company_id,
        invoice_id=invoice_id,
    )
    return _pdf_response(pdf, filename)


@router.post("/{invoice_id}/reconcile", response_model=InvoiceHistoryItem)
async def reconcile_invoice(
    invoice_id: UUID,
    payload: InvoiceReconciliationRequest,
    token: str = Depends(extract_bearer_token),
) -> InvoiceHistoryItem:
    """Resolve a provider-ambiguous attempt after an explicit external check."""
    return await ClientInvoiceService.reconcile_invoice(
        user_jwt=token,
        company_id=payload.company_id,
        invoice_id=invoice_id,
        resolution=payload.resolution,
        invoice_number=payload.invoice_number,
        net_total=payload.net_total,
        gross_total=payload.gross_total,
    )
