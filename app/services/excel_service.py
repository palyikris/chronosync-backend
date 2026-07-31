import io
import logging
import os
from typing import List, Dict, Any, Optional
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.drawing.image import Image
from PIL import Image as PILImage

from app.services.supabase_service import SupabaseDataService

logger = logging.getLogger(__name__)


class ExcelReportService:
    TEXTS = {
        "hu": {
            "title": "Számlamelléklet (teljesítési igazolás)",
            "statement": "Szerződésünk 4 pontja szerint csatoljuk az adott elszámolási időszakban igénybe vett tanácsadási szolgáltatásokról szóló kimutatást.",
            "used_hours_sum": "Felhasznált órák összege",
            "available_hours": "Havi rendelkezésre álló óraszám",
            "previous_hours": "Előző havi órakeret",
            "difference": "Különbözet",
            "task": "Feladat",
            "hours": "Időráfordítás (óra)",
        },
        "en": {
            "title": "Invoice attachment (certificate of performance)",
            "statement": "In accordance with clause 4 of our contract, we attach the statement of consulting services used during the given billing period.",
            "used_hours_sum": "Total used hours",
            "available_hours": "Available hours per month",
            "previous_hours": "Hours from previous month",
            "difference": "Difference",
            "task": "Task",
            "hours": "Time spent (hours)",
        },
    }

    def __init__(self, logo_path: Optional[str] = None):
        """
        Initialize the service with an optional path to the Ecovis logo.
        """
        self.logo_path: Optional[str] = None
        self.set_logo_path(logo_path)

    def set_logo_path(self, logo_path: Optional[str] = None) -> "ExcelReportService":
        """Store a logo path for the next workbook generation."""
        if logo_path is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            logo_path = os.path.join(base_dir, "assets", "ecovis_logo.png")

        self.logo_path = logo_path
        return self

    def _resolve_logo_path(self, logo_path: Optional[str] = None) -> Optional[str]:
        """Resolve the logo path lazily so late updates are picked up."""
        if logo_path is not None:
            return logo_path
        if self.logo_path:
            return self.logo_path

        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base_dir, "assets", "ecovis_logo.png")

    @staticmethod
    def _add_logo_to_sheet(ws, logo_path: Optional[str], row: int) -> None:
        if not logo_path or not os.path.exists(logo_path):
            logger.warning("No logo path available for sheet %s", ws.title)
            return

        try:
            with PILImage.open(logo_path) as pil_img:
                logger.info(
                    "Loaded logo image for %s: format=%s, size=%s",
                    ws.title,
                    pil_img.format,
                    pil_img.size,
                )
                pil_img = pil_img.convert("RGBA")
                image_bytes = io.BytesIO()
                pil_img.save(image_bytes, format="PNG")
                image_bytes.seek(0)
                img = Image(image_bytes)
                img.width = 220
                img.height = 48
                ws.add_image(img, f"A{row}")
                ws.row_dimensions[row].height = 48
                logger.info("Successfully added logo image to %s", ws.title)
        except Exception as exc:
            logger.exception(
                "Failed to add logo image to sheet %s from %s: %s",
                ws.title,
                logo_path,
                exc,
            )

    async def generate_szamlamelleklet(
        self,
        client_reports: List[Dict[str, Any]],
        period_text: str = "2026 január",
        user_jwt: Optional[str] = None,
    ) -> io.BytesIO:
        """
        Generates a multi-sheet Excel workbook where each client has a dedicated sheet.
        """

        wb = Workbook()
        wb.remove(wb.active)  # type: ignore

        font_client_name = Font(name="Calibri", size=13, bold=True)
        font_doc_title = Font(name="Calibri", size=14, bold=True)
        font_period = Font(name="Calibri", size=12, bold=False)
        font_body = Font(name="Calibri", size=11, bold=False)
        font_header = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        font_summary_label = Font(name="Calibri", size=11, bold=True)
        font_summary_value = Font(name="Calibri", size=11, bold=True)

        fill_header = PatternFill(
            start_color="4F81BD", end_color="4F81BD", fill_type="solid"
        )
        fill_intro = PatternFill(
            start_color="EAF3FB", end_color="EAF3FB", fill_type="solid"
        )
        fill_summary = PatternFill(
            start_color="F7FAFC", end_color="F7FAFC", fill_type="solid"
        )
        fill_alt_row = PatternFill(
            start_color="FAFCFE", end_color="FAFCFE", fill_type="solid"
        )

        align_left = Alignment(horizontal="left", vertical="center", wrap_text=True)
        align_right = Alignment(horizontal="right", vertical="center")
        align_top_left = Alignment(horizontal="left", vertical="top", wrap_text=True)

        thin_border_side = Side(border_style="thin", color="D9D9D9")
        thin_border = Border(
            left=thin_border_side,
            right=thin_border_side,
            top=thin_border_side,
            bottom=thin_border_side,
        )
        medium_border_side = Side(border_style="medium", color="4F81BD")
        medium_border = Border(
            left=medium_border_side,
            right=medium_border_side,
            top=medium_border_side,
            bottom=medium_border_side,
        )

        for report in client_reports:
            client_code = report.get("client_code", "CLIENT")
            safe_title = client_code.translate(str.maketrans("", "", r"\\/*?:[]"))[:30]
            client_name = report.get("client_name", "")
            entries = report.get("entries", [])
            logo_path = report.get("logo_path")

            logo_source_id = report.get("company_id")
            if not logo_source_id:
                logger.warning(
                    "No company_id found for sheet %s; cannot fetch logo from Supabase",
                    safe_title,
                )
            if not logo_path and user_jwt and logo_source_id:
                logger.info(
                    "Attempting to fetch logo for sheet %s using company_id=%s",
                    safe_title,
                    logo_source_id,
                )
                logo_path = await SupabaseDataService.fetch_company_logo_path(
                    user_jwt, str(logo_source_id)
                )
                if logo_path:
                    report["logo_path"] = logo_path
                    logger.info(
                        "Stored fetched logo path for sheet %s: %s",
                        safe_title,
                        logo_path,
                    )
                else:
                    logger.warning(
                        "No logo file returned for sheet %s (id=%s)",
                        safe_title,
                        logo_source_id,
                    )

            available_hours_per_month = float(
                report.get("available_hours_per_month", 0.0) or 0.0
            )
            hours_from_previous_month = float(
                report.get("hours_from_previous_month", 0.0) or 0.0
            )
            language = str(report.get("invoice_attachment_language", "hu")).lower()
            text = self.TEXTS.get(language, self.TEXTS["hu"])

            resolved_logo_path = self._resolve_logo_path(logo_path)

            ws = wb.create_sheet(title=safe_title)
            ws.views.sheetView[0].showGridLines = True

            for row_idx in range(1, 5):
                ws.row_dimensions[row_idx].height = 22

            intro_cells = [
                ws.cell(row=1, column=1, value=client_name),
                ws.cell(row=2, column=1, value=text["title"]),
                ws.cell(row=3, column=1, value=period_text),
                ws.cell(row=4, column=1, value=text["statement"]),
            ]
            for cell in intro_cells:
                cell.alignment = align_top_left
                cell.border = thin_border
                cell.fill = fill_intro

            intro_cells[0].font = font_client_name
            intro_cells[1].font = font_doc_title
            intro_cells[2].font = font_period
            intro_cells[3].font = font_body

            hdr_a = ws.cell(row=6, column=1, value=text["task"])
            hdr_b = ws.cell(row=6, column=2, value=text["hours"])
            for hdr, align in [(hdr_a, align_left), (hdr_b, align_right)]:
                hdr.font = font_header
                hdr.fill = fill_header
                hdr.alignment = align
                hdr.border = thin_border

            ws.row_dimensions[6].height = 24

            current_row = 7
            max_col1_len = len(text["task"])
            first_entry_row = current_row

            for idx, entry in enumerate(entries, start=1):
                project_name = entry.get("project_name", "")
                hours = entry.get("hours", 0.0)

                cell_a = ws.cell(row=current_row, column=1, value=project_name)
                cell_b = ws.cell(row=current_row, column=2, value=hours)

                cell_a.font = font_body
                cell_a.alignment = align_left
                cell_a.border = thin_border
                cell_b.font = font_body
                cell_b.alignment = align_right
                cell_b.number_format = "0.0"
                cell_b.border = thin_border

                if idx % 2 == 0:
                    cell_a.fill = fill_alt_row
                    cell_b.fill = fill_alt_row

                if len(str(project_name)) > max_col1_len:
                    max_col1_len = len(str(project_name))

                ws.row_dimensions[current_row].height = 20
                current_row += 1

            summary_header_row = current_row
            summary_start_row = summary_header_row + 1
            used_hours_row = summary_start_row
            available_hours_row = summary_start_row + 1
            previous_hours_row = summary_start_row + 2
            difference_row = summary_start_row + 3

            summary_header = ws.cell(row=summary_header_row, column=1, value="Summary")
            summary_header.font = font_summary_label
            summary_header.fill = fill_summary
            summary_header.alignment = align_left
            summary_header.border = thin_border
            ws.cell(row=summary_header_row, column=2, value="").fill = fill_summary

            used_hours_label = ws.cell(
                row=used_hours_row, column=1, value=text["used_hours_sum"]
            )
            used_hours_value = ws.cell(
                row=used_hours_row,
                column=2,
                value=(
                    f"=SUM(B{first_entry_row}:B{current_row - 1})"
                    if current_row > first_entry_row
                    else 0
                ),
            )

            available_hours_label = ws.cell(
                row=available_hours_row, column=1, value=text["available_hours"]
            )
            available_hours_value = ws.cell(
                row=available_hours_row, column=2, value=available_hours_per_month
            )

            previous_hours_label = ws.cell(
                row=previous_hours_row, column=1, value=text["previous_hours"]
            )
            previous_hours_value = ws.cell(
                row=previous_hours_row, column=2, value=hours_from_previous_month
            )

            difference_label = ws.cell(
                row=difference_row, column=1, value=text["difference"]
            )
            difference_value = ws.cell(
                row=difference_row,
                column=2,
                value=f"=B{available_hours_row}-B{used_hours_row}+B{previous_hours_row}",
            )

            for cell in [
                used_hours_label,
                available_hours_label,
                previous_hours_label,
                difference_label,
            ]:
                cell.font = font_summary_label
                cell.alignment = align_left
                cell.fill = fill_summary
                cell.border = thin_border

            for cell in [
                used_hours_value,
                available_hours_value,
                previous_hours_value,
                difference_value,
            ]:
                cell.font = font_summary_value
                cell.alignment = align_right
                cell.number_format = "0.0"
                cell.fill = fill_summary
                cell.border = thin_border

            ws.column_dimensions["A"].width = max(
                max_col1_len + 5,
                len(text["used_hours_sum"]) + 5,
                len(text["available_hours"]) + 5,
                len(text["previous_hours"]) + 5,
                len(text["difference"]) + 5,
                45,
            )
            ws.column_dimensions["B"].width = 18

            for row_idx in range(1, difference_row + 1):
                ws.cell(row=row_idx, column=1).alignment = align_left
                ws.cell(row=row_idx, column=2).alignment = align_right

            for row_idx in range(1, difference_row + 1):
                if row_idx in {1, 2, 3, 4, 6, summary_header_row}:
                    ws.cell(row=row_idx, column=1).border = thin_border
                    ws.cell(row=row_idx, column=2).border = thin_border

            for border_row in range(6, difference_row + 1):
                ws.cell(row=border_row, column=1).border = thin_border
                ws.cell(row=border_row, column=2).border = thin_border

            ws.cell(row=summary_header_row, column=1).border = medium_border
            ws.cell(row=summary_header_row, column=2).border = medium_border

            if resolved_logo_path:
                logger.info(
                    "Attempting to add logo to sheet %s from %s",
                    safe_title,
                    resolved_logo_path,
                )
                self._add_logo_to_sheet(ws, resolved_logo_path, difference_row + 2)

        file_stream = io.BytesIO()
        wb.save(file_stream)
        file_stream.seek(0)
        return file_stream
