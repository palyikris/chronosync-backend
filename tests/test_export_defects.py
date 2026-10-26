import asyncio
from datetime import date
import io
from unittest.mock import AsyncMock, patch

from openpyxl import load_workbook
from PIL import Image

from app.services.excel_service import ExcelReportService
from app.services.supabase_service import SupabaseDataService


def report(**overrides):
    return {
        "company_id": "company-1", "client_code": "CLIENT", "client_name": "Acme",
        "invoice_attachment_language": "en", "available_hours_per_month": 40,
        "hours_from_previous_month": 0,
        "entries": [{"task_description": "Work", "hours": 2}],
        **overrides,
    }


def generate(reports, **kwargs):
    return load_workbook(asyncio.run(ExcelReportService().generate_szamlamelleklet(
        reports, start_date=date(2026, 10, 1), end_date=date(2026, 10, 31), **kwargs,
    )))


def test_export_accepts_pasted_control_characters_without_losing_unicode_or_linebreaks():
    sheet = generate([report(
        client_code="A\x01B\ufffe", client_name="Acme\x00\ud800 Ltd",
        entries=[{"task_description": "Árvíz\x0bfollow-up\nNext\tstep 🙂", "hours": 2}],
    )]).active
    assert sheet.title == "AB"
    assert sheet["C6"].value == "Acme Ltd"
    assert sheet["C17"].value == "Árvízfollow-up\nNext\tstep 🙂"


def test_default_logo_resolves_from_the_repository_assets_directory():
    service = ExcelReportService()
    assert service._resolve_logo_path() is not None
    assert len(generate([report()]).active._images) == 1


def test_company_logo_takes_precedence_over_the_bundled_fallback():
    logo = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(logo, format="PNG")
    fetch_logo = AsyncMock(return_value=logo.getvalue())
    with patch.object(SupabaseDataService, "fetch_company_logo_bytes", fetch_logo):
        workbook = generate([report()], user_jwt="test-jwt")
    fetch_logo.assert_awaited_once_with("test-jwt", "company-1")
    exported_image = workbook.active._images[0]
    with Image.open(io.BytesIO(exported_image._data())) as actual_logo:
        assert actual_logo.getpixel((0, 0))[:3] == (255, 0, 0)
