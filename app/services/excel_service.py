import io
import os
from typing import List, Dict, Any, Optional
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.drawing.image import Image
from openpyxl.utils import get_column_letter


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
        if logo_path is None:
            # Default fallback path relative to the app structure
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            logo_path = os.path.join(base_dir, "assets", "ecovis_logo.png")

        self.logo_path = logo_path

    def generate_szamlamelleklet(
        self, client_reports: List[Dict[str, Any]], period_text: str = "2026 január"
    ) -> io.BytesIO:
        """
        Generates a multi-sheet Excel workbook where each client has a dedicated sheet.

        Expected structure for client_reports:
        [
            {
                "client_code": "COS",
                "client_name": 'Cosentino Hungary Kft. "v.a."',
                "entries": [
                    {"project_name": "Intrastat, MNB", "hours": 0.7},
                    {"project_name": "Cégkapu regisztráció", "hours": 1.3},
                    {"project_name": "Bérszámfejtés", "hours": 5.4}
                ]
            },
            ...
        ]
        """
        wb = Workbook()
        # Remove the default sheet created on workbook instantiation
        wb.remove(wb.active) # pyright: ignore[reportArgumentType]

        # Style Definitions
        font_client_name = Font(name="Calibri", size=13, bold=True)
        font_doc_title = Font(name="Calibri", size=14, bold=True)
        font_period = Font(name="Calibri", size=12, bold=False)
        font_body = Font(name="Calibri", size=11, bold=False)
        font_header = Font(name="Calibri", size=11, bold=True, color="FFFFFF")

        fill_header = PatternFill(
            start_color="4F81BD", end_color="4F81BD", fill_type="solid"
        )

        align_left = Alignment(horizontal="left", vertical="center")
        align_right = Alignment(horizontal="right", vertical="center")

        thin_border_side = Side(border_style="thin", color="D9D9D9")
        thin_border = Border(
            left=thin_border_side,
            right=thin_border_side,
            top=thin_border_side,
            bottom=thin_border_side,
        )

        for report in client_reports:
            client_code = report.get("client_code", "CLIENT")
            client_name = report.get("client_name", "")
            entries = report.get("entries", [])
            available_hours_per_month = float(
                report.get("available_hours_per_month", 0.0) or 0.0
            )
            hours_from_previous_month = float(
                report.get("hours_from_previous_month", 0.0) or 0.0
            )
            language = str(report.get("invoice_attachment_language", "hu")).lower()
            text = self.TEXTS.get(language, self.TEXTS["hu"])

            # Excel sheet tabs allow max 31 chars and prohibit special characters: \ / ? * : [ ]
            safe_title = client_code.translate(str.maketrans("", "", r"\/*?:[]"))[:30]
            ws = wb.create_sheet(title=safe_title)

            # Ensure grid lines remain visible
            ws.views.sheetView[0].showGridLines = True

            # Row 1: Client Full Name
            c1 = ws.cell(row=1, column=1, value=client_name)
            c1.font = font_client_name

            # Row 2: Document Title
            c2 = ws.cell(row=2, column=1, value=text["title"])
            c2.font = font_doc_title

            # Row 3: Period
            c3 = ws.cell(row=3, column=1, value=period_text)
            c3.font = font_period

            # Row 4: Boilerplate Statement
            c4 = ws.cell(
                row=4,
                column=1,
                value=text["statement"],
            )
            c4.font = font_body

            # Row 5: Client hour summary
            summary_label_a = ws.cell(row=5, column=1, value=text["available_hours"])
            summary_value_a = ws.cell(row=5, column=2, value=available_hours_per_month)
            summary_label_b = ws.cell(row=5, column=3, value=text["previous_hours"])
            summary_value_b = ws.cell(row=5, column=4, value=hours_from_previous_month)

            for cell in [summary_label_a, summary_label_b]:
                cell.font = font_body
                cell.alignment = align_left

            for cell in [summary_value_a, summary_value_b]:
                cell.font = font_body
                cell.alignment = align_right
                cell.number_format = "0.0"

            # Row 6: Table Header
            hdr_a = ws.cell(row=6, column=1, value=text["task"])
            hdr_b = ws.cell(row=6, column=2, value=text["hours"])

            for hdr, align in [(hdr_a, align_left), (hdr_b, align_right)]:
                hdr.font = font_header
                hdr.fill = fill_header
                hdr.alignment = align

            # Rows 7+: Data Entries
            current_row = 7
            max_col1_len = len(text["task"])
            first_entry_row = current_row

            for entry in entries:
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

                if len(str(project_name)) > max_col1_len:
                    max_col1_len = len(str(project_name))

                current_row += 1

            summary_start_row = current_row
            used_hours_row = summary_start_row
            available_hours_row = summary_start_row + 1
            previous_hours_row = summary_start_row + 2
            difference_row = summary_start_row + 3

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
                row=available_hours_row,
                column=2,
                value=available_hours_per_month,
            )

            previous_hours_label = ws.cell(
                row=previous_hours_row, column=1, value=text["previous_hours"]
            )
            previous_hours_value = ws.cell(
                row=previous_hours_row,
                column=2,
                value=hours_from_previous_month,
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
                cell.font = font_body
                cell.alignment = align_left

            for cell in [
                used_hours_value,
                available_hours_value,
                previous_hours_value,
                difference_value,
            ]:
                cell.font = font_body
                cell.alignment = align_right
                cell.number_format = "0.0"

            # Auto-adjust column widths based on content with padding
            ws.column_dimensions["A"].width = max(
                max_col1_len + 5,
                len(text["used_hours_sum"]) + 5,
                len(text["available_hours"]) + 5,
                len(text["previous_hours"]) + 5,
                len(text["difference"]) + 5,
                45,
            )
            ws.column_dimensions["B"].width = 22

            # Embed Logo at the bottom
            if os.path.exists(self.logo_path):
                img = Image(self.logo_path)
                # Standard width/height for logo display
                img.width = 220
                img.height = 48
                # Place logo 2 rows below the last table row
                logo_cell = f"A{current_row + 2}"
                ws.add_image(img, logo_cell)

        file_stream = io.BytesIO()
        wb.save(file_stream)
        file_stream.seek(0)
        return file_stream
