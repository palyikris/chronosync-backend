import asyncio
import base64
import hashlib
import io
from decimal import Decimal
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from openpyxl import load_workbook

from app.main import app
from app.schemas.invoices import InvoiceReviewProjectLine
from app.services.client_invoice_service import ClientInvoiceService as Invoices
from app.services.company_service import CompanyCredentialService
from app.services.invoice_annex_service import InvoiceAnnexService as Annex
from app.services.invoice_providers.base import InvoiceEmailAttachment
from app.services.invoice_providers.szamlazz_hu import SzamlazzHuProvider as Provider, SzamlazzHuRequestError
from app.services.supabase_service import SupabaseDataService
from test_manual_invoice_issue import invoice_review, invoice_request, history_item, fake_agent_key, COMPANY_ID, CLIENT_ID, INVOICE_ID
from test_project_invoice_pricing import Database, COMPANY, CLIENT, MONTHLY


def sample_review():
    review=invoice_review()
    item=InvoiceReviewProjectLine(project_id=UUID(int=10),project_name='Internal project',
        invoice_name='Advisory services',invoice_note='Reviewed work',po_number='PO-42',
        pricing_year=2026,pricing_updated_at='2026-10-01T00:00:00Z',quantity='2.5',unit='óra',
        net_unit_price='10000',vat_code='27',currency='HUF',net_total='25000',vat_total='6750',gross_total='31750',
        logged_hours='2.5')
    return review.model_copy(update={'billing_model':'project','items':[item],
        'attach_timesheet_report':True,'approved_entries':[review.approved_entries[0].model_copy(
            update={'project_id':item.project_id,'billing_multiplier':1,'description':'=DO_NOT_EXECUTE()'})]})


def attach_review(language='hu', *, email=True):
    review=sample_review()
    review.recipient.send_email=email
    attachment=asyncio.run(Annex.build(review,language=language))
    review.attachment=Annex.metadata(attachment,language=language)
    return review,attachment


@pytest.mark.parametrize('language',['en','hu'])
def test_annex_workbook_is_deterministic_localized_and_matches_review(language):
    review,attachment=attach_review(language)
    again=asyncio.run(Annex.build(review,language=language))
    assert attachment.content==again.content
    assert len(attachment.content)<2_000_000
    assert hashlib.sha256(attachment.content).hexdigest()==review.attachment.sha256
    wb=load_workbook(io.BytesIO(attachment.content),data_only=True)
    billing,times=wb.worksheets
    assert billing.title==Annex.TEXT[language]['billing']
    assert billing['A9'].value=='Advisory services'
    assert Decimal(str(billing['F9'].value))==review.items[0].net_total
    assert Decimal(str(billing['H10'].value))==review.gross_total
    assert billing['I9'].value=='Reviewed work\nPO: PO-42'
    assert times['C6'].value=="'=DO_NOT_EXECUTE()"
    assert times['C6'].data_type=='s'
    assert times['F6'].value==2.5
    assert billing.freeze_panes=='A9' and times.freeze_panes=='A6'
    assert billing.page_setup.fitToWidth==1


def test_quantity_based_invoice_without_entries_still_has_a_meaningful_annex():
    review=sample_review().model_copy(update={'approved_entries':[]})
    item=review.items[0].model_copy(update={'quantity':Decimal(1),'unit':'hó','net_unit_price':Decimal(25000),'logged_hours':None})
    review.items=[item]
    attachment=asyncio.run(Annex.build(review,language='en'))
    wb=load_workbook(io.BytesIO(attachment.content),data_only=True)
    assert wb.worksheets[0]['B9'].value==1
    assert wb.worksheets[1]['A6'].value==Annex.TEXT['en']['empty']


def test_report_byte_limit_blocks_before_invoice_issuance():
    with patch.object(Annex,'MAX_BYTES',1):
        with pytest.raises(HTTPException) as raised:
            asyncio.run(Annex.build(sample_review(),language='en'))
    assert raised.value.status_code==413


