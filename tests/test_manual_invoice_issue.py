import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.schemas.invoices import (
    ClientInvoiceReview,
    InvoiceCreateRequest,
    InvoiceHistoryItem,
)
from app.services.client_invoice_service import ClientInvoiceService
from app.services.company_service import CompanyCredentialService
from app.services.invoice_providers.szamlazz_hu import (
    SzamlazzHuProvider,
    SzamlazzHuRejectedError,
    SzamlazzHuRequestError,
    SzamlazzHuResponseError,
)


COMPANY_ID = UUID("10000000-0000-0000-0000-000000000001")
CLIENT_ID = UUID("20000000-0000-0000-0000-000000000002")
INVOICE_ID = UUID("30000000-0000-0000-0000-000000000003")
TIMESHEET_ID = UUID("40000000-0000-0000-0000-000000000004")


def invoice_request() -> InvoiceCreateRequest:
    return InvoiceCreateRequest.model_validate(
        {
            "issue_date": "2026-10-22",
            "fulfillment_date": "2026-09-30",
            "due_date": "2026-10-30",
            "payment_method": "Átutalás",
            "currency": "HUF",
            "language": "hu",
            "order_number": "cs-review",
            "external_id": "cs-review",
            "preview": False,
            "customer": {
                "name": "Minta Kft.",
                "country": "HU",
                "postal_code": "1111",
                "city": "Budapest",
                "address": "Teszt utca 1.",
                "email": "billing@example.test",
                "send_email": True,
                "tax_subject": 1,
                "tax_number": "12345678-2-41",
            },
            "items": [
                {
                    "name": "Szoftverfejlesztés",
                    "quantity": "2.5",
                    "unit": "óra",
                    "net_unit_price": "10000",
                    "vat_rate": "27",
                }
            ],
        }
    )


def invoice_review() -> ClientInvoiceReview:
    return ClientInvoiceReview.model_validate(
        {
            "company_id": COMPANY_ID,
            "client_id": CLIENT_ID,
            "client_code": "MINTA",
            "client_name": "Minta Kft.",
            "period_start": "2026-09-01",
            "period_end": "2026-09-30",
            "issue_date": "2026-10-22",
            "fulfillment_date": "2026-09-30",
            "due_date": "2026-10-30",
            "payment_method": "Átutalás",
            "language": "hu",
            "billing_model": "hourly",
            "item_name": "Szoftverfejlesztés",
            "quantity": "2.5",
            "unit": "óra",
            "net_unit_price": "10000",
            "vat_code": "27",
            "currency": "HUF",
            "net_total": "25000",
            "vat_total": "6750",
            "gross_total": "31750",
            "recipient": {
                "name": "Minta Kft.",
                "country": "HU",
                "postal_code": "1111",
                "city": "Budapest",
                "address": "Teszt utca 1.",
                "email": "billing@example.test",
                "send_email": True,
                "tax_subject": 1,
                "tax_number": "12345678-2-41",
            },
            "approved_entries": [
                {
                    "id": TIMESHEET_ID,
                    "work_date": "2026-09-15",
                    "hours_logged": "2.5",
                    "description": "Implementation",
                    "project_name": "ChronoSync",
                }
            ],
            "review_fingerprint": "a" * 64,
        }
    )


def history_item() -> InvoiceHistoryItem:
    now = datetime.now(UTC)
    return InvoiceHistoryItem(
        id=INVOICE_ID,
        company_id=COMPANY_ID,
        client_id=CLIENT_ID,
        client_name="Minta Kft.",
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
        status="issued",
        provider="szamlazz_hu",
        external_id=f"chronosync-{INVOICE_ID}",
        recipient_name="Minta Kft.",
        recipient_email="billing@example.test",
        send_email=True,
        quantity=Decimal("2.5"),
        unit="óra",
        net_unit_price=Decimal("10000"),
        vat_code="27",
        currency="HUF",
        net_total=Decimal("25000"),
        vat_total=Decimal("6750"),
        gross_total=Decimal("31750"),
        invoice_number="E-2026-1",
        pdf_available=True,
        attempt_count=1,
        issued_at=now,
        created_at=now,
        updated_at=now,
    )


@asynccontextmanager
async def fake_agent_key(*_args, **_kwargs):
    yield "agent-key"


