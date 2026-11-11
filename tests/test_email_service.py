import asyncio
import base64
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.services.email_service import (
    EmailSendError, EmailWorker, email_configured, invoice_email_transport,
    render_email, require_email_ready, send_resend, validate_email_configuration,
)


@pytest.fixture(autouse=True)
def config(monkeypatch):
    for name, value in {
        'RESEND_API_KEY': 're_synthetic', 'EMAIL_FROM': 'ChronoSync <mail@updates.example.test>',
        'SUPABASE_SERVICE_ROLE_KEY': 'synthetic-server-key', 'EMAIL_REPLY_TO': '',
        'FRONTEND_URL': 'https://app.example.test', 'SUPABASE_URL': 'https://project.supabase.co',
        'EMAIL_WORKER_ENABLED': True, 'INVOICE_EMAIL_TRANSPORT': 'auto',
    }.items():
        monkeypatch.setattr(settings, name, value)


def leave_job():
    return {'id': str(uuid4()), 'claim_token': str(uuid4()), 'kind': 'leave_requested',
            'recipient': 'admin@example.test', 'message': None, 'event_data': {
                'company_name': 'Test <Company>', 'employee_name': '<script>alert(1)</script>',
                'start_date': '2026-11-11', 'end_date': '2026-11-12'}}


def invoice_job(kind='invoice_issued'):
    annex = b'PK\x03\x04saved-review-bytes'
    return {**leave_job(), 'kind': kind, 'recipient': 'buyer@example.test', 'event_data': {
        'company_name': 'Test Company', 'recipient_name': 'Buyer', 'invoice_number': 'E/2026/42',
        'language': 'en', 'period_start': '2026-10-01', 'period_end': '2026-10-31',
        'due_date': '2026-11-20', 'gross_total': '31750', 'currency': 'HUF',
        'pdf_base64': base64.b64encode(b'%PDF-issued-document').decode(),
        'attachment_base64': base64.b64encode(annex).decode(),
        'attachment': {'filename': 'reviewed-attachment.xlsx', 'sha256': hashlib.sha256(annex).hexdigest(),
                       'size_bytes': len(annex)}}}


def test_auto_transport_and_disabled_feature(monkeypatch):
    assert email_configured() and invoice_email_transport() == 'resend'
    monkeypatch.setattr(settings, 'RESEND_API_KEY', '')
    monkeypatch.setattr(settings, 'EMAIL_FROM', '')
    validate_email_configuration()
    assert not email_configured() and invoice_email_transport() == 'provider'
    monkeypatch.setattr(settings, 'INVOICE_EMAIL_TRANSPORT', 'resend')
    with pytest.raises(ValueError, match='RESEND_API_KEY, EMAIL_FROM'):
        validate_email_configuration()


@pytest.mark.parametrize('name,value', [
    ('EMAIL_FROM', 'sender@example.test\r\nBcc: leaked@example.test'),
    ('EMAIL_FROM', 'a@example.test, b@example.test'),
    ('EMAIL_REPLY_TO', 'invalid'), ('FRONTEND_URL', 'http://app.example.test'),
    ('FRONTEND_URL', 'https://user:password@app.example.test'),
    ('SUPABASE_SERVICE_ROLE_KEY', 'replace-with-secret'),
])
def test_invalid_config_is_rejected_without_secret_values(monkeypatch, name, value):
    monkeypatch.setattr(settings, name, value)
    with pytest.raises(ValueError) as exc:
        validate_email_configuration()
    assert value not in str(exc.value)


def test_invoice_requires_an_active_worker(monkeypatch):
    monkeypatch.setattr(settings, 'EMAIL_WORKER_ENABLED', False)
    with pytest.raises(HTTPException) as exc:
        require_email_ready()
    assert exc.value.status_code == 503


def test_leave_html_is_escaped_and_recipient_is_private():
    message = render_email(leave_job())
    assert message['to'] == ['admin@example.test']
    assert '<script>' not in message['html'] and '&lt;script&gt;' in message['html']
    assert 'https://app.example.test/leaves' in message['text']
    assert 'attachments' not in message and 'cc' not in message and 'bcc' not in message


@pytest.mark.parametrize('decision,label', [('APPROVED', 'Leave approved'), ('REJECTED', 'Leave rejected')])
def test_leave_decision_email_is_bilingual_and_escapes_employee_data(decision, label):
    job = leave_job()
    job.update(kind='leave_decision', recipient='employee@example.test')
    job['event_data']['decision'] = decision
    message = render_email(job)
    assert label in message['subject'] and '2026-11-11' in message['text']
    assert '<script>' not in message['html'] and '&lt;script&gt;' in message['html']
    assert message['to'] == ['employee@example.test'] and 'attachments' not in message
    assert 'https://app.example.test/leaves' in message['text']


