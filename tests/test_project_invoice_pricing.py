import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.schemas.invoices import ClientInvoiceReviewRequest, InvoiceReviewEntry
from app.services.client_invoice_service import ClientInvoiceService
from app.services.project_invoice_pricing import build_project_lines
from app.services.supabase_service import SupabaseDataService
from test_client_invoice_preview import billing_profile

COMPANY = UUID('10000000-0000-0000-0000-000000000001')
CLIENT = UUID('20000000-0000-0000-0000-000000000002')
HOURLY = UUID('30000000-0000-0000-0000-000000000001')
MONTHLY = UUID('30000000-0000-0000-0000-000000000002')
STAMP = '2026-10-28T10:00:00Z'


class Database:
    """Apply real query scopes and simulate an API page cap of one record."""
    def __init__(self):
        self.tables = {
            'clients': [dict(id=str(CLIENT), company_id=str(COMPANY), name='Buyer', client_code='BUY', is_active=True)],
            'client_billing_profiles': [dict(billing_profile(net_unit_price='999999', currency='USD'),
                client_id=str(CLIENT), company_id=str(COMPANY), updated_at=STAMP)],
            'projects': [dict(id=str(pid), company_id=str(COMPANY), client_id=str(CLIENT), name=name, is_active=True)
                for pid, name in [(HOURLY, 'Hourly work'), (MONTHLY, 'Monthly work')]],
            'project_pricing': [dict(project_id=str(pid), company_id=str(COMPANY), pricing_year=2026,
                indexed_fee=fee, original_fee='123456', unit=unit, currency='EUR', updated_at=STAMP)
                for pid, fee, unit in [(HOURLY, '100', 'óra'), (MONTHLY, '500', 'hó')]],
            'timesheets': [dict(id=str(UUID(int=i+1)), company_id=str(COMPANY), client_id=str(CLIENT),
                project_id=str(pid), work_date='2026-10-15', hours_logged=hours, description='Work', status='approved',
                projects={'name': name}) for i, (pid, hours, name) in enumerate([
                    (HOURLY, '2.5', 'Hourly work'), (MONTHLY, '3', 'Monthly work')])],
        }
        self.calls = []

    def rpc(self, name, params):
        assert name == 'ensure_company_project_pricing' and params == {'p_company_id': str(COMPANY)}
        return SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(data=0)))

    def table(self, table):
        db = self
        class Query:
            def __init__(self):
                self.filters = []
                self.start = 0
                self.limit_value = 1
            def select(self, *_): return self
            def order(self, *_): return self
            def eq(self, key, value):
                db.calls.append((table, key, value))
                self.filters.append(lambda row: str(row.get(key)) == str(value)); return self
            def gt(self, key, value):
                self.filters.append(lambda row: str(row.get(key)) > str(value)); return self
            def gte(self, key, value):
                self.filters.append(lambda row: str(row.get(key)) >= str(value)); return self
            def lte(self, key, value):
                self.filters.append(lambda row: str(row.get(key)) <= str(value)); return self
            def limit(self, value): self.limit_value = min(value, 1); return self
            def range(self, start, end): self.start = start; self.limit_value = min(end-start+1, 1); return self
            async def execute(self):
                rows = [row for row in db.tables[table] if all(predicate(row) for predicate in self.filters)]
                return SimpleNamespace(data=rows[self.start:self.start+self.limit_value])
        return Query()


def lines(db, quantities=None, entries=None, start=date(2026, 10, 1), end=date(2026, 10, 31)):
    if entries is None:
        entries = [InvoiceReviewEntry(id=row['id'], project_id=row['project_id'], work_date=row['work_date'],
            hours_logged=row['hours_logged'], description='Work', project_name=row['projects']['name'])
            for row in db.tables['timesheets']]
    return asyncio.run(build_project_lines(db, company_id=COMPANY, client_id=CLIENT,
        period_start=start, period_end=end, entries=entries,
        project_quantities={MONTHLY: Decimal(1)} if quantities is None else quantities,
        vat_code='27', tax_subject=1))


def test_project_prices_and_quantities_are_scoped_paginated_and_do_not_use_original_fees():
    db = Database()
    result = lines(db)
    assert [(line.net_unit_price, line.quantity, line.net_total) for line in result] == [
        (Decimal(100), Decimal('2.5'), Decimal(250)), (Decimal(500), Decimal(1), Decimal(500))]
    assert all(line.currency == 'EUR' for line in result)
    for table in ['projects', 'project_pricing']:
        assert (table, 'company_id', str(COMPANY)) in db.calls
    assert ('projects', 'client_id', str(CLIENT)) in db.calls
    assert ('project_pricing', 'pricing_year', 2026) in db.calls


@pytest.mark.parametrize('missing', [None, 'absent'])
def test_missing_indexed_price_blocks_even_with_an_original_or_client_price(missing):
    db = Database()
    if missing == 'absent': db.tables['project_pricing'].pop(0)
    else: db.tables['project_pricing'][0]['indexed_fee'] = None
    with pytest.raises(HTTPException) as raised: lines(db)
    assert raised.value.status_code == 409
    assert 'no 2026 indexed price' in raised.value.detail


