from fastapi import APIRouter, Depends

from app.api.v1.endpoints.reports import extract_bearer_token
from app.schemas.users import CompanyInvitationRequest, CompanyInvitationResponse
from app.services.invitation_service import InvitationService

router = APIRouter()


@router.post('/invitations', response_model=CompanyInvitationResponse, status_code=201)
async def invite_company_member(
    payload: CompanyInvitationRequest, token: str = Depends(extract_bearer_token),
) -> CompanyInvitationResponse:
    return CompanyInvitationResponse(**await InvitationService.invite(token, payload))