def test_confirmed_provider_success_finalizes_ledger_after_single_live_call():
    review = invoice_review()
    invoice = invoice_request()
    build_context = AsyncMock(return_value=(review, invoice))
    reserve = AsyncMock(return_value=INVOICE_ID)
    create_invoice = AsyncMock(
        return_value={
            "invoice_number": "E-2026-1",
            "net_total": Decimal("25000"),
            "gross_total": Decimal("31750"),
            "pdf": b"%PDF-live",
        }
    )
    finalize = AsyncMock()
    fetch_history = AsyncMock(return_value=history_item())
    assert_live_mode = AsyncMock()

    with (
        patch.object(ClientInvoiceService, "_build_review_context", build_context),
        patch.object(
            ClientInvoiceService,
            "_assert_live_invoice_mode",
            assert_live_mode,
        ),
        patch.object(ClientInvoiceService, "_reserve_invoice", reserve),
        patch.object(
            CompanyCredentialService,
            "decrypted_szamlazz_agent_key",
            fake_agent_key,
        ),
        patch.object(SzamlazzHuProvider, "create_invoice", create_invoice),
        patch.object(ClientInvoiceService, "_finalize_invoice", finalize),
        patch.object(
            ClientInvoiceService, "get_invoice_history_item", fetch_history
        ),
    ):
        result = asyncio.run(
            ClientInvoiceService.issue_client_invoice(
                user_jwt="jwt",
                company_id=COMPANY_ID,
                client_id=CLIENT_ID,
                period_start=date(2026, 9, 1),
                period_end=date(2026, 9, 30),
                review_fingerprint="a" * 64,
            )
        )

    submitted = create_invoice.await_args.args[0]
    assert submitted["preview"] is False
    assert submitted["customer"]["send_email"] is True
    assert submitted["external_id"] == f"chronosync-{INVOICE_ID}"
    finalize.assert_awaited_once()
    assert_live_mode.assert_awaited_once_with(
        user_jwt="jwt", company_id=COMPANY_ID
    )
    assert result.invoice_number == "E-2026-1"


def test_network_ambiguity_marks_attempt_uncertain_and_never_finalizes():
    review = invoice_review()
    invoice = invoice_request()
    mark_attempt = AsyncMock()
    finalize = AsyncMock()

    with (
        patch.object(
            ClientInvoiceService,
            "_build_review_context",
            AsyncMock(return_value=(review, invoice)),
        ),
        patch.object(
            ClientInvoiceService,
            "_reserve_invoice",
            AsyncMock(return_value=INVOICE_ID),
        ),
        patch.object(
            ClientInvoiceService,
            "_assert_live_invoice_mode",
            AsyncMock(),
        ),
        patch.object(
            CompanyCredentialService,
            "decrypted_szamlazz_agent_key",
            fake_agent_key,
        ),
        patch.object(
            SzamlazzHuProvider,
            "create_invoice",
            AsyncMock(side_effect=SzamlazzHuRequestError("timeout")),
        ),
        patch.object(ClientInvoiceService, "_mark_attempt", mark_attempt),
        patch.object(ClientInvoiceService, "_finalize_invoice", finalize),
    ):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                ClientInvoiceService.issue_client_invoice(
                    user_jwt="jwt",
                    company_id=COMPANY_ID,
                    client_id=CLIENT_ID,
                    period_start=date(2026, 9, 1),
                    period_end=date(2026, 9, 30),
                    review_fingerprint="a" * 64,
                )
            )

    assert exc_info.value.status_code == 502
    assert mark_attempt.await_args.kwargs["attempt_status"] == "uncertain"
    finalize.assert_not_awaited()


