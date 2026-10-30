"""Invoice quantities must survive project API caps below the requested limit."""
import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

from app.services.client_invoice_service import ClientInvoiceService
from app.services.supabase_service import SupabaseDataService

COMPANY = UUID("10000000-0000-0000-0000-000000000001")
CLIENT = UUID("20000000-0000-0000-0000-000000000001")


class CappedClient:
    def __init__(self):
        self.pages = 0
        self.rows = [{
            "id": str(UUID(int=i)), "company_id": str(COMPANY), "client_id": str(CLIENT),
            "work_date": "2026-10-01", "hours_logged": "1.25", "description": "Work",
            "projects": {"name": "Project"},
            "clients": {"id": str(CLIENT), "company_id": str(COMPANY),
                        "name": "Acme", "client_code": "ACME", "is_active": True},
        } for i in range(1, 6)]

    def table(self, _name):
        client = self

        class Query:
            cursor = ""

            def __getattr__(self, _name):
                return lambda *_args, **_kwargs: self

            def gt(self, _column, cursor):
                self.cursor = cursor
                return self

            async def execute(self):
                client.pages += 1
                # Simulate an API cap of two, regardless of .limit(1000).
                return SimpleNamespace(data=[r for r in client.rows if r["id"] > self.cursor][:2])

        return Query()


def test_attachment_retrieves_every_page_under_a_lower_project_cap():
    client = CappedClient()
    reports = asyncio.run(SupabaseDataService._fetch_aggregated_timesheets_with_client(
        client, company_id=COMPANY, client_ids=[CLIENT],
        start_date=date(2026, 10, 1), end_date=date(2026, 10, 31),
    ))
    assert reports[0]["used_hours"] == 6.25
    assert len(reports[0]["entries"]) == 5
    assert client.pages == 4


def test_invoice_preview_and_review_include_the_same_complete_quantity():
    params = dict(company_id=COMPANY, client_id=CLIENT,
                  period_start=date(2026, 10, 1), period_end=date(2026, 10, 31))
    quantity = asyncio.run(ClientInvoiceService._fetch_approved_hours(CappedClient(), **params))
    entries = asyncio.run(ClientInvoiceService._fetch_approved_entries(CappedClient(), **params))
    assert quantity == Decimal("6.25")
    assert len(entries) == 5
    assert sum(entry.hours_logged for entry in entries) == quantity


def test_payroll_attachment_multiplies_entries_and_carryover_without_changing_source_hours():
    client = CappedClient()
    for row in client.rows[:2]:
        row['projects']['payroll_headcount'] = 4
    reports = asyncio.run(SupabaseDataService._fetch_aggregated_timesheets_with_client(
        client, company_id=COMPANY, client_ids=[CLIENT],
        start_date=date(2026, 10, 1), end_date=date(2026, 10, 31),
    ))
    assert reports[0]['used_hours'] == 13.75  # 2 × 1.25 × 4 + 3 × 1.25
    assert reports[0]['difference_hours'] == -13.75
    assert [e['hours'] for e in reports[0]['entries']] == [5, 5, 1.25, 1.25, 1.25]
    assert all(row['hours_logged'] == '1.25' for row in client.rows)
