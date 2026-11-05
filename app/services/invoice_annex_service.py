"""Build a reproducible, per-client billing annex from the invoice review only."""
import asyncio
import hashlib
import io
import math
import re
from datetime import date, datetime
from decimal import Decimal
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from fastapi import HTTPException
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.schemas.invoices import ClientInvoiceReview, InvoiceAttachmentInfo
from app.services.excel_service import ExcelReportService
from app.services.invoice_providers.base import InvoiceEmailAttachment


class InvoiceAnnexService:
    MAX_BYTES = 2_000_000
    TEXT = {
        "en": {
            "billing": "Billing breakdown", "time": "Time entries", "period": "Service period",
            "issued": "Issue date", "currency": "Currency", "recipient": "Recipient",
            "columns": ["Service", "Quantity", "Unit", "Net unit price", "VAT code", "Net", "VAT", "Gross", "Notes / PO"],
            "time_columns": ["Date", "Project", "Description", "Logged hours", "Multiplier", "Hours × multiplier"],
            "total": "Total", "empty": "No time entries. Charges are listed in the billing breakdown.",
            "note": "Non-hourly charges use the quantities shown in the billing breakdown.",
        },
        "hu": {
            "billing": "Számlázási részletező", "time": "Időkimutatás", "period": "Elszámolási időszak",
            "issued": "Kiállítás dátuma", "currency": "Pénznem", "recipient": "Vevő",
            "columns": ["Szolgáltatás", "Mennyiség", "Egység", "Nettó egységár", "Áfakulcs", "Nettó", "Áfa", "Bruttó", "Megjegyzés / PO"],
            "time_columns": ["Dátum", "Projekt", "Leírás", "Rögzített órák", "Szorzó", "Órák × szorzó"],
            "total": "Összesen", "empty": "Nincs időbejegyzés. A díjakat a számlázási részletező tartalmazza.",
            "note": "A nem óradíjas tételek mennyiségét a számlázási részletező tartalmazza.",
        },
    }

    @staticmethod
    def _text(value: object) -> str:
        value = str(value or "")
        if len(value) > 32_766:
            raise HTTPException(409, "A billing report cell exceeds Excel's text limit. Shorten the source text before invoicing.")
        return ExcelReportService._safe_cell_text(value)

    @classmethod
    async def build(cls, review: ClientInvoiceReview, *, language: str) -> InvoiceEmailAttachment:
        if language not in cls.TEXT:
            raise HTTPException(409, "The client's invoice attachment language is unsupported.")
        return await asyncio.to_thread(cls._build, review, language)

    @classmethod
    def metadata(cls, attachment: InvoiceEmailAttachment, *, language: str) -> InvoiceAttachmentInfo:
        return InvoiceAttachmentInfo(filename=attachment.filename, size_bytes=len(attachment.content),
            sha256=hashlib.sha256(attachment.content).hexdigest(), language=language)

    @classmethod
    def _build(cls, review: ClientInvoiceReview, language: str) -> InvoiceEmailAttachment:
        text = cls.TEXT[language]
        workbook = Workbook()
        billing = workbook.active
        billing.title = text["billing"]
        times = workbook.create_sheet(text["time"])
        fixed_time = datetime.combine(review.issue_date, datetime.min.time())
        workbook.properties.creator = "Chronosync"
        workbook.properties.created = fixed_time
        workbook.properties.modified = fixed_time
        money_format = '#,##0' if review.currency == 'HUF' else '#,##0.00'

        def base(sheet, title, widths):
            count = len(widths)
            sheet.sheet_view.showGridLines = False
            sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=count)
            sheet.cell(1, 1, title).font = Font(name="Calibri", size=18, bold=True, color="214332")
            sheet.row_dimensions[1].height = 32
            sheet.merge_cells(start_row=2, start_column=1, end_row=2, end_column=count)
            sheet.cell(2, 1, cls._text(f"{review.client_name} ({review.client_code})")).font = Font(name="Calibri", size=12, bold=True)
            sheet.cell(3, 1, text["period"])
            sheet.merge_cells(start_row=3, start_column=2, end_row=3, end_column=count)
            sheet.cell(3, 2, f"{review.period_start} – {review.period_end}")
            for column, width in enumerate(widths, 1):
                sheet.column_dimensions[get_column_letter(column)].width = width
            sheet.sheet_properties.pageSetUpPr.fitToPage = True
            sheet.page_setup.orientation = "landscape"
            sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
            sheet.page_setup.fitToWidth = 1
            sheet.page_setup.fitToHeight = 0
            sheet.oddFooter.center.text = "&P / &N"

        def header(sheet, row, labels):
            for column, label in enumerate(labels, 1):
                cell = sheet.cell(row, column, label)
                cell.fill = PatternFill("solid", fgColor="214332")
                cell.font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
                cell.alignment = Alignment(horizontal="left", indent=1, wrap_text=True, vertical="center")
            sheet.row_dimensions[row].height = 32
            sheet.freeze_panes = f"A{row + 1}"
            sheet.print_title_rows = f"1:{row}"

        def body(sheet, row, values):
            line_count = 1
            for column, value in enumerate(values, 1):
                cell = sheet.cell(row, column, float(value) if isinstance(value, Decimal) else value)
                cell.font = Font(name="Calibri", size=11)
                cell.alignment = Alignment(horizontal="right" if isinstance(value, (Decimal, int, float, date)) else "left",
                    indent=1, vertical="top", wrap_text=True)
                if row % 2:
                    cell.fill = PatternFill("solid", fgColor="F0F5EA")
                if isinstance(value, str):
                    width = max(1, sheet.column_dimensions[get_column_letter(column)].width - 2)
                    line_count = max(line_count, sum(max(1, math.ceil(len(part) / width)) for part in value.split("\n")))
            sheet.row_dimensions[row].height = min(409, max(32, 16 * line_count + 8))

        base(billing, text["billing"], [34, 11, 12, 17, 10, 15, 15, 15, 40])
        billing.cell(4, 1, text["recipient"])
        billing.merge_cells("B4:I4")
        billing.cell(4, 2, cls._text(review.recipient.name))
        billing.cell(5, 1, text["issued"])
        billing.cell(5, 2, review.issue_date).number_format = "yyyy-mm-dd"
        billing.cell(5, 4, text["currency"])
        billing.cell(5, 5, review.currency)
        header(billing, 8, text["columns"])
        for row, item in enumerate(review.items, 9):
            note = "\n".join(part for part in [item.invoice_note, f"PO: {item.po_number}" if item.po_number else None] if part)
            body(billing, row, [cls._text(item.invoice_name or item.project_name), item.quantity,
                cls._text(item.unit), item.net_unit_price, cls._text(item.vat_code),
                item.net_total, item.vat_total, item.gross_total, cls._text(note)])
            billing.cell(row, 2).number_format = "0.####"
            billing.cell(row, 4).number_format = "#,##0.####"
            for column in (6, 7, 8):
                billing.cell(row, column).number_format = money_format
        total_row = 9 + len(review.items)
        billing.merge_cells(start_row=total_row, start_column=1, end_row=total_row, end_column=5)
        billing.cell(total_row, 1, text["total"])
        for column, value in ((6, review.net_total), (7, review.vat_total), (8, review.gross_total)):
            billing.cell(total_row, column, float(value)).number_format = money_format
        for cell in billing[total_row]:
            cell.font = Font(name="Calibri", bold=True, size=12)
            cell.fill = PatternFill("solid", fgColor="DDE9CB")
            cell.alignment = Alignment(horizontal="right" if cell.column in (6, 7, 8) else "left", indent=1)
        billing.auto_filter.ref = f"A8:I{max(8, total_row - 1)}"

        base(times, text["time"], [16, 34, 64, 18, 14, 24])
        header(times, 5, text["time_columns"])
        names = {item.project_id: item.invoice_name or item.project_name for item in review.items}
        entries = sorted(review.approved_entries, key=lambda item: (item.work_date, str(item.id)))
        for row, entry in enumerate(entries, 6):
            body(times, row, [entry.work_date, cls._text(names.get(entry.project_id, entry.project_name)),
                cls._text(entry.description), entry.hours_logged, entry.billing_multiplier,
                entry.hours_logged * entry.billing_multiplier])
            times.cell(row, 1).number_format = "yyyy-mm-dd"
            for column in (4, 6):
                times.cell(row, column).number_format = "0.00"
        last = 6 + len(entries)
        if not entries:
            times.merge_cells(f"A{last}:F{last}")
            times.cell(last, 1, text["empty"])
        else:
            times.cell(last, 1, text["total"])
            times.cell(last, 4, float(sum((entry.hours_logged for entry in entries), Decimal(0)))).number_format = "0.00"
            times.cell(last, 6, float(sum((entry.hours_logged * entry.billing_multiplier for entry in entries), Decimal(0)))).number_format = "0.00"
            times.auto_filter.ref = f"A5:F{last - 1}"
        times.merge_cells(f"A{last + 2}:F{last + 2}")
        times.cell(last + 2, 1, text["note"]).alignment = Alignment(wrap_text=True)
        times.row_dimensions[last + 2].height = 32

        raw = io.BytesIO()
        workbook.save(raw)
        workbook.close()
        canonical = io.BytesIO()
        stamp = fixed_time.isoformat(timespec="seconds") + "Z"
        # ZIP metadata and openpyxl's save-time modification date must not alter
        # review fingerprints between review, download and issuance.
        with ZipFile(raw) as source, ZipFile(canonical, "w", compression=ZIP_DEFLATED, compresslevel=9) as target:
            for name in sorted(source.namelist()):
                data = source.read(name)
                if name == "docProps/core.xml":
                    data = re.sub(rb"(<dcterms:modified[^>]*>).*?(</dcterms:modified>)",
                        lambda match: match[1] + stamp.encode() + match[2], data)
                info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                target.writestr(info, data)
        content = canonical.getvalue()
        if len(content) > cls.MAX_BYTES:
            raise HTTPException(413, "The billing report exceeds the provider's 2 MB attachment limit. Resolve this before issuing the invoice.")
        code = re.sub(r"[^A-Za-z0-9_-]", "", review.client_code)[:32] or "client"
        return InvoiceEmailAttachment(filename=f"invoice-annex-{code}-{review.period_start}-{review.period_end}.xlsx", content=content)