@pytest.mark.parametrize(
    ("provider_error", "expected_status", "expected_attempt_status"),
    [
        (SzamlazzHuRejectedError("invalid request", code="57"), 422, "failed"),
        (SzamlazzHuResponseError("invalid success payload"), 502, "uncertain"),
    ],
)
def test_only_explicit_provider_rejection_releases_invoice_for_retry(
    provider_error, expected_status, expected_attempt_status
):
    mark_attempt = AsyncMock()
    finalize = AsyncMock()

    with (
        patch.object(
            ClientInvoiceService,
            "_build_review_context",
            AsyncMock(return_value=(invoice_review(), invoice_request())),
        ),
        patch.object(
            ClientInvoiceService,
            "_reserve_invoice",
            AsyncMock(return_value=INVOICE_ID),
        ),
        patch.object(
            ClientInvoiceService,
            "_assert_live_invoice_mode",
            AsyncMock(),
        ),
        patch.object(
            CompanyCredentialService,
            "decrypted_szamlazz_agent_key",
            fake_agent_key,
        ),
        patch.object(
            SzamlazzHuProvider,
            "create_invoice",
            AsyncMock(side_effect=provider_error),
        ),
        patch.object(ClientInvoiceService, "_mark_attempt", mark_attempt),
        patch.object(ClientInvoiceService, "_finalize_invoice", finalize),
    ):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                ClientInvoiceService.issue_client_invoice(
                    user_jwt="jwt",
                    company_id=COMPANY_ID,
                    client_id=CLIENT_ID,
                    period_start=date(2026, 9, 1),
                    period_end=date(2026, 9, 30),
                    review_fingerprint="a" * 64,
                )
            )

    assert exc_info.value.status_code == expected_status
    assert mark_attempt.await_args.kwargs["attempt_status"] == expected_attempt_status
    finalize.assert_not_awaited()


def test_huf_review_totals_match_provider_rounding():
    assert ClientInvoiceService._calculate_totals(invoice_request()) == (
        Decimal("25000"),
        Decimal("6750"),
        Decimal("31750"),
    )


def test_preview_mode_blocks_before_reserving_or_calling_provider():
    reserve = AsyncMock()
    create_invoice = AsyncMock()
    preview_mode_error = HTTPException(
        status_code=409,
        detail="Live invoice issuance is disabled while the company is in preview mode.",
    )

    with (
        patch.object(
            ClientInvoiceService,
            "_build_review_context",
            AsyncMock(return_value=(invoice_review(), invoice_request())),
        ),
        patch.object(
            ClientInvoiceService,
            "_assert_live_invoice_mode",
            AsyncMock(side_effect=preview_mode_error),
        ),
        patch.object(ClientInvoiceService, "_reserve_invoice", reserve),
        patch.object(SzamlazzHuProvider, "create_invoice", create_invoice),
    ):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                ClientInvoiceService.issue_client_invoice(
                    user_jwt="jwt",
                    company_id=COMPANY_ID,
                    client_id=CLIENT_ID,
                    period_start=date(2026, 9, 1),
                    period_end=date(2026, 9, 30),
                    review_fingerprint="a" * 64,
                )
            )

    assert exc_info.value.status_code == 409
    reserve.assert_not_awaited()
    create_invoice.assert_not_awaited()


@pytest.mark.parametrize('code',['71','152'])
def test_duplicate_provider_rejection_stays_locked(code):
    mark=AsyncMock()
    with (
        patch.object(ClientInvoiceService,'_build_review_context',AsyncMock(return_value=(invoice_review(),invoice_request()))),
        patch.object(ClientInvoiceService,'_reserve_invoice',AsyncMock(return_value=INVOICE_ID)),
        patch.object(ClientInvoiceService,'_assert_live_invoice_mode',AsyncMock()),
        patch.object(CompanyCredentialService,'decrypted_szamlazz_agent_key',fake_agent_key),
        patch.object(SzamlazzHuProvider,'create_invoice',AsyncMock(side_effect=SzamlazzHuRejectedError('Duplicate',code=code))),
        patch.object(ClientInvoiceService,'_mark_attempt',mark),
    ):
        with pytest.raises(HTTPException):
            asyncio.run(issue())
    assert mark.await_args.kwargs['attempt_status']=='uncertain'


async def issue(fingerprint='a'*64):
    return await ClientInvoiceService.issue_client_invoice(user_jwt='jwt',company_id=COMPANY_ID,
        client_id=CLIENT_ID,period_start=date(2026,9,1),period_end=date(2026,9,30),review_fingerprint=fingerprint)


def test_stale_review_never_reserves_or_calls_provider():
    reserve=AsyncMock()
    provider=AsyncMock()
    with (patch.object(ClientInvoiceService,'_build_review_context',AsyncMock(return_value=(invoice_review(),invoice_request()))),
          patch.object(ClientInvoiceService,'_reserve_invoice',reserve),
          patch.object(SzamlazzHuProvider,'create_invoice',provider)):
        with pytest.raises(HTTPException) as error:
            asyncio.run(issue('b'*64))
    assert error.value.status_code==409
    reserve.assert_not_awaited()
    provider.assert_not_awaited()


