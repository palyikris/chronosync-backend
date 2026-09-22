from fastapi import APIRouter, Depends, status
from uuid import UUID

from app.api.v1.endpoints.reports import extract_bearer_token
from app.schemas.company import (
    InvoiceProviderConfigRequest,
    InvoiceProviderConfigResponse,
    SzamlazzKeyRequest,
)
from app.services.company_service import CompanyCredentialService

router = APIRouter()


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
    status_code=status.HTTP_204_NO_CONTENT,
)
async def set_invoice_settings(
    payload: InvoiceProviderConfigRequest,
    token: str = Depends(extract_bearer_token),
) -> None:
    await CompanyCredentialService.set_invoice_settings(
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
