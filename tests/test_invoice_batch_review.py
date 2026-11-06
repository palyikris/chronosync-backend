import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from test_manual_invoice_issue import CLIENT_ID, COMPANY_ID, invoice_review

from app.api.v1.endpoints.invoices import router
from app.schemas.invoices import InvoiceBatchReviewRequest
from app.services.client_invoice_service import ClientInvoiceService
from app.services.company_service import CompanyCredentialService
from app.services.invoice_batch_service import InvoiceBatchService
from app.services.supabase_service import SupabaseDataService

SECOND_CLIENT = UUID("20000000-0000-0000-0000-000000000003")


def payload(**overrides):
    return InvoiceBatchReviewRequest.model_validate(
        {
            "company_id": COMPANY_ID,
            "client_ids": [CLIENT_ID, SECOND_CLIENT],
            "period_start": "2026-09-01",
            "period_end": "2026-09-30",
            **overrides,
        }
    )


@pytest.fixture
def batch(monkeypatch):
    query = Mock()
    for method in ("select", "eq", "in_", "lte", "gte", "limit"):
        getattr(query, method).return_value = query
    query.execute = AsyncMock(return_value=SimpleNamespace(data=[]))
    supabase = SimpleNamespace(table=Mock(return_value=query))
    access = AsyncMock()
    close = AsyncMock()
    live_mode = AsyncMock()
    review = AsyncMock(
        side_effect=lambda **kwargs: invoice_review().model_copy(
            update={"client_id": kwargs["client_id"]}
        )
    )
    reserve = AsyncMock()
    credentials = Mock()

    @asynccontextmanager
    async def credential_context(*args):
        credentials(*args)
        yield "synthetic-test-key"

    monkeypatch.setattr(
        SupabaseDataService,
        "get_authenticated_client",
        AsyncMock(return_value=supabase),
    )
    monkeypatch.setattr(
        SupabaseDataService, "_assert_invoice_attachment_access_with_client", access
    )
    monkeypatch.setattr(SupabaseDataService, "_close_authenticated_client", close)
    monkeypatch.setattr(ClientInvoiceService, "_assert_live_invoice_mode", live_mode)
    monkeypatch.setattr(ClientInvoiceService, "build_invoice_review", review)
    monkeypatch.setattr(ClientInvoiceService, "_reserve_invoice", reserve)
    monkeypatch.setattr(
        CompanyCredentialService, "decrypted_szamlazz_agent_key", credential_context
    )
    return SimpleNamespace(
        query=query,
        supabase=supabase,
        access=access,
        close=close,
        live_mode=live_mode,
        review=review,
        reserve=reserve,
        credentials=credentials,
    )


def run_batch():
    return asyncio.run(
        InvoiceBatchService.review(user_jwt="test-jwt", payload=payload())
    )


def test_preflight_returns_exact_reviews_and_checks_tenant_scoped_overlaps(batch):
    result = run_batch()
    assert [item.client_id for item in result] == [CLIENT_ID, SECOND_CLIENT]
    assert all(item.review.review_fingerprint == "a" * 64 for item in result)
    assert all(item.reason is None for item in result)
    batch.query.eq.assert_any_call("company_id", str(COMPANY_ID))
    batch.query.eq.assert_any_call("client_id", str(SECOND_CLIENT))
    batch.query.in_.assert_called_with("status", ["issuing", "issued", "uncertain"])
    batch.query.lte.assert_called_with("period_start", "2026-09-30")
    batch.query.gte.assert_called_with("period_end", "2026-09-01")
    batch.reserve.assert_not_awaited()
    batch.access.assert_awaited_once_with(
        batch.supabase,
        user_jwt="test-jwt",
        company_id=COMPANY_ID,
        required_module="invoicing",
    )
    batch.credentials.assert_called_once()
    batch.close.assert_awaited_once_with(batch.supabase)


