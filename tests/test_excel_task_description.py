import asyncio
from unittest.mock import MagicMock, patch

from app.services.supabase_service import SupabaseDataService


class FakeResponse:
    def __init__(self, data):
        self.data = data


def test_fetch_aggregated_timesheets_keeps_task_description():
    fake_client = MagicMock()
    fake_table = MagicMock()
    fake_query = MagicMock()

    fake_query.gte.return_value = fake_query
    fake_query.lte.return_value = fake_query
    fake_query.execute.return_value = FakeResponse(
        [
            {
                "work_date": "2026-10-01",
                "hours_logged": 2.5,
                "description": "Data migration review",
                "projects": {
                    "name": "Alpha Project",
                    "is_active": True,
                    "client_id": "client-1",
                    "clients": {
                        "client_code": "ABC",
                        "name": "Acme Ltd",
                        "company_id": "company-1",
                        "is_active": True,
                        "invoice_attachment_language": "en",
                        "available_hours_per_month": 40,
                        "hours_from_previous_month": 2,
                    },
                },
            }
        ]
    )
    fake_table.select.return_value = fake_query
    fake_client.table.return_value = fake_table

    with patch.object(
        SupabaseDataService,
        "get_authenticated_client",
        return_value=fake_client,
    ):
        result = asyncio.run(
            SupabaseDataService.fetch_aggregated_timesheets(
                "fake-jwt", ["ABC"], "2026-09-01", "2026-09-30"
            )
        )

    assert result[0]["entries"][0]["project_name"] == "Alpha Project"
    assert result[0]["entries"][0]["task_description"] == "Data migration review"
