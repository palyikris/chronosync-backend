import asyncio
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import subprocess
from unittest.mock import AsyncMock, patch
from xml.etree import ElementTree

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.schemas.invoices import InvoiceDateOverrides, InvoiceBatchReviewRequest
from app.services.client_invoice_service import ClientInvoiceService
from app.services.invoice_providers.szamlazz_hu import SzamlazzHuProvider
from app.services.invoice_terms import build_invoice_comment, resolve_invoice_dates
from app.services.supabase_service import SupabaseDataService
from test_project_invoice_pricing import Database, COMPANY, CLIENT, MONTHLY


@pytest.mark.parametrize("issue,end,reference,offset,expected", [
    ("2026-01-31", "2026-01-31", "issue_date", 1, "2026-02-28"),
    ("2028-01-31", "2028-01-31", "issue_date", 1, "2028-02-29"),
    ("2026-12-31", "2026-12-31", "issue_date", 2, "2027-02-28"),
    ("2026-11-03", "2026-10-31", "period_end", 1, "2026-11-30"),
    ("2026-11-03", "2026-10-31", "issue_date", 1, "2026-12-31"),
    ("2026-11-03", "2026-10-31", "issue_date", 0, "2026-11-30"),
])
def test_month_end_uses_calendar_and_selected_reference(issue, end, reference, offset, expected):
    due, fulfillment = resolve_invoice_dates(issue_date=date.fromisoformat(issue),
        period_end=date.fromisoformat(end), overrides=InvoiceDateOverrides(),
        profile={"payment_due_policy": "month_end", "payment_due_reference": reference,
            "payment_due_month_offset": offset, "fulfillment_date_policy": "due_date"})
    assert due == fulfillment == date.fromisoformat(expected)


def test_existing_terms_and_fulfillment_are_preserved():
    assert resolve_invoice_dates(issue_date=date(2026, 11, 3), period_end=date(2026, 10, 31),
        profile={"payment_term_days": 8}, overrides=InvoiceDateOverrides()) == (date(2026, 11, 11), date(2026, 10, 31))


@pytest.mark.parametrize("overrides", [
    {"due_date": "2026-11-02"}, {"due_date": "2027-11-05"},
    {"fulfillment_date": "2025-11-01"}, {"fulfillment_date": "2027-11-05"},
])
def test_manual_dates_are_bounded(overrides):
    with pytest.raises(ValueError):
        resolve_invoice_dates(issue_date=date(2026, 11, 3), period_end=date(2026, 10, 31),
            profile={"payment_term_days": 8}, overrides=InvoiceDateOverrides(**overrides))


def test_override_resolves_past_due_rule_and_fulfillment_follows_due_date():
    profile = {"payment_due_policy": "month_end", "payment_due_reference": "period_end",
        "payment_due_month_offset": 0, "fulfillment_date_policy": "due_date"}
    with pytest.raises(ValueError, match="before the issue date"):
        resolve_invoice_dates(issue_date=date(2026, 11, 3), period_end=date(2026, 10, 31),
            profile=profile, overrides=InvoiceDateOverrides())
    assert resolve_invoice_dates(issue_date=date(2026, 11, 3), period_end=date(2026, 10, 31),
        profile=profile, overrides=InvoiceDateOverrides(due_date="2026-11-20")) == (date(2026, 11, 20), date(2026, 11, 20))


@pytest.mark.parametrize("value", ["2026-02-30", "11/20/2026", "2026-11-20T12:00:00Z", 1795132800, [], {}])
def test_override_contract_rejects_non_calendar_dates(value):
    with pytest.raises(ValidationError):
        InvoiceDateOverrides(due_date=value)


def test_override_contract_cannot_change_issue_date_or_enter_automatic_batch():
    with pytest.raises(ValidationError):
        InvoiceDateOverrides(issue_date="2026-11-01")
    with pytest.raises(ValidationError):
        InvoiceBatchReviewRequest(company_id=COMPANY, client_ids=[CLIENT],
            period_start="2026-10-01", period_end="2026-10-31", date_overrides={"due_date": "2026-11-20"})


@pytest.mark.parametrize("language,label", [("en", "Service period"), ("hu", "Elszámolási időszak")])
def test_note_preserves_period_and_has_room_within_provider_limit(language, label):
    profile = {"invoice_language": language, "invoice_note": "A" * 450}
    comment = build_invoice_comment(period_start=date(2026, 10, 1), period_end=date(2026, 10, 31), profile=profile)
    assert comment.startswith(f"{label}: 2026-10-01 - 2026-10-31\n")
    assert len(comment) <= 500
    with pytest.raises(ValueError):
        build_invoice_comment(period_start=date(2026, 10, 1), period_end=date(2026, 10, 31),
            profile={**profile, "invoice_note": "A" * 451})


