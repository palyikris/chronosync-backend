import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.main import app
from app.services.client_invoice_service import ClientInvoiceService


@pytest.mark.parametrize('override', [
    {}, {'confirm':False}, {'confirm':'true'}, {'confirm':True,'review_fingerprint':'bad'},
    {'confirm':True,'period_end':'2026-08-31'},
    {'confirm':True,'period_end':'2028-09-30'},
])
def test_issue_route_rejects_unconfirmed_or_invalid_payload(override):
    payload={'company_id':'10000000-0000-0000-0000-000000000001',
             'client_id':'20000000-0000-0000-0000-000000000002',
             'period_start':'2026-09-01','period_end':'2026-09-30','review_fingerprint':'a'*64,**override}
    handler=AsyncMock()
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            return await client.post('/api/v1/invoices/client-issue',headers={'Authorization':'Bearer dummy'},json=payload)
    with patch.object(ClientInvoiceService,'issue_client_invoice',handler):
        response=asyncio.run(request())
    assert response.status_code==422
    handler.assert_not_awaited()


def test_issue_route_requires_bearer_token():
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            return await client.post('/api/v1/invoices/client-issue',json={})
    assert asyncio.run(request()).status_code in {401,403}
