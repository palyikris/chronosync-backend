"""Save the standard invoice attachment from the immutable invoice review."""
import asyncio
import hashlib
import io
import re
from datetime import datetime
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from fastapi import HTTPException

from app.schemas.invoices import ClientInvoiceReview, InvoiceAttachmentInfo
from app.services.excel_service import ExcelReportService
from app.services.invoice_providers.base import InvoiceEmailAttachment


class InvoiceAnnexService:
    MAX_BYTES = 2_000_000

    @staticmethod
    def _text(value: object) -> str:
        value = str(value or "")
        if len(value) > 32_766:
            raise HTTPException(409, "An invoice attachment cell exceeds Excel's text limit. Shorten the source text before invoicing.")
        return ExcelReportService._safe_cell_text(value)

    @classmethod
    async def build(cls, review: ClientInvoiceReview, *, language: str) -> InvoiceEmailAttachment:
        if language not in ExcelReportService.TEXTS:
            raise HTTPException(409, "The client's invoice attachment language is unsupported.")
        return await asyncio.to_thread(cls._build, review, language)

    @classmethod
    def metadata(cls, attachment: InvoiceEmailAttachment, *, language: str) -> InvoiceAttachmentInfo:
        return InvoiceAttachmentInfo(filename=attachment.filename, size_bytes=len(attachment.content),
            sha256=hashlib.sha256(attachment.content).hexdigest(), language=language)

    @classmethod
    def _build(cls, review: ClientInvoiceReview, language: str) -> InvoiceEmailAttachment:
        names = {item.project_id: item.invoice_name or item.project_name for item in review.items}
        entries = [{
            "task_description": cls._text("\n".join((
                f"{entry.work_date} · {names.get(entry.project_id, entry.project_name)}",
                entry.description or names.get(entry.project_id, entry.project_name)))),
            "hours": entry.hours_logged,
            "billing_multiplier": entry.billing_multiplier,
        } for entry in sorted(review.approved_entries, key=lambda item: (item.work_date, str(item.id)))]
        items = [{
            "name": cls._text(item.invoice_name or item.project_name),
            "note": cls._text(item.invoice_note),
            "po_number": cls._text(item.po_number),
            "quantity": item.quantity,
            "unit": cls._text(item.unit),
        } for item in review.items]
        for item in items:
            cls._text("\n".join((item["name"], item["note"], f"PO: {item['po_number']}")))
        # Reuse the established certificate template; no second billing workbook.
        # No JWT is passed: generation cannot fetch later data or finalize a month.
        raw = asyncio.run(ExcelReportService().generate_szamlamelleklet([{
            "company_id": str(review.company_id),
            "client_code": review.client_code,
            "client_name": cls._text(review.client_name),
            "invoice_attachment_language": language,
            "entries": entries,
            "invoice_items": items,
        }], start_date=review.period_start, end_date=review.period_end,
            issue_date_text=f"Budapest, {review.issue_date:%Y. %m. %d.}"))
        canonical = io.BytesIO()
        stamp = datetime.combine(review.issue_date, datetime.min.time()).isoformat(timespec="seconds") + "Z"
        # ZIP and workbook metadata must stay identical at review and issuance.
        with ZipFile(raw) as source, ZipFile(canonical, "w", compression=ZIP_DEFLATED, compresslevel=9) as target:
            for name in sorted(source.namelist()):
                data = source.read(name)
                if name == "docProps/core.xml":
                    for field in ("created", "modified"):
                        pattern = rb"(<dcterms:" + field.encode() + rb"[^>]*>).*?(</dcterms:" + field.encode() + rb">)"
                        data = re.sub(pattern, lambda match: match[1] + stamp.encode() + match[2], data)
                info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                target.writestr(info, data)
        content = canonical.getvalue()
        if len(content) > cls.MAX_BYTES:
            raise HTTPException(413, "The invoice attachment exceeds the provider's 2 MB attachment limit. Resolve this before issuing the invoice.")
        code = re.sub(r"[^A-Za-z0-9_-]", "", review.client_code)[:32] or "client"
        return InvoiceEmailAttachment(filename=f"invoice-attachment-{code}-{review.period_start}-{review.period_end}.xlsx", content=content)