def test_zero_price_is_valid_and_non_hourly_quantities_are_explicit():
    db = Database()
    db.tables['project_pricing'][0]['indexed_fee'] = '0'
    assert lines(db)[0].net_total == 0
    with pytest.raises(HTTPException, match='') as raised: lines(db, quantities={})
    assert 'Enter the invoice quantity' in raised.value.detail
    with pytest.raises(HTTPException) as raised: lines(db, quantities={HOURLY: Decimal(99), MONTHLY: Decimal(1)})
    assert 'cannot be overridden' in raised.value.detail


@pytest.mark.parametrize('start,end', [(date(2026,10,2),date(2026,10,31)), (date(2026,10,1),date(2026,11,30))])
def test_monthly_lines_require_exactly_one_full_calendar_month(start, end):
    with pytest.raises(HTTPException) as raised: lines(Database(), start=start, end=end)
    assert 'one complete calendar month' in raised.value.detail


def test_fixed_quantity_can_invoice_without_timesheets_and_mixed_currency_is_rejected():
    db = Database()
    assert lines(db, entries=[])[0].quantity == 1
    db.tables['project_pricing'][1]['currency'] = 'HUF'
    with pytest.raises(HTTPException) as raised: lines(db)
    assert 'one currency' in raised.value.detail


def test_unknown_project_and_cross_year_period_are_rejected():
    with pytest.raises(HTTPException) as raised: lines(Database(), quantities={UUID(int=999): Decimal(1)})
    assert 'outside this client' in raised.value.detail
    with pytest.raises(HTTPException) as raised: lines(Database(), start=date(2025,12,1))
    assert 'one pricing year' in raised.value.detail


def test_review_preview_and_issue_use_project_prices_and_fingerprint_changes_before_provider_call():
    db = Database()
    args = dict(user_jwt='fake', company_id=COMPANY, client_id=CLIENT,
        period_start=date(2026,10,1), period_end=date(2026,10,31), project_quantities={MONTHLY: Decimal(1)})
    with (
        patch.object(SupabaseDataService, 'get_authenticated_client', AsyncMock(return_value=db)),
        patch.object(SupabaseDataService, '_close_authenticated_client', AsyncMock()),
        patch.object(SupabaseDataService, '_assert_invoice_attachment_access_with_client', AsyncMock()),
        patch.object(ClientInvoiceService, '_assert_live_invoice_mode', AsyncMock()) as live_mode,
    ):
        review, invoice = asyncio.run(ClientInvoiceService._build_review_context(**args))
        preview, _ = asyncio.run(ClientInvoiceService.build_preview_invoice(**args))
        assert review.billing_model == 'project'
        assert review.net_total == Decimal(750)
        assert invoice.currency == 'EUR'  # The deprecated client profile says USD.
        assert [item.net_unit_price for item in invoice.items] == [Decimal(100), Decimal(500)]
        assert preview.items == invoice.items
        assert preview.customer.send_email is False and preview.preview is True
        assert len(review.approved_entries) == 2
        db.tables['project_pricing'][0]['indexed_fee'] = '101'
        with pytest.raises(HTTPException) as raised:
            asyncio.run(ClientInvoiceService.issue_client_invoice(**args, review_fingerprint=review.review_fingerprint))
        assert raised.value.status_code == 409
        live_mode.assert_not_awaited()


def test_invoice_api_accepts_quantities_but_rejects_caller_defined_prices():
    with pytest.raises(ValidationError):
        ClientInvoiceReviewRequest(company_id=COMPANY, client_id=CLIENT,
            period_start='2026-10-01', period_end='2026-10-31', net_unit_price=1)


@pytest.mark.parametrize('headcount,quantity', [(None, '2.5'), (1, '2.5'), (12, '30')])
def test_payroll_multiplies_only_hourly_lines_and_preserves_logged_time(headcount, quantity):
    db = Database()
    for project in db.tables['projects']:
        project['payroll_headcount'] = headcount
    result = lines(db)
    assert result[0].quantity == Decimal(quantity)
    assert result[0].net_total == Decimal(quantity) * 100
    assert result[0].logged_hours == Decimal('2.5')
    assert result[0].payroll_headcount == headcount
    assert result[1].quantity == 1  # Monthly quantities are never multiplied.
    assert result[1].logged_hours is None
    assert db.tables['timesheets'][0]['hours_logged'] == '2.5'


def test_payroll_headcount_changes_require_review_before_issuing():
    db = Database()
    args = dict(user_jwt='fake', company_id=COMPANY, client_id=CLIENT,
        period_start=date(2026,10,1), period_end=date(2026,10,31), project_quantities={MONTHLY: Decimal(1)})
    with (
        patch.object(SupabaseDataService, 'get_authenticated_client', AsyncMock(return_value=db)),
        patch.object(SupabaseDataService, '_close_authenticated_client', AsyncMock()),
        patch.object(SupabaseDataService, '_assert_invoice_attachment_access_with_client', AsyncMock()),
        patch.object(ClientInvoiceService, '_assert_live_invoice_mode', AsyncMock()) as live_mode,
    ):
        review, _ = asyncio.run(ClientInvoiceService._build_review_context(**args))
        db.tables['projects'][0]['payroll_headcount'] = 1  # Even when the total stays identical.
        with pytest.raises(HTTPException) as raised:
            asyncio.run(ClientInvoiceService.issue_client_invoice(**args, review_fingerprint=review.review_fingerprint))
        assert raised.value.status_code == 409
        live_mode.assert_not_awaited()
