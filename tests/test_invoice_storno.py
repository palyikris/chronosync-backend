import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
import shutil
import subprocess
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from app.main import app
from app.schemas.invoices import InvoiceReversalDetails
from app.services.client_invoice_service import ClientInvoiceService as Invoices
from app.services.company_service import CompanyCredentialService
from app.services.invoice_storno_service import InvoiceStornoService as Storno
from app.services.invoice_providers.szamlazz_hu import (
    SzamlazzHuProvider as Provider, SzamlazzHuRejectedError, SzamlazzHuRequestError,
    SzamlazzHuResponseError,
)
from test_manual_invoice_issue import history_item, COMPANY_ID, INVOICE_ID

PDF = b'%PDF-storno'
ORIGINAL = dict(invoice_number='E-2026-1', net_total=Decimal('25000'),
    gross_total=Decimal('31750'), currency='HUF', format='3', reversed=False,
    document_type='SZ', issue_date='2026-10-22')
REVERSED = dict(invoice_number='E-2026-2', original_invoice_number='E-2026-1',
    net_total=Decimal('-25000'), gross_total=Decimal('-31750'), currency='HUF',
    format='3', document_type='SS', issue_date='2026-11-04')
RESULT = dict(invoice_number='E-2026-2', net_total=Decimal('-25000'),
    gross_total=Decimal('-31750'), pdf=PDF)


@asynccontextmanager
async def key(*args, **kwargs):
    yield 'synthetic-test-key'


@pytest.fixture
def scenario():
    current = history_item()
    result = current.model_copy(update={'status': 'reversed', 'reversal': InvoiceReversalDetails(
        id=uuid4(), status='reversed', reason='Correction', issue_date=date(2026,11,4),
        e_invoice=True, invoice_number='E-2026-2', net_total='-25000', gross_total='-31750',
        pdf_available=True, attempt_count=1)})
    rpc = AsyncMock(side_effect=lambda jwt, name, params:
        [{'request_id': params['p_request_id']}] if name == 'reserve_invoice_storno'
        else [{'invoice_id': str(INVOICE_ID), 'status': 'reversed'}])
    query = AsyncMock(return_value=ORIGINAL)
    reverse = AsyncMock(return_value=RESULT)
    pdf = AsyncMock(return_value=RESULT)
    fetch = AsyncMock(side_effect=lambda **kwargs: result if rpc.await_count and
        any(call.args[1] == 'finalize_invoice_storno' for call in rpc.await_args_list) else current)
    with patch.object(Invoices, 'get_invoice_history_item', fetch), \
         patch.object(Invoices, '_assert_live_invoice_mode', AsyncMock()), \
         patch.object(CompanyCredentialService, 'decrypted_szamlazz_agent_key', key), \
         patch.object(Provider, 'query_invoice', query), \
         patch.object(Provider, 'reverse_invoice', reverse), \
         patch.object(Provider, 'get_invoice_pdf', pdf), \
         patch.object(Storno, '_rpc', rpc):
        yield dict(current=current, result=result, rpc=rpc, query=query, reverse=reverse, pdf=pdf, fetch=fetch)


def run_reverse():
    return asyncio.run(Storno.reverse(user_jwt='test', company_id=COMPANY_ID,
        invoice_id=INVOICE_ID, original_invoice_number='E-2026-1', reason='Correction'))


def run_reconcile(resolution='reversed'):
    return asyncio.run(Storno.reconcile(user_jwt='test', company_id=COMPANY_ID,
        invoice_id=INVOICE_ID, original_invoice_number='E-2026-1', reason='Correction',
        resolution=resolution, reversal_invoice_number='E-2026-2' if resolution=='reversed' else None))