def test_report_does_not_silently_truncate_long_source_text():
    review=sample_review()
    review.approved_entries[0].description='x'*32767
    with pytest.raises(HTTPException): asyncio.run(Annex.build(review,language='en'))


def test_attachment_preferences_and_language_are_part_of_actual_review_fingerprint():
    db=Database()
    profile=db.tables['client_billing_profiles'][0]
    profile['attach_timesheet_report']=True
    db.tables['clients'][0]['invoice_attachment_language']='en'
    async def review():
        return await Invoices.build_invoice_review(user_jwt='test',company_id=COMPANY,client_id=CLIENT,
            period_start=date(2026,10,1),period_end=date(2026,10,31),
            project_quantities={MONTHLY:Decimal(1)})
    with patch.object(SupabaseDataService,'get_authenticated_client',AsyncMock(return_value=db)), \
         patch.object(SupabaseDataService,'_close_authenticated_client',AsyncMock()), \
         patch.object(SupabaseDataService,'_assert_invoice_attachment_access_with_client',AsyncMock()):
        first=asyncio.run(review())
        repeated=asyncio.run(review())
        assert first.review_fingerprint==repeated.review_fingerprint
        assert first.attachment.language=='en'
        db.tables['clients'][0]['invoice_attachment_language']='hu'
        assert asyncio.run(review()).review_fingerprint!=first.review_fingerprint
        profile['attach_timesheet_report']=False
        disabled=asyncio.run(review())
        assert disabled.attachment is None
        assert disabled.review_fingerprint!=first.review_fingerprint


def test_saved_quantity_review_survives_browser_number_roundtrip():
    db=Database()
    db.tables['client_billing_profiles'][0]['attach_timesheet_report']=True
    db.tables['project_pricing'][1]['recurring_quantity']='1.0000'
    params=dict(user_jwt='test',company_id=COMPANY,client_id=CLIENT,
        period_start=date(2026,10,1),period_end=date(2026,10,31))
    with patch.object(SupabaseDataService,'get_authenticated_client',AsyncMock(return_value=db)), \
         patch.object(SupabaseDataService,'_close_authenticated_client',AsyncMock()), \
         patch.object(SupabaseDataService,'_assert_invoice_attachment_access_with_client',AsyncMock()):
        first=asyncio.run(Invoices.build_invoice_review(**params,project_quantities={}))
        quantities={UUID(str(key)):Decimal(str(float(value))) for key,value in first.project_quantities.items()}
        repeated=asyncio.run(Invoices.build_invoice_review(**params,project_quantities=quantities))
        assert repeated.review_fingerprint==first.review_fingerprint
        assert repeated.attachment==first.attachment
        attachment=asyncio.run(Invoices.preview_invoice_attachment(**params,
            project_quantities=quantities,review_fingerprint=first.review_fingerprint))
        assert hashlib.sha256(attachment.content).hexdigest()==first.attachment.sha256


@pytest.mark.parametrize('email',[True,False])
def test_issuance_saves_reviewed_bytes_before_provider_and_only_uploads_when_email_enabled(email):
    review,attachment=attach_review(email=email)
    invoice=invoice_request()
    invoice.customer.send_email=email
    events=[]
    async def store(**kwargs):
        assert kwargs['attachment'].content==attachment.content
        events.append('store')
    async def create(*args,**kwargs):
        assert events==['store']
        assert kwargs.get('attachments',[])==([attachment] if email else [])
        events.append('provider')
        return dict(invoice_number='E-2026-1',net_total=review.net_total,gross_total=review.gross_total,pdf=b'%PDF-live')
    with patch.object(Invoices,'_build_review_context',AsyncMock(return_value=(review,invoice))), \
         patch.object(Invoices,'_assert_live_invoice_mode',AsyncMock()), \
         patch.object(Invoices,'_reserve_invoice',AsyncMock(return_value=INVOICE_ID)), \
         patch.object(Invoices,'_store_invoice_attachment',AsyncMock(side_effect=store)), \
         patch.object(CompanyCredentialService,'decrypted_szamlazz_agent_key',fake_agent_key), \
         patch.object(Provider,'create_invoice',AsyncMock(side_effect=create)) as provider, \
         patch.object(Invoices,'_finalize_invoice',AsyncMock()), \
         patch.object(Invoices,'get_invoice_history_item',AsyncMock(return_value=history_item())):
        asyncio.run(Invoices.issue_client_invoice(user_jwt='test',company_id=COMPANY_ID,
            client_id=CLIENT_ID,period_start=review.period_start,period_end=review.period_end,
            review_fingerprint=review.review_fingerprint))
        provider.assert_awaited_once()
    assert events==['store','provider']


