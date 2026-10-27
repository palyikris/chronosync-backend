"""Trusted tenant invitations. No passwords or membership claims from the browser."""
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from postgrest.exceptions import APIError

from app.core.config import settings
from app.schemas.users import CompanyInvitationRequest
from app.services.supabase_service import SupabaseDataService


class InvitationService:
    @classmethod
    async def invite(cls, token: str, payload: CompanyInvitationRequest) -> dict:
        # Fail before persisting an invitation if the sender is not configured.
        if not settings.SUPABASE_SERVICE_ROLE_KEY:
            raise HTTPException(503, 'Invitation email service is not configured.')
        origin = settings.FRONTEND_URL.rstrip('/')
        parsed = urlsplit(origin)
        if (parsed.scheme not in ('https', 'http') or not parsed.netloc
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
                or (parsed.scheme != 'https' and parsed.hostname not in ('localhost', '127.0.0.1'))):
            raise HTTPException(503, 'Invitation application URL is not configured correctly.')
        client = await SupabaseDataService.get_authenticated_client(token)
        try:
            # This RPC authorizes the actual JWT and commits the pending invite
            # before the Auth API can create a user and fire its signup trigger.
            response = await client.rpc('prepare_company_invitation', {
                'p_email': payload.email, 'p_company_id': str(payload.company_id),
                'p_full_name': payload.full_name, 'p_role': payload.role,
            }).execute()
            invitation = response.data
        except APIError as exc:
            code = str(exc.code)
            status = {'42501': 403, 'PT409': 409, 'PT429': 429, '22023': 422}.get(code, 502)
            messages = {403: 'Company administrator access required.',
                        409: 'This email already has an incompatible membership or pending invitation.',
                        429: 'Wait one minute before resending this invitation.',
                        422: 'Invalid invitation.', 502: 'Could not prepare the invitation.'}
            raise HTTPException(status, messages[status]) from None
        finally:
            await SupabaseDataService._close_authenticated_client(client)
        if not isinstance(invitation, dict) or not invitation.get('id'):
            raise HTTPException(502, 'Could not prepare the invitation.')
        existing = invitation.get('existing_user') is True
        await cls._send_email(invitation['email'], existing=existing, origin=origin)
        return {'id': invitation['id'], 'email': invitation['email'], 'status': 'sent'}

    @staticmethod
    async def _send_email(email: str, *, existing: bool, origin: str) -> None:
        # Existing identities receive a non-creating OTP. Never create a second
        # identity, manufacture a password, or expose an admin key to React.
        key = settings.SUPABASE_ANON_KEY if existing else settings.SUPABASE_SERVICE_ROLE_KEY
        operation = 'otp' if existing else 'invite'
        landing = 'magic' if existing else 'invite'
        body = {'email': email, 'create_user': False} if existing else {'email': email}
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    f'{settings.SUPABASE_URL.rstrip("/")}/auth/v1/{operation}',
                    params={'redirect_to': f'{origin}/auth/{landing}'},
                    headers={'apikey': key, 'Authorization': f'Bearer {key}'}, json=body,
                )
        except httpx.HTTPError:
            # The pending invitation remains available for an explicit resend.
            raise HTTPException(502, 'Invitation delivery could not be confirmed. Wait one minute before resending.') from None
        if response.status_code == 429:
            raise HTTPException(429, 'Email rate limit reached. Try again later.')
        if not response.is_success:
            # Auth error bodies can contain identity details; do not log/return them.
            raise HTTPException(502, 'Invitation email could not be sent. Check the Auth email configuration.')