def test_storno_success_reads_original_and_calls_provider_once(scenario):
    assert run_reverse().status == 'reversed'
    scenario['query'].assert_awaited_once_with(external_id=f'chronosync-{INVOICE_ID}')
    scenario['reverse'].assert_awaited_once()
    assert scenario['reverse'].await_args.kwargs['e_invoice'] is True
    names = [call.args[1] for call in scenario['rpc'].await_args_list]
    assert names == ['reserve_invoice_storno', 'finalize_invoice_storno']
    params = scenario['rpc'].await_args_list[-1].args[2]
    assert params['p_net_total'] == '-25000'
    assert base64.b64decode(params['p_pdf_base64']) == PDF


@pytest.mark.parametrize('error,state', [
    (SzamlazzHuRequestError('timeout'),'uncertain'),
    (SzamlazzHuResponseError('malformed'),'uncertain'),
    (SzamlazzHuRejectedError('already reversed',code='999'),'uncertain'),
    (SzamlazzHuRejectedError('invalid key',code='3'),'failed'),
    (RuntimeError('unexpected'),'uncertain'),
])
def test_provider_failure_never_releases_work_or_retries(scenario,error,state):
    scenario['reverse'].side_effect=error
    with pytest.raises((HTTPException, RuntimeError)):
        run_reverse()
    scenario['reverse'].assert_awaited_once()
    assert [c.args[1] for c in scenario['rpc'].await_args_list] == ['reserve_invoice_storno','mark_invoice_storno_attempt']
    assert scenario['rpc'].await_args_list[-1].args[2]['p_status'] == state


@pytest.mark.parametrize('override', [{'invoice_number':'E-2026-1'}, {'net_total':'-24999'},
    {'gross_total':'-31749'}, {'pdf':None}, {'pdf':b'bad'}])
def test_inconsistent_storno_confirmation_stays_uncertain(scenario,override):
    scenario['reverse'].return_value={**RESULT,**override}
    with pytest.raises(HTTPException): run_reverse()
    assert scenario['rpc'].await_args_list[-1].args[1] == 'mark_invoice_storno_attempt'
    assert scenario['rpc'].await_args_list[-1].args[2]['p_status'] == 'uncertain'
    assert all(c.args[1] != 'finalize_invoice_storno' for c in scenario['rpc'].await_args_list)


@pytest.mark.parametrize('override', [{'invoice_number':'WRONG'}, {'currency':'EUR'},
    {'net_total':Decimal(1)}, {'reversed':True}, {'format':'0'}])
def test_original_preflight_blocks_mismatch_or_external_storno_before_reservation(scenario,override):
    scenario['query'].return_value={**ORIGINAL,**override}
    with pytest.raises(HTTPException): run_reverse()
    scenario['rpc'].assert_not_awaited()
    scenario['reverse'].assert_not_awaited()


def test_preview_mode_blocks_before_provider_call(scenario):
    with patch.object(Invoices,'_assert_live_invoice_mode',AsyncMock(side_effect=HTTPException(409,'preview'))):
        with pytest.raises(HTTPException): run_reverse()
    scenario['query'].assert_not_awaited()
    scenario['rpc'].assert_not_awaited()


def test_reconcile_external_storno_uses_reference_and_reads_pdf_without_creation(scenario):
    scenario['query'].side_effect=[{**ORIGINAL,'reversed':True}, REVERSED]
    assert run_reconcile().status == 'reversed'
    scenario['reverse'].assert_not_awaited()
    scenario['pdf'].assert_awaited_once_with(invoice_number='E-2026-2')
    assert [c.args[1] for c in scenario['rpc'].await_args_list] == ['finalize_invoice_storno']


@pytest.mark.parametrize('override', [{'original_invoice_number':'OTHER'}, {'document_type':'SZ'},
    {'currency':'EUR'}, {'format':'1'}, {'net_total':Decimal('-1')}, {'issue_date':'bad'}])
def test_recovery_cannot_use_unrelated_negative_invoice(scenario,override):
    scenario['query'].side_effect=[{**ORIGINAL,'reversed':True}, {**REVERSED,**override}]
    with pytest.raises(HTTPException): run_reconcile()
    scenario['rpc'].assert_not_awaited()
    scenario['reverse'].assert_not_awaited()