def test_report_storage_failure_never_calls_provider_and_releases_unissued_attempt():
    review,_=attach_review()
    with patch.object(Invoices,'_build_review_context',AsyncMock(return_value=(review,invoice_request()))), \
         patch.object(Invoices,'_assert_live_invoice_mode',AsyncMock()), \
         patch.object(Invoices,'_reserve_invoice',AsyncMock(return_value=INVOICE_ID)), \
         patch.object(Invoices,'_store_invoice_attachment',AsyncMock(side_effect=HTTPException(502,'storage failed'))), \
         patch.object(Invoices,'_mark_attempt',AsyncMock()) as mark, \
         patch.object(Provider,'create_invoice',AsyncMock()) as provider:
        with pytest.raises(HTTPException):
            asyncio.run(Invoices.issue_client_invoice(user_jwt='test',company_id=COMPANY_ID,
                client_id=CLIENT_ID,period_start=review.period_start,period_end=review.period_end,
                review_fingerprint=review.review_fingerprint))
        provider.assert_not_awaited()
        assert mark.await_args.kwargs['attempt_status']=='failed'


def test_provider_timeout_never_repeats_an_invoice_with_attachment():
    review,_=attach_review()
    with patch.object(Invoices,'_build_review_context',AsyncMock(return_value=(review,invoice_request()))), \
         patch.object(Invoices,'_assert_live_invoice_mode',AsyncMock()), \
         patch.object(Invoices,'_reserve_invoice',AsyncMock(return_value=INVOICE_ID)), \
         patch.object(Invoices,'_store_invoice_attachment',AsyncMock()) as store, \
         patch.object(CompanyCredentialService,'decrypted_szamlazz_agent_key',fake_agent_key), \
         patch.object(Invoices,'_mark_attempt',AsyncMock()) as mark, \
         patch.object(Provider,'create_invoice',AsyncMock(side_effect=SzamlazzHuRequestError('timeout'))) as provider:
        with pytest.raises(HTTPException):
            asyncio.run(Invoices.issue_client_invoice(user_jwt='test',company_id=COMPANY_ID,
                client_id=CLIENT_ID,period_start=review.period_start,period_end=review.period_end,
                review_fingerprint=review.review_fingerprint))
        provider.assert_awaited_once();store.assert_awaited_once()
        assert mark.await_args.kwargs['attempt_status']=='uncertain'


def test_changed_report_hash_blocks_before_any_reservation():
    review,_=attach_review()
    review.attachment.sha256='0'*64
    with patch.object(Invoices,'_build_review_context',AsyncMock(return_value=(review,invoice_request()))), \
         patch.object(Invoices,'_assert_live_invoice_mode',AsyncMock()), \
         patch.object(Invoices,'_reserve_invoice',AsyncMock()) as reserve:
        with pytest.raises(HTTPException):
            asyncio.run(Invoices.issue_client_invoice(user_jwt='test',company_id=COMPANY_ID,
                client_id=CLIENT_ID,period_start=review.period_start,period_end=review.period_end,
                review_fingerprint=review.review_fingerprint))
        reserve.assert_not_awaited()