@pytest.mark.parametrize('overrides',[
    {'invoice_number':None},{'net_total':'24999'},{'gross_total':'31751'},
    {'net_total':'NaN'},{'pdf':None},{'pdf':b'not-pdf'},
])
def test_incomplete_provider_success_never_finalizes(overrides):
    result={'invoice_number':'E-2026-1','net_total':'25000','gross_total':'31750','pdf':b'%PDF-live',**overrides}
    mark=AsyncMock()
    finalize=AsyncMock()
    with (
        patch.object(ClientInvoiceService,'_build_review_context',AsyncMock(return_value=(invoice_review(),invoice_request()))),
        patch.object(ClientInvoiceService,'_reserve_invoice',AsyncMock(return_value=INVOICE_ID)),
        patch.object(ClientInvoiceService,'_assert_live_invoice_mode',AsyncMock()),
        patch.object(CompanyCredentialService,'decrypted_szamlazz_agent_key',fake_agent_key),
        patch.object(SzamlazzHuProvider,'create_invoice',AsyncMock(return_value=result)),
        patch.object(ClientInvoiceService,'_mark_attempt',mark),
        patch.object(ClientInvoiceService,'_finalize_invoice',finalize),
    ):
        with pytest.raises(HTTPException):
            asyncio.run(issue())
    assert mark.await_args.kwargs['attempt_status']=='uncertain'
    finalize.assert_not_awaited()


def test_failure_after_provider_call_is_not_treated_as_configuration_rejection():
    @asynccontextmanager
    async def key_with_cleanup_error(*args,**kwargs):
        yield 'dummy-key'
        raise HTTPException(502,'Credential connection cleanup failed')
    mark=AsyncMock()
    with (
        patch.object(ClientInvoiceService,'_build_review_context',AsyncMock(return_value=(invoice_review(),invoice_request()))),
        patch.object(ClientInvoiceService,'_reserve_invoice',AsyncMock(return_value=INVOICE_ID)),
        patch.object(ClientInvoiceService,'_assert_live_invoice_mode',AsyncMock()),
        patch.object(CompanyCredentialService,'decrypted_szamlazz_agent_key',key_with_cleanup_error),
        patch.object(SzamlazzHuProvider,'create_invoice',AsyncMock(return_value={})),
        patch.object(ClientInvoiceService,'_mark_attempt',mark),
    ):
        with pytest.raises(HTTPException):
            asyncio.run(issue())
    assert mark.await_args.kwargs['attempt_status']=='uncertain'


def test_committed_finalization_with_lost_response_recovers_from_history():
    mark=AsyncMock()
    with (
        patch.object(ClientInvoiceService,'_build_review_context',AsyncMock(return_value=(invoice_review(),invoice_request()))),
        patch.object(ClientInvoiceService,'_reserve_invoice',AsyncMock(return_value=INVOICE_ID)),
        patch.object(ClientInvoiceService,'_assert_live_invoice_mode',AsyncMock()),
        patch.object(CompanyCredentialService,'decrypted_szamlazz_agent_key',fake_agent_key),
        patch.object(SzamlazzHuProvider,'create_invoice',AsyncMock(return_value={'invoice_number':'E-2026-1','net_total':'25000','gross_total':'31750','pdf':b'%PDF-live'})),
        patch.object(ClientInvoiceService,'_finalize_invoice',AsyncMock(side_effect=RuntimeError('Lost response after commit'))),
        patch.object(ClientInvoiceService,'get_invoice_history_item',AsyncMock(return_value=history_item())),
        patch.object(ClientInvoiceService,'_mark_attempt',mark),
    ):
        result=asyncio.run(issue())
    assert result.status=='issued'
    mark.assert_not_awaited()


@pytest.mark.parametrize('rpc_result',[None,[],{},[{'invoice_id':'invalid'}]])
def test_reservation_requires_a_valid_database_confirmation(rpc_result):
    from types import SimpleNamespace
    from app.services.supabase_service import SupabaseDataService
    execute=AsyncMock(return_value=SimpleNamespace(data=rpc_result))
    class Client:
        def rpc(self,*args):
            return SimpleNamespace(execute=execute)
    with (patch.object(SupabaseDataService,'get_authenticated_client',AsyncMock(return_value=Client())),
          patch.object(SupabaseDataService,'_close_authenticated_client',AsyncMock())):
        with pytest.raises(HTTPException):
            asyncio.run(ClientInvoiceService._reserve_invoice(user_jwt='jwt',review=invoice_review()))
