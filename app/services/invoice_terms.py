"""Resolve client invoice text and dates before review, preview and issuance."""
import calendar
from datetime import date, timedelta
from typing import Any

from app.schemas.invoices import InvoiceDateOverrides


def resolve_invoice_dates(
    *, issue_date: date, period_end: date, profile: dict[str, Any],
    overrides: InvoiceDateOverrides,
) -> tuple[date, date]:
    policy = profile.get("payment_due_policy", "days_after_issue")
    if policy == "days_after_issue":
        due_date = issue_date + timedelta(days=int(profile["payment_term_days"]))
    elif policy == "month_end":
        reference = profile.get("payment_due_reference", "issue_date")
        if reference not in {"issue_date", "period_end"}:
            raise ValueError("Invalid payment deadline reference.")
        anchor = issue_date if reference == "issue_date" else period_end
        offset = int(profile.get("payment_due_month_offset", 1))
        if not 0 <= offset <= 2:
            raise ValueError("Invalid payment deadline month offset.")
        year, month_index = divmod(anchor.year * 12 + anchor.month - 1 + offset, 12)
        due_date = date(year, month_index + 1, calendar.monthrange(year, month_index + 1)[1])
    else:
        raise ValueError("Invalid payment deadline policy.")
    due_date = overrides.due_date or due_date
    if due_date < issue_date:
        raise ValueError("The payment deadline is before the issue date. Adjust the deadline or client rule.")
    if overrides.due_date and due_date > issue_date + timedelta(days=366):
        raise ValueError("A manual payment deadline must be within 366 days of the issue date.")
    fulfillment_policy = profile.get("fulfillment_date_policy", "period_end")
    dates = {"period_end": period_end, "due_date": due_date, "issue_date": issue_date}
    if fulfillment_policy not in dates:
        raise ValueError("Invalid fulfillment date policy.")
    fulfillment_date = overrides.fulfillment_date or dates[fulfillment_policy]
    if overrides.fulfillment_date and abs((fulfillment_date - issue_date).days) > 366:
        raise ValueError("A manual fulfillment date must be within 366 days of the issue date.")
    return due_date, fulfillment_date


def build_invoice_comment(*, period_start: date, period_end: date, profile: dict[str, Any]) -> str:
    note = str(profile.get("invoice_note") or "").strip(" ")
    if len(note) > 450:
        raise ValueError("The invoice note cannot exceed 450 characters.")
    label = "Elszámolási időszak" if profile["invoice_language"] == "hu" else "Service period"
    period = f"{label}: {period_start.isoformat()} - {period_end.isoformat()}"
    return f"{period}\n{note}" if note else period
