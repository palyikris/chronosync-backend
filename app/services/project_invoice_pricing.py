"""Resolve invoice lines exclusively from tenant-scoped annual project prices."""
import calendar
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any, Callable
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError

from app.schemas.invoices import InvoiceLineItem, InvoiceReviewEntry, InvoiceReviewProjectLine
from app.services.invoice_providers.amounts import calculate_line_totals

HOURLY_UNITS = {"óra", "hour", "hours", "h", "hr"}
MONTHLY_UNITS = {"hó", "hónap", "month"}


async def _pages(factory: Callable[[], Any]) -> list[dict[str, Any]]:
    rows = []
    while True:
        response = await factory().range(len(rows), len(rows) + 999).execute()
        page = response.data or []
        if not isinstance(page, list):
            raise HTTPException(502, "Invalid project pricing response.")
        if not page:
            return rows
        rows.extend(page)
        if len(rows) > 50000:
            raise HTTPException(413, "Too many project pricing records.")


async def build_project_lines(
    supabase: Any, *, company_id: UUID, client_id: UUID,
    period_start: date, period_end: date, entries: list[InvoiceReviewEntry],
    project_quantities: dict[UUID, Decimal], vat_code: str, tax_subject: int,
) -> list[InvoiceReviewProjectLine]:
    if period_start.year != period_end.year:
        raise HTTPException(409, "Select an invoice period within one pricing year.")
    # Idempotent recovery if the scheduled New Year rollover has not run yet.
    await supabase.rpc("ensure_company_project_pricing", {"p_company_id": str(company_id)}).execute()
    projects = await _pages(lambda: supabase.table("projects")
        .select("id,company_id,client_id,name,is_active,payroll_headcount")
        .eq("company_id", str(company_id)).eq("client_id", str(client_id)).order("id"))
    projects_by_id = {str(p["id"]): p for p in projects}
    quantities = {str(key): Decimal(value) for key, value in project_quantities.items()}
    if set(quantities) - projects_by_id.keys():
        raise HTTPException(409, "A quantity refers to a project outside this client.")
    hours = defaultdict(lambda: Decimal(0))
    for entry in entries:
        if entry.project_id is None or str(entry.project_id) not in projects_by_id:
            raise HTTPException(409, "An approved entry has no valid project for this client.")
        hours[str(entry.project_id)] += entry.hours_logged
    selected = set(hours) | {key for key, value in quantities.items() if value > 0}
    if not selected:
        raise HTTPException(404, "No approved time or project quantities were found for this period.")
    prices = await _pages(lambda: supabase.table("project_pricing")
        .select("project_id,company_id,pricing_year,indexed_fee,unit,currency,updated_at")
        .eq("company_id", str(company_id)).eq("pricing_year", period_start.year).order("project_id"))
    prices_by_id = {str(p["project_id"]): p for p in prices}
    lines = []
    for project_id in sorted(selected):
        project = projects_by_id[project_id]
        name = str(project["name"])
        price = prices_by_id.get(project_id)
        if project.get("is_active") is not True:
            raise HTTPException(409, f"Activate project '{name}' before invoicing its work.")
        if price is None or price.get("indexed_fee") is None:
            raise HTTPException(409, f"Project '{name}' has no {period_start.year} indexed price. Set it in Company settings → Projects.")
        unit = str(price["unit"])
        if unit.strip().casefold() in HOURLY_UNITS:
            if project_id in quantities:
                raise HTTPException(409, f"Hourly quantity for '{name}' comes from approved time and cannot be overridden.")
            quantity = hours[project_id] * (project.get("payroll_headcount") or 1)
        else:
            quantity = quantities.get(project_id, Decimal(0))
            if quantity <= 0:
                raise HTTPException(409, f"Enter the invoice quantity ({unit}) for project '{name}'.")
            if unit.strip().casefold() in MONTHLY_UNITS and (
                period_start.day != 1 or period_end != period_start.replace(
                    day=calendar.monthrange(period_start.year, period_start.month)[1]) or quantity != 1
            ):
                raise HTTPException(409, f"Monthly project '{name}' requires one complete calendar month and quantity 1.")
        try:
            item = InvoiceLineItem(name=name, quantity=quantity, unit=unit,
                net_unit_price=price["indexed_fee"], vat_rate=vat_code)
            net, vat, gross = calculate_line_totals(item, currency=price["currency"], tax_subject=tax_subject)
            lines.append(InvoiceReviewProjectLine(project_id=project_id, project_name=name,
                payroll_headcount=project.get("payroll_headcount"),
                logged_hours=hours[project_id] if unit.strip().casefold() in HOURLY_UNITS else None,
                pricing_year=price["pricing_year"], pricing_updated_at=price["updated_at"],
                quantity=quantity, unit=unit, net_unit_price=item.net_unit_price,
                vat_code=item.vat_rate, currency=price["currency"], net_total=net, vat_total=vat, gross_total=gross))
        except (ValidationError, ValueError, ArithmeticError) as exc:
            raise HTTPException(409, f"Project '{name}' has invalid pricing or tax settings.") from exc
    if len({line.currency for line in lines}) != 1:
        raise HTTPException(409, "The billable projects must use one currency for this invoice.")
    return lines