def test_timesheet_rejection_email_includes_reason_entry_and_attention_link():
    job = {**leave_job(), 'kind': 'timesheet_rejected', 'recipient': 'employee@example.test'}
    job['event_data'].update(work_date='2026-10-01', hours_logged=2.5, client_name='Client', project_name='Project',
                             description='Entry details', rejection_reason='<b>Explain these hours</b>')
    message = render_email(job)
    assert message['to'] == ['employee@example.test'] and 'Timesheet rejected' in message['subject']
    assert '2026-10-01' in message['text'] and '2.5' in message['text'] and 'Entry details' in message['text']
    assert '<b>Explain these hours</b>' in message['text']
    assert '<b>' not in message['html'] and '&lt;b&gt;Explain these hours&lt;/b&gt;' in message['html']
    assert '/timesheet?view=attention' in message['text'] and 'attachments' not in message


def test_unrecognized_event_is_rejected_instead_of_rendered_as_an_invoice():
    job = leave_job()
    job['kind'] = 'unsupported'
    with pytest.raises(ValueError, match='Unsupported email event'):
        render_email(job)


@pytest.mark.parametrize('kind,count', [('invoice_issued', 2), ('invoice_reversed', 1)])
def test_document_emails_use_archived_bytes_and_localized_content(kind, count):
    job = invoice_job(kind)
    message = render_email(job)
    assert len(message['attachments']) == count
    assert message['attachments'][0]['content'] == job['event_data']['pdf_base64']
    if count == 2:
        assert message['attachments'][1]['content'] == job['event_data']['attachment_base64']
        assert 'Due date: 2026-11-20' in message['text']
    assert 'E/2026/42' in message['subject']


def test_tampered_saved_attachment_is_not_sent():
    job = invoice_job()
    job['event_data']['attachment']['sha256'] = '0' * 64
    with pytest.raises(ValueError):
        render_email(job)


@pytest.mark.parametrize('status,retry', [(400, False), (401, False), (403, False), (422, False), (429, True), (500, True)])
def test_provider_error_classification_never_leaks_body(status, retry):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(status, json={'message': 'secret-payload'}))) as client:
            with pytest.raises(EmailSendError) as exc:
                await send_resend(client, render_email(leave_job()), 'job-1')
        assert exc.value.retryable is retry and 'secret-payload' not in str(exc.value)
    asyncio.run(run())