def run_review(db, overrides=None):
    return asyncio.run(ClientInvoiceService._build_review_context(user_jwt="fake", company_id=COMPANY,
        client_id=CLIENT, period_start=date(2026, 10, 1), period_end=date(2026, 10, 31),
        project_quantities={MONTHLY: Decimal(1)}, date_overrides=overrides))


@pytest.fixture
def db():
    database = Database()
    with (
        patch.object(SupabaseDataService, "get_authenticated_client", AsyncMock(return_value=database)),
        patch.object(SupabaseDataService, "_close_authenticated_client", AsyncMock()),
        patch.object(SupabaseDataService, "_assert_invoice_attachment_access_with_client", AsyncMock()),
        patch("app.services.client_invoice_service.datetime") as clock,
    ):
        clock.now.return_value = datetime(2026, 11, 10, 12)
        yield database


def test_note_and_dates_are_in_fingerprint_without_changing_time_or_totals(db):
    old, _ = run_review(db)
    db.tables["client_billing_profiles"][0]["invoice_note"] = "Reviewed & approved <services>\nSecond line"
    noted, _ = run_review(db)
    changed, invoice = run_review(db, InvoiceDateOverrides(due_date="2026-12-31", fulfillment_date="2026-11-15"))
    assert len({old.review_fingerprint, noted.review_fingerprint, changed.review_fingerprint}) == 3
    assert old.items == changed.items and old.approved_entries == changed.approved_entries
    assert old.gross_total == changed.gross_total
    assert invoice.due_date == changed.due_date == date(2026, 12, 31)
    assert invoice.fulfillment_date == changed.fulfillment_date == date(2026, 11, 15)
    assert invoice.comment == changed.invoice_comment
    schema = Path(__file__).parent / "fixtures/szamlazz/xmlszamla.xsd"
    # Ordinary recipient avoids the documented upstream group-field schema discrepancy.
    invoice.customer.group_tax_number = None
    xml = SzamlazzHuProvider("dummy-not-a-credential").build_invoice_xml(invoice)
    subprocess.run(["xmllint", "--nonet", "--schema", str(schema), "--noout", "-"],
        input=xml, capture_output=True, check=True)
    root = ElementTree.fromstring(xml)
    ns = {"i": "http://www.szamlazz.hu/xmlszamla"}
    assert root.findtext("i:fejlec/i:megjegyzes", namespaces=ns) == changed.invoice_comment
    assert root.findtext("i:fejlec/i:fizetesiHataridoDatum", namespaces=ns) == "2026-12-31"


@pytest.mark.parametrize("change", ["note", "dates", "rule"])
def test_changed_dates_or_note_never_reach_reservation_or_provider_with_old_review(db, change):
    old, _ = run_review(db)
    overrides = InvoiceDateOverrides(due_date="2026-12-31") if change == "dates" else None
    if change == "note":
        db.tables["client_billing_profiles"][0]["invoice_note"] = "Changed after review"
    if change == "rule":
        db.tables["client_billing_profiles"][0].update(payment_due_policy="month_end", payment_due_month_offset=1)
    with patch.object(ClientInvoiceService, "_assert_live_invoice_mode", AsyncMock()) as mode:
        with pytest.raises(HTTPException) as raised:
            asyncio.run(ClientInvoiceService.issue_client_invoice(user_jwt="fake", company_id=COMPANY,
                client_id=CLIENT, period_start=date(2026, 10, 1), period_end=date(2026, 10, 31),
                project_quantities={MONTHLY: Decimal(1)}, review_fingerprint=old.review_fingerprint, date_overrides=overrides))
        assert raised.value.status_code == 409
        mode.assert_not_awaited()
    with pytest.raises(HTTPException) as raised:
        asyncio.run(ClientInvoiceService.build_preview_invoice(user_jwt="fake", company_id=COMPANY,
            client_id=CLIENT, period_start=date(2026, 10, 1), period_end=date(2026, 10, 31),
            project_quantities={MONTHLY: Decimal(1)}, review_fingerprint=old.review_fingerprint, date_overrides=overrides))
    assert raised.value.status_code == 409


def test_default_and_explicit_empty_overrides_are_identical_and_preview_keeps_dates(db):
    review, _ = run_review(db)
    assert review.review_fingerprint == run_review(db, InvoiceDateOverrides())[0].review_fingerprint
    overrides = InvoiceDateOverrides(due_date="2026-12-31")
    reviewed, _ = run_review(db, overrides)
    preview, _ = asyncio.run(ClientInvoiceService.build_preview_invoice(user_jwt="fake", company_id=COMPANY,
        client_id=CLIENT, period_start=date(2026, 10, 1), period_end=date(2026, 10, 31),
        project_quantities={MONTHLY: Decimal(1)}, date_overrides=overrides,
        review_fingerprint=reviewed.review_fingerprint))
    assert preview.preview and not preview.customer.send_email
    assert preview.due_date == reviewed.due_date
