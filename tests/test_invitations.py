import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from postgrest.exceptions import APIError
from pydantic import ValidationError

from app.core.config import settings
from app.schemas.users import CompanyInvitationRequest
from app.services.invitation_service import InvitationService
from app.services.supabase_service import SupabaseDataService


def payload(**changes):
    return CompanyInvitationRequest.model_validate({
        'email': 'NEW@Example.COM', 'full_name': 'New Member',
        'company_id': str(uuid4()), 'role': 'regular', **changes,
    })


@pytest.mark.parametrize('changes', [{'role':'super_admin'},{'password':'TempPassword123!'}, {'email':'bad email'}])
def test_invitation_contract_rejects_privileged_roles_and_passwords(changes):
    with pytest.raises(ValidationError):
        payload(**changes)


def test_sender_commits_and_closes_membership_transaction_before_auth_call():
    events = []
    invitation_id = str(uuid4())
    async def execute():
        events.append('persisted')
        return SimpleNamespace(data={'id': invitation_id,'email':'new@example.com','existing_user':False})
    def rpc(name, params):
        assert name == 'prepare_company_invitation'
        assert params['p_email'] == 'new@example.com'
        return SimpleNamespace(execute=execute)
    async def close(_client):
        events.append('closed')
    async def send(email, *, existing, origin):
        assert email == 'new@example.com' and not existing
        assert origin == 'https://chronosync-lovat.vercel.app'
        events.append('email')
    with patch.object(settings,'SUPABASE_SERVICE_ROLE_KEY','server-secret'), \
         patch.object(SupabaseDataService,'get_authenticated_client',AsyncMock(return_value=SimpleNamespace(rpc=rpc))), \
         patch.object(SupabaseDataService,'_close_authenticated_client',close), \
         patch.object(InvitationService,'_send_email',send):
        result = asyncio.run(InvitationService.invite('caller-jwt',payload()))
    assert events == ['persisted','closed','email']
    assert result == {'id': invitation_id,'email':'new@example.com','status':'sent'}


@pytest.mark.parametrize('existing',[True,False])
def test_auth_delivery_uses_correct_operation_and_never_creates_existing_user(existing):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200,json={})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with patch.object(settings,'SUPABASE_URL','https://project.supabase.co'), \
         patch.object(settings,'SUPABASE_ANON_KEY','publishable'), \
         patch.object(settings,'SUPABASE_SERVICE_ROLE_KEY','server-secret'), \
         patch('app.services.invitation_service.httpx.AsyncClient',return_value=client):
        asyncio.run(InvitationService._send_email('new@example.com',existing=existing,origin='https://app.example'))
    request = seen[0]
    assert request.url.path == ('/auth/v1/otp' if existing else '/auth/v1/invite')
    assert request.url.params['redirect_to'] == ('https://app.example/auth/magic' if existing else 'https://app.example/auth/invite')
    assert request.headers['apikey'] == ('publishable' if existing else 'server-secret')
    assert (b'"create_user":false' in request.content) is existing
    assert b'password' not in request.content and b'company_id' not in request.content


def test_missing_service_key_does_not_persist_an_invitation():
    with patch.object(settings,'SUPABASE_SERVICE_ROLE_KEY',''), \
         patch.object(SupabaseDataService,'get_authenticated_client',AsyncMock()) as get_client:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(InvitationService.invite('jwt',payload()))
    assert exc.value.status_code == 503
    get_client.assert_not_awaited()


@pytest.mark.parametrize('origin',['https://evil.example/path','https://user:pass@app.example','javascript:alert(1)','http://app.example'])
def test_invalid_configured_origin_is_rejected_before_invitation(origin):
    with patch.object(settings,'SUPABASE_SERVICE_ROLE_KEY','server-secret'), patch.object(settings,'FRONTEND_URL',origin):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(InvitationService.invite('jwt',payload()))
    assert exc.value.status_code == 503


def test_denied_invitation_never_calls_auth_or_exposes_database_details():
    client = SimpleNamespace(rpc=lambda *_: SimpleNamespace(execute=AsyncMock(side_effect=APIError({
        'code':'42501','message':'private database details','details':'secret', 'hint':''}))))
    with patch.object(settings,'SUPABASE_SERVICE_ROLE_KEY','server-secret'), \
         patch.object(SupabaseDataService,'get_authenticated_client',AsyncMock(return_value=client)), \
         patch.object(SupabaseDataService,'_close_authenticated_client',AsyncMock()) as close, \
         patch.object(InvitationService,'_send_email',AsyncMock()) as send:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(InvitationService.invite('jwt',payload()))
    assert exc.value.status_code == 403 and 'private' not in exc.value.detail
    close.assert_awaited_once()
    send.assert_not_awaited()