@pytest.mark.parametrize(
    "reason",
    [
        "Complete the client's billing profile before invoicing.",
        "Project 'Payroll' has no 2026 indexed price.",
        "Enter the invoice quantity (person) for project 'Payroll'.",
        "The billable projects must use one currency for this invoice.",
    ],
)
def test_missing_metadata_or_manual_quantities_skip_only_that_client(batch, reason):
    batch.review.side_effect = [
        HTTPException(409, reason),
        invoice_review().model_copy(update={"client_id": SECOND_CLIENT}),
    ]
    result = run_batch()
    assert result[0].review is None
    assert result[0].reason == reason
    assert result[1].review.client_id == SECOND_CLIENT


def test_already_issued_or_unresolved_period_is_flagged_before_review(batch):
    batch.query.execute.side_effect = [
        SimpleNamespace(data=[{"id": "existing"}]),
        SimpleNamespace(data=[]),
    ]
    result = run_batch()
    assert "overlaps" in result[0].reason
    assert result[0].review is None
    assert result[1].review is not None
    batch.review.assert_awaited_once()


def test_preview_mode_flags_all_clients_but_keeps_attachment_selection(batch):
    batch.live_mode.side_effect = HTTPException(
        409, "Live invoice issuance is disabled in preview mode."
    )
    result = run_batch()
    assert len(result) == 2
    assert all(item.review is None and "preview mode" in item.reason for item in result)
    batch.review.assert_not_awaited()
    batch.reserve.assert_not_awaited()


def test_missing_provider_key_is_reported_without_contacting_provider(
    batch, monkeypatch
):
    @asynccontextmanager
    async def missing_key(*args):
        raise HTTPException(404, "No Számla Agent key is configured for this company.")
        yield

    monkeypatch.setattr(
        CompanyCredentialService, "decrypted_szamlazz_agent_key", missing_key
    )
    assert all("No Számla Agent key" in item.reason for item in run_batch())
    batch.review.assert_not_awaited()


@pytest.mark.parametrize("status_code", [401, 403, 502, 503])
def test_review_authorization_failures_and_outages_are_never_silent_skips(
    batch, status_code
):
    batch.review.side_effect = HTTPException(status_code, "Unavailable")
    with pytest.raises(HTTPException) as raised:
        run_batch()
    assert raised.value.status_code == status_code
    batch.close.assert_awaited_once()


def test_non_admin_cannot_preflight(batch):
    batch.access.side_effect = HTTPException(403, "Admin required")
    with pytest.raises(HTTPException) as raised:
        run_batch()
    assert raised.value.status_code == 403
    batch.review.assert_not_awaited()
    batch.credentials.assert_not_called()


@pytest.mark.parametrize(
    "overrides",
    [
        {"client_ids": []},
        {"client_ids": [CLIENT_ID, CLIENT_ID]},
        {"client_ids": [UUID(int=i) for i in range(101)]},
        {"period_end": "2026-08-31"},
        {"period_end": "2028-01-01"},
        {"confirm": True},
    ],
)
def test_preflight_rejects_invalid_batch_selection(overrides):
    with pytest.raises(ValidationError):
        payload(**overrides)


def test_batch_review_route_requires_auth_and_serializes_fingerprints(batch):
    app = FastAPI()
    app.include_router(router, prefix="/invoices")

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/invoices/batch-review", json=payload().model_dump(mode="json")
            )
            assert response.status_code == 401
            return await client.post(
                "/invoices/batch-review",
                json=payload().model_dump(mode="json"),
                headers={"Authorization": "Bearer test-jwt"},
            )

    response = asyncio.run(request())
    assert response.status_code == 200
    assert response.json()[0]["review"]["review_fingerprint"] == "a" * 64
    assert response.json()[0]["reason"] is None


def test_oversized_required_report_skips_only_its_client(batch):
    batch.review.side_effect=[HTTPException(413,'The billing report exceeds the 2 MB attachment limit.'),
        invoice_review().model_copy(update={'client_id':SECOND_CLIENT})]
    result=run_batch()
    assert result[0].review is None and '2 MB' in result[0].reason
    assert result[1].review.client_id==SECOND_CLIENT