def test_repeated_network_attempts_use_exact_request_and_idempotency_key():
    seen = []
    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            raise httpx.ReadTimeout('unconfirmed', request=request)
        return httpx.Response(200, json={'id': 'provider-1'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            message = render_email(leave_job())
            with pytest.raises(EmailSendError) as exc:
                await send_resend(client, message, 'job-1')
            assert exc.value.retryable
            assert await send_resend(client, message, 'job-1') == 'provider-1'
    asyncio.run(run())
    assert seen[0].content == seen[1].content
    assert seen[0].headers['Idempotency-Key'] == seen[1].headers['Idempotency-Key'] == 'chronosync/job-1'


def test_worker_persists_payload_before_sending_and_keeps_acceptance_separate():
    job = invoice_job()
    events = []
    async def rpc(name, params):
        events.append(name)
        if name == 'claim_transactional_email': return job
        if name == 'prepare_transactional_email': return params['p_message']
        assert params['p_provider_id'] == 'provider-1'
        return True
    def handler(request):
        events.append('resend')
        assert len(json.loads(request.content)['attachments']) == 2
        return httpx.Response(200, json={'id': 'provider-1'})
    async def run():
        worker = EmailWorker()
        worker.rpc = rpc
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            worker.client = client
            assert await worker.process_one()
    asyncio.run(run())
    assert events == ['claim_transactional_email', 'prepare_transactional_email', 'resend', 'finish_transactional_email']


def test_retry_uses_frozen_message_despite_changed_sender(monkeypatch):
    job = leave_job()
    job['message'] = render_email(job)
    monkeypatch.setattr(settings, 'EMAIL_FROM', 'changed@example.test')
    async def rpc(name, params):
        if name == 'claim_transactional_email': return job
        assert name == 'finish_transactional_email'
        return True
    def handler(request):
        assert json.loads(request.content)['from'] == job['message']['from']
        return httpx.Response(200, json={'id': 'provider-1'})
    async def run():
        worker = EmailWorker()
        worker.rpc = rpc
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            worker.client = client
            await worker.process_one()
    asyncio.run(run())


def test_startup_fails_on_missing_migration_and_closes_http_client():
    client = SimpleNamespace(aclose=AsyncMock())
    async def run():
        worker = EmailWorker()
        with patch('app.services.email_service.httpx.AsyncClient', return_value=client), \
             patch.object(worker, 'rpc', AsyncMock(side_effect=RuntimeError('missing migration'))):
            with pytest.raises(RuntimeError): await worker.start()
        assert worker.task is None and worker.client is None
        client.aclose.assert_awaited_once()
    asyncio.run(run())


def test_worker_stop_cancels_and_closes_resources():
    async def run():
        worker = EmailWorker()
        with patch.object(worker, 'rpc', AsyncMock(return_value=True)):
            await worker.start()
            task, client = worker.task, worker.client
            await worker.start()
            assert worker.task is task
            await worker.stop()
            await worker.stop()
        assert task.cancelled() and client.is_closed
        assert worker.task is None and worker.client is None
    asyncio.run(run())


@pytest.mark.parametrize('allowed', [True, False])
def test_status_endpoint_uses_authorized_rpc_and_never_exposes_payloads(allowed):
    from app.main import app
    from app.services.supabase_service import SupabaseDataService
    from postgrest.exceptions import APIError
    company_id = str(uuid4())
    def rpc(name, params):
        assert name == 'get_company_email_status' and params == {'p_company_id': company_id}
        if allowed:
            return SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(data={
                'queue': {'pending': 2, 'accepted': 1}, 'recent_failures': [],
            })))
        return SimpleNamespace(execute=AsyncMock(side_effect=APIError({
            'code': '42501', 'message': 'private database details', 'details': '', 'hint': '',
        })))
    async def run():
        with patch.object(SupabaseDataService, 'get_authenticated_client', AsyncMock(return_value=SimpleNamespace(rpc=rpc))), \
             patch.object(SupabaseDataService, '_close_authenticated_client', AsyncMock()) as close:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                anonymous = await client.get(f'/api/v1/company/email-status?company_id={company_id}')
                assert anonymous.status_code == 401
                response = await client.get(f'/api/v1/company/email-status?company_id={company_id}',
                    headers={'Authorization': 'Bearer synthetic-token'})
            close.assert_awaited_once()
        assert response.status_code == (200 if allowed else 403)
        body = response.json()
        assert 'RESEND_API_KEY' not in str(body) and 'private database details' not in str(body)
        if allowed:
            assert body['queue'] == {'pending': 2, 'accepted': 1} and body['invoice_transport'] == 'resend'
    asyncio.run(run())


def test_resend_issuance_disables_provider_email_and_keeps_saved_attachment():
    from app.services.client_invoice_service import ClientInvoiceService
    from app.services.invoice_providers.szamlazz_hu import SzamlazzHuProvider
    from test_invoice_annex import attach_review
    from test_manual_invoice_issue import COMPANY_ID, CLIENT_ID, INVOICE_ID, fake_agent_key, history_item, invoice_request
    from app.services.company_service import CompanyCredentialService
    review, attachment = attach_review()
    review.email_transport = 'resend'
    invoice = invoice_request()
    invoice.customer.send_email = True
    create = AsyncMock(return_value={'invoice_number': 'TEST-1', 'pdf': b'%PDF-issued',
                                    'net_total': str(review.net_total), 'gross_total': str(review.gross_total)})
    with patch.object(ClientInvoiceService, '_build_review_context', AsyncMock(return_value=(review, invoice))), \
         patch.object(ClientInvoiceService, '_assert_live_invoice_mode', AsyncMock()), \
         patch.object(ClientInvoiceService, '_reserve_invoice', AsyncMock(return_value=INVOICE_ID)), \
         patch.object(ClientInvoiceService, '_store_invoice_attachment', AsyncMock()) as store, \
         patch.object(ClientInvoiceService, '_finalize_invoice', AsyncMock()), \
         patch.object(ClientInvoiceService, 'get_invoice_history_item', AsyncMock(return_value=history_item())), \
         patch.object(CompanyCredentialService, 'decrypted_szamlazz_agent_key', fake_agent_key), \
         patch.object(SzamlazzHuProvider, 'create_invoice', create):
        asyncio.run(ClientInvoiceService.issue_client_invoice(user_jwt='test', company_id=COMPANY_ID,
            client_id=CLIENT_ID, period_start=review.period_start, period_end=review.period_end,
            review_fingerprint=review.review_fingerprint))
    store.assert_awaited_once()
    assert create.await_args.args[0]['customer']['send_email'] is False
    assert not create.await_args.kwargs.get('attachments')
