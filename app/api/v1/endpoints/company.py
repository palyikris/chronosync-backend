from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from postgrest.exceptions import APIError

from app.api.v1.endpoints.reports import extract_bearer_token
from app.core.config import settings as server_settings
from app.schemas.company import (
    InvoiceConnectionTestResponse,
    InvoiceProviderConfigRequest,
    InvoiceProviderConfigResponse,
    SzamlazzKeyRequest,
)
from app.schemas.email import EmailStatusResponse
from app.services.company_service import CompanyCredentialService
from app.services.email_service import (
    email_configured, email_missing_variables, invoice_email_transport,
)
from app.services.supabase_service import SupabaseDataService

router = APIRouter()


@router.get("/email-status", response_model=EmailStatusResponse)
async def get_email_status(
    company_id: UUID, token: str = Depends(extract_bearer_token),
) -> EmailStatusResponse:
    client = await SupabaseDataService.get_authenticated_client(token)
    try:
        try:
            response = await client.rpc("get_company_email_status", {"p_company_id": str(company_id)}).execute()
        except APIError as exc:
            if exc.code == "42501":
                raise HTTPException(403, "Active company administrator access required.") from None
            raise HTTPException(503, "Email status is unavailable. Check the email migration.") from None
        return EmailStatusResponse(
            configured=email_configured(), worker_enabled=server_settings.EMAIL_WORKER_ENABLED,
            invoice_transport=invoice_email_transport(), missing_env=email_missing_variables(),
            **response.data,
        )
    finally:
        await SupabaseDataService._close_authenticated_client(client)


@router.get(
    "/invoice-settings",
    response_model=InvoiceProviderConfigResponse,
)
async def get_invoice_settings(
    company_id: UUID, token: str = Depends(extract_bearer_token)
) -> InvoiceProviderConfigResponse:
    settings = await CompanyCredentialService.get_invoice_settings(
        user_jwt=token, company_id=company_id
    )
    return InvoiceProviderConfigResponse(**settings)


@router.put(
    "/invoice-settings",
    response_model=InvoiceProviderConfigResponse,
)
async def set_invoice_settings(
    payload: InvoiceProviderConfigRequest,
    token: str = Depends(extract_bearer_token),
) -> InvoiceProviderConfigResponse:
    saved_settings = await CompanyCredentialService.set_invoice_settings(
        user_jwt=token,
        company_id=payload.company_id,
        invoice_provider=payload.invoice_provider,
        api_key=payload.api_key.get_secret_value() if payload.api_key else None,
    )
    return InvoiceProviderConfigResponse(**saved_settings)


@router.post("/invoice-settings/test-connection")
async def test_invoice_connection(
    payload: InvoiceProviderConfigRequest,
    token: str = Depends(extract_bearer_token),
) -> InvoiceConnectionTestResponse:
    return await CompanyCredentialService.test_invoice_connection(
        user_jwt=token,
        company_id=payload.company_id,
        invoice_provider=payload.invoice_provider,
        api_key=payload.api_key.get_secret_value() if payload.api_key else None,
    )


@router.post("/szamlazz-key", status_code=status.HTTP_204_NO_CONTENT)
async def set_szamlazz_key(
    payload: SzamlazzKeyRequest, token: str = Depends(extract_bearer_token)
) -> None:
    """Store a tenant's Számla Agent key without returning the credential."""
    await CompanyCredentialService.set_szamlazz_agent_key(
        user_jwt=token,
        company_id=payload.company_id,
        agent_key=payload.agent_key.get_secret_value(),
    )
