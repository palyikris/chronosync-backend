import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
import pytest
from postgrest import AsyncPostgrestClient

from app.main import app
from app.services.client_invoice_service import ClientInvoiceService
from app.services.supabase_service import SupabaseDataService

COMPANY = UUID("10000000-0000-0000-0000-000000000001")
CLIENT = UUID("20000000-0000-0000-0000-000000000002")
OTHER_CLIENT = UUID("20000000-0000-0000-0000-000000000003")
OTHER_COMPANY = UUID("10000000-0000-0000-0000-000000000004")


def history_row(index, client_id, company_id=COMPANY):
    return {
        "id": str(UUID(int=index)), "company_id": str(company_id),
        "client_id": str(client_id), "recipient_name": "Synthetic client",
        "period_start": "2026-10-01", "period_end": "2026-10-31",
        "status": "issued", "provider": "szamlazz_hu", "external_id": f"test-{index}",
        "quantity": "1", "unit": "hó", "net_unit_price": "100", "vat_code": "27",
        "currency": "EUR", "net_total": "100", "vat_total": "27", "gross_total": "127",
        "invoice_number": f"TEST-{index}", "created_at": f"2026-11-06T00:00:{index:02d}Z",
        "updated_at": "2026-11-06T00:00:00Z",
    }


@pytest.mark.parametrize("client_id", [None, CLIENT])
def test_history_filters_by_company_and_client_before_limiting(client_id):
    # This client's invoice is older than the 50 most recent company invoices.
    rows = [history_row(index, OTHER_CLIENT) for index in range(2, 57)]
    rows += [history_row(1, CLIENT), history_row(59, CLIENT, OTHER_COMPANY)]
    reads = []

    def transport(request):
        if request.url.path.endswith("/rpc/expire_stale_manual_invoice_attempts"):
            return httpx.Response(200, json=[])
        assert request.method == "GET"
        assert request.url.path.endswith("/invoice_ledger")
        query = request.url.params
        reads.append(query)
        result = rows
        for field in ("company_id", "client_id"):
            if field in query:
                assert query[field].startswith("eq.")
                result = [row for row in result if row[field] == query[field][3:]]
        result = sorted(result, key=lambda row: row["created_at"], reverse=True)
        return httpx.Response(200, json=result[:int(query["limit"])])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            db = AsyncPostgrestClient("https://history.invalid/rest/v1", http_client=http)
            client = SimpleNamespace(table=db.from_, rpc=db.rpc)
            close = AsyncMock()
            with (
                patch.object(SupabaseDataService, "get_authenticated_client", AsyncMock(return_value=client)),
                patch.object(SupabaseDataService, "_assert_invoice_attachment_access_with_client", AsyncMock()),
                patch.object(SupabaseDataService, "_close_authenticated_client", close),
            ):
                result = await ClientInvoiceService.list_invoice_history(
                    user_jwt="test", company_id=COMPANY, client_id=client_id,
                )
            close.assert_awaited_once_with(client)
            return result

    result = asyncio.run(run())
    assert len(reads) == 1
    assert reads[0]["company_id"] == f"eq.{COMPANY}"
    assert reads[0]["order"] == "created_at.desc"
    assert all(item.company_id == COMPANY for item in result)
    if client_id:
        assert reads[0]["client_id"] == f"eq.{CLIENT}"
        assert [item.invoice_number for item in result] == ["TEST-1"]
    else:
        assert "client_id" not in reads[0]
        assert len(result) == 50
        assert all(item.client_id == OTHER_CLIENT for item in result)


@pytest.mark.parametrize("client_id", [None, str(CLIENT), "invalid-client"])
def test_history_route_validates_and_passes_optional_client_scope(client_id):
    handler = AsyncMock(return_value=[])

    async def request():
        params = {"company_id": str(COMPANY)}
        if client_id is not None:
            params["client_id"] = client_id
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.get("/api/v1/invoices/history", params=params,
                                    headers={"Authorization": "Bearer test"})

    with patch.object(ClientInvoiceService, "list_invoice_history", handler):
        response = asyncio.run(request())
    if client_id == "invalid-client":
        assert response.status_code == 422
        handler.assert_not_awaited()
    else:
        assert response.status_code == 200
        handler.assert_awaited_once_with(user_jwt="test", company_id=COMPANY, limit=50,
                                        client_id=UUID(client_id) if client_id else None)