def test_provider_multipart_contains_the_exact_workbook_and_valid_xml():
    _,attachment=attach_review('en')
    def handler(request):
        assert b'name="action-xmlagentxmlfile"' in request.content
        assert b'name="attachfile1"' in request.content
        assert attachment.filename.encode() in request.content
        assert attachment.content in request.content
        assert b'<sendEmail>true</sendEmail>' in request.content
        encoded=base64.b64encode(b'%PDF-live').decode()
        return httpx.Response(200,content=f'<xmlszamlavalasz xmlns="http://www.szamlazz.hu/xmlszamlavalasz"><sikeres>true</sikeres><szamlaszam>TEST-1</szamlaszam><szamlanetto>25000</szamlanetto><szamlabrutto>31750</szamlabrutto><pdf>{encoded}</pdf></xmlszamlavalasz>')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result=await Provider('synthetic',http_client=client).create_invoice(invoice_request().model_dump(),attachments=[attachment])
            assert result['invoice_number']=='TEST-1'
    asyncio.run(run())


@pytest.mark.parametrize('mode',['preview','email_off','too_many'])
def test_invalid_email_attachment_request_never_reaches_network(mode):
    _,attachment=attach_review()
    invoice=invoice_request()
    if mode=='preview':invoice.preview=True
    if mode=='email_off':invoice.customer.send_email=False
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail('Unexpected provider call'))) as client:
            with pytest.raises(ValueError):
                await Provider('synthetic',http_client=client).create_invoice(invoice.model_dump(),attachments=[attachment]*(6 if mode=='too_many' else 1))
    asyncio.run(run())


def test_reviewed_workbook_download_is_read_only_and_serves_expected_bytes():
    review,attachment=attach_review()
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            return await client.post('/api/v1/invoices/client-attachment',headers={'Authorization':'Bearer dummy'},json={
                'company_id':str(COMPANY_ID),'client_id':str(CLIENT_ID),'period_start':str(review.period_start),
                'period_end':str(review.period_end),'review_fingerprint':review.review_fingerprint})
    with patch.object(Invoices,'build_invoice_review',AsyncMock(return_value=review)), \
         patch.object(Provider,'create_invoice',AsyncMock()) as provider:
        response=asyncio.run(run())
        assert response.status_code==200
        assert response.content==attachment.content
        assert response.headers['cache-control']=='no-store'
        assert attachment.filename in response.headers['content-disposition']
        provider.assert_not_awaited()


def test_archive_download_uses_saved_bytes_without_regenerating_changed_source():
    review,attachment=attach_review()
    current=history_item().model_copy(update={'status':'reversed','attachment':review.attachment,'attachment_available':True})
    class Query:
        def select(self,*_):return self
        def eq(self,*_):return self
        def limit(self,*_):return self
        async def execute(self):return SimpleNamespace(data=[{'attachment_bytes':'\\x'+attachment.content.hex()}])
    client=SimpleNamespace(table=lambda _:Query())
    with patch.object(Invoices,'get_invoice_history_item',AsyncMock(return_value=current)), \
         patch.object(SupabaseDataService,'get_authenticated_client',AsyncMock(return_value=client)), \
         patch.object(SupabaseDataService,'_close_authenticated_client',AsyncMock()), \
         patch.object(Annex,'build',AsyncMock()) as generate:
        result=asyncio.run(Invoices.get_invoice_attachment(user_jwt='test',company_id=COMPANY_ID,invoice_id=INVOICE_ID))
        assert result.content==attachment.content
        generate.assert_not_awaited()


def test_payroll_line_and_entry_multiplier_reconcile_in_report():
    review=sample_review()
    review.approved_entries[0].billing_multiplier=3
    review.items[0]=review.items[0].model_copy(update={'quantity':Decimal('7.5'),'is_payroll':True,
        'net_total':Decimal(75000),'vat_total':Decimal(20250),'gross_total':Decimal(95250)})
    review.net_total=Decimal(75000);review.vat_total=Decimal(20250);review.gross_total=Decimal(95250)
    attachment=asyncio.run(Annex.build(review,language='en'))
    wb=load_workbook(io.BytesIO(attachment.content),data_only=True)
    assert wb.worksheets[0]['B9'].value==wb.worksheets[1]['F6'].value==7.5
    assert wb.worksheets[0]['H10'].value==95250