def test_recovery_cannot_trust_only_a_matching_negative_pdf(scenario):
    scenario['query'].side_effect=[ORIGINAL,REVERSED]
    with pytest.raises(HTTPException): run_reconcile()
    scenario['rpc'].assert_not_awaited()


def test_uncertain_attempt_can_be_verified_not_reversed_without_a_provider_write(scenario):
    reversal=scenario['result'].reversal.model_copy(update={'status':'uncertain'})
    scenario['fetch'].side_effect=None
    scenario['fetch'].return_value=scenario['current'].model_copy(update={'reversal':reversal})
    run_reconcile('not_reversed')
    assert [c.args[1] for c in scenario['rpc'].await_args_list] == ['resolve_invoice_storno_not_reversed']
    scenario['reverse'].assert_not_awaited()
    scenario['pdf'].assert_not_awaited()


def test_lost_finalization_response_is_recovered_without_resending(scenario):
    scenario['fetch'].side_effect=[scenario['current'],scenario['result']]
    with patch.object(Storno,'_finalize',AsyncMock(side_effect=RuntimeError('lost response'))):
        assert run_reverse().status == 'reversed'
    scenario['reverse'].assert_awaited_once()
    assert [c.args[1] for c in scenario['rpc'].await_args_list] == ['reserve_invoice_storno']


def test_local_finalization_failure_and_history_outage_remain_uncertain(scenario):
    scenario['fetch'].side_effect=[scenario['current'],RuntimeError('database unavailable')]
    with patch.object(Storno,'_finalize',AsyncMock(side_effect=RuntimeError('database unavailable'))):
        with pytest.raises(HTTPException): run_reverse()
    assert scenario['rpc'].await_args_list[-1].args[2]['p_status']=='uncertain'


def document_xml(extra='', flag=''):
    return f'''<szamla xmlns="http://www.szamlazz.hu/szamla"><alap>
      <szamlaszam>TEST-1</szamlaszam><tipus>SZ</tipus><eszamla>3</eszamla>
      <kelt>2026-11-04</kelt><devizanem>HUF</devizanem>{flag}{extra}</alap>
      <osszegek><totalossz><netto>25000</netto><brutto>31750</brutto></totalossz></osszegek></szamla>'''


@pytest.mark.parametrize('flag,expected', [('',False),('<sztornozott>true</sztornozott>',True),
    ('<sztornozott>0</sztornozott>',False)])
def test_document_query_handles_provider_optional_cancellation_flag(flag,expected):
    assert Provider.parse_invoice_document(httpx.Response(200,content=document_xml(flag=flag)))['reversed'] is expected


@pytest.mark.parametrize('body', [document_xml('<szamlaszam>OTHER</szamlaszam>'),
    document_xml(flag='<sztornozott>maybe</sztornozott>'), '<szamla/>',
    '<!DOCTYPE x [<!ENTITY a "abc">]><szamla>&a;</szamla>'])
def test_document_query_rejects_ambiguous_or_unsafe_xml(body):
    with pytest.raises(SzamlazzHuResponseError): Provider.parse_invoice_document(httpx.Response(200,content=body))


def test_storno_transport_and_official_xsd(tmp_path):
    def handler(request):
        assert b'name="action-szamla_agent_st"' in request.content
        assert b'szamlaKulsoAzon' not in request.content
        assert b'<ns0:email />' in request.content or b'<email />' in request.content
        pdf=base64.b64encode(PDF).decode()
        return httpx.Response(200,content=f'''<xmlszamlavalasz xmlns="http://www.szamlazz.hu/xmlszamlavalasz">
          <sikeres>true</sikeres><szamlaszam>TEST-ST</szamlaszam><szamlanetto>-25000</szamlanetto>
          <szamlabrutto>-31750</szamlabrutto><pdf>{pdf}</pdf></xmlszamlavalasz>''')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider=Provider('synthetic',http_client=client)
            xml=provider.build_storno_xml(invoice_number='TEST-1',issue_date=date(2026,11,4),reason='Correction & billing',e_invoice=True)
            path=tmp_path/'storno.xml';path.write_bytes(xml)
            if shutil.which('xmllint'):
                schema=Path(__file__).parent/'fixtures/szamlazz/xmlszamlast.xsd'
                result=subprocess.run(['xmllint','--nonet','--noout','--schema',str(schema),str(path)],capture_output=True)
                assert result.returncode==0,result.stderr
            result=await provider.reverse_invoice(invoice_number='TEST-1',issue_date=date(2026,11,4),reason='Correction',e_invoice=True)
            assert result['net_total']==Decimal('-25000') and result['pdf']==PDF
    asyncio.run(run())


