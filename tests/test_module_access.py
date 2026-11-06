import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from fastapi import HTTPException

from app.modules.access import require_company_module
from app.services.supabase_service import SupabaseDataService
from app.services.client_invoice_service import ClientInvoiceService

COMPANY = "10000000-0000-0000-0000-000000000001"
USER = "20000000-0000-0000-0000-000000000001"


class Query:
    def __init__(self, rows):
        self.rows = rows

    def __getattr__(self, name):
        return lambda *args, **kwargs: self

    async def execute(self):
        return SimpleNamespace(data=self.rows)


@pytest.mark.parametrize("enabled,module,allowed", [
    ([], "leave_attachments", False),
    ([], "invoicing", False),
    (["leave_attachments"], "leave_attachments", True),
    (["leave_attachments"], "invoicing", False),
    (["invoicing"], "invoicing", False),
    (["leave_attachments", "invoicing"], "invoicing", True),
])
def test_module_matrix(enabled, module, allowed):
    client = SimpleNamespace(table=lambda _: Query([{
        "enabled_modules": enabled, "is_active": True, "is_deleted": False,
    }]))
    if allowed:
        asyncio.run(require_company_module(client, COMPANY, module))
    else:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(require_company_module(client, COMPANY, module))
        assert exc.value.status_code == 403


@pytest.mark.parametrize("company", [None, {}, {"enabled_modules": ["leave_attachments"]},
    {"enabled_modules": ["leave_attachments"], "is_active": False, "is_deleted": False},
    {"enabled_modules": ["leave_attachments"], "is_active": True, "is_deleted": True}])
def test_missing_inactive_or_deleted_company_fails_closed(company):
    client = SimpleNamespace(table=lambda _: Query([company] if company else []))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(require_company_module(client, COMPANY, "leave_attachments"))
    assert exc.value.status_code == 403


def test_disabled_attachment_module_blocks_before_any_timesheet_query_and_closes_client():
    def table(name):
        if name == "profiles":
            return Query([{"id": USER, "company_id": COMPANY, "role": "company_admin", "is_active": True}])
        assert name == "companies", "Disabled module must not read timesheets"
        return Query([{"enabled_modules": [], "is_active": True, "is_deleted": False}])
    client = SimpleNamespace(table=table)
    close = AsyncMock()
    token = jwt.encode({"sub": USER}, "test-key-with-at-least-thirty-two-bytes")
    with patch.object(SupabaseDataService, "get_authenticated_client", AsyncMock(return_value=client)), \
         patch.object(SupabaseDataService, "_close_authenticated_client", close):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(SupabaseDataService.fetch_aggregated_timesheets(token, COMPANY, [], "2026-10-01", "2026-10-31"))
    assert exc.value.status_code == 403
    close.assert_awaited_once_with(client)


@pytest.mark.parametrize("client_id", [None, "30000000-0000-0000-0000-000000000003"])
def test_first_module_does_not_grant_invoice_history_access(client_id):
    def table(name):
        if name == "profiles":
            return Query([{"id": USER, "company_id": COMPANY, "role": "company_admin", "is_active": True}])
        assert name == "companies", "Must reject before reading the invoice ledger"
        return Query([{"enabled_modules": ["leave_attachments"], "is_active": True, "is_deleted": False}])
    client = SimpleNamespace(table=table)
    token = jwt.encode({"sub": USER}, "test-key-with-at-least-thirty-two-bytes")
    with patch.object(SupabaseDataService, "get_authenticated_client", AsyncMock(return_value=client)), \
         patch.object(SupabaseDataService, "_close_authenticated_client", AsyncMock()):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(ClientInvoiceService.list_invoice_history(user_jwt=token, company_id=COMPANY, client_id=client_id))
    assert exc.value.status_code == 403