@pytest.mark.parametrize('override', [{'confirm':False},{'confirm':'true'},{'reason':' '},
    {'original_invoice_number':' '},{'extra':'not allowed'}])
def test_storno_route_requires_explicit_valid_confirmation(override):
    payload={'company_id':str(COMPANY_ID),'original_invoice_number':'TEST-1','reason':'Correction','confirm':True,**override}
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            return await client.post(f'/api/v1/invoices/{INVOICE_ID}/storno',json=payload,headers={'Authorization':'Bearer dummy'})
    with patch.object(Storno,'reverse',AsyncMock()) as handler:
        assert asyncio.run(run()).status_code==422
        handler.assert_not_awaited()


def test_storno_route_returns_typed_reversal_after_single_provider_call(scenario):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            return await client.post(f'/api/v1/invoices/{INVOICE_ID}/storno',
                headers={'Authorization':'Bearer dummy'},json={'company_id':str(COMPANY_ID),
                'original_invoice_number':'E-2026-1','reason':'Correction','confirm':True})
    response=asyncio.run(run())
    assert response.status_code==200, response.text
    assert response.json()['status']=='reversed'
    assert Decimal(response.json()['reversal']['gross_total'])==Decimal('-31750')
    scenario['reverse'].assert_awaited_once()


def test_storno_cancellation_preserves_durable_uncertainty(scenario):
    scenario['reverse'].side_effect=asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): run_reverse()
    assert scenario['rpc'].await_args_list[-1].args[2]['p_status']=='uncertain'


def test_storno_stale_confirmation_cannot_target_another_original(scenario):
    scenario['fetch'].side_effect=None
    scenario['fetch'].return_value=scenario['current'].model_copy(update={'invoice_number':'CHANGED'})
    with pytest.raises(HTTPException): run_reverse()
    scenario['query'].assert_not_awaited()
    scenario['reverse'].assert_not_awaited()


def test_uncertain_storno_cannot_be_sent_again(scenario):
    scenario['fetch'].side_effect=None
    scenario['fetch'].return_value=scenario['current'].model_copy(update={
        'reversal':scenario['result'].reversal.model_copy(update={'status':'uncertain'})})
    with pytest.raises(HTTPException): run_reverse()
    scenario['query'].assert_not_awaited()
    scenario['reverse'].assert_not_awaited()


@pytest.mark.parametrize('headers', [{'szlahu_szamlaszam':'OTHER'}, {'szlahu_nettovegosszeg':'1'},
    {'szlahu_bruttovegosszeg':'NaN'}, {'szlahu_hibakod':'3'}])
def test_invoice_query_rejects_conflicting_headers(headers):
    with pytest.raises(SzamlazzHuResponseError):
        Provider.parse_invoice_document(httpx.Response(200,content=document_xml(),headers=headers))


def test_invoice_query_rejects_duplicate_metadata_blocks():
    body=document_xml().replace('</alap>','</alap><alap/>')
    with pytest.raises(SzamlazzHuResponseError):
        Provider.parse_invoice_document(httpx.Response(200,content=body))


def test_legacy_electronic_format_can_be_recovered_as_current_electronic_storno(scenario):
    scenario['query'].side_effect=[{**ORIGINAL,'reversed':True,'format':'2'}, REVERSED]
    assert run_reconcile().status == 'reversed'
    scenario['reverse'].assert_not_awaited()
