import asyncio
from unittest.mock import AsyncMock, patch

from app.services.supabase_service import SupabaseDataService


COMPANY_ID = "10000000-0000-0000-0000-000000000001"
CLIENT_ID = "20000000-0000-0000-0000-000000000001"


class FakeResponse:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.requested_limit = len(rows)
        self.cursor_id = None

    def __getattr__(self, method_name):
        def record(*args, **kwargs):
            self.calls.append((method_name, args, kwargs))
            if method_name == "limit":
                self.requested_limit = args[0]
            if method_name == "gt" and args[0] == "id":
                self.cursor_id = str(args[1])
            return self

        return record

    async def execute(self):
        rows = self.rows
        if self.cursor_id is not None:
            rows = [row for row in rows if str(row["id"]) > self.cursor_id]
        return FakeResponse(rows[: self.requested_limit])


class FakeClient:
    def __init__(self, query):
        self.query = query

    def table(self, table_name):
        assert table_name == "timesheets"
        return self.query


def make_entry(entry_id, description, hours, project_active=True):
    return {
        "id": entry_id,
        "company_id": COMPANY_ID,
        "client_id": CLIENT_ID,
        "work_date": "2026-10-01",
        "hours_logged": hours,
        "description": description,
        "projects": {
            "name": "Alpha Project",
            # Historical entries must remain billable after a project is archived.
            "is_active": project_active,
        },
        "clients": {
            "id": CLIENT_ID,
            "client_code": "ABC",
            "name": "Acme Ltd",
            "company_id": COMPANY_ID,
            "is_active": True,
            "invoice_attachment_language": "en",
            "available_hours_per_month": "40.00",
            "hours_from_previous_month": "2.00",
        },
    }


def test_fetch_aggregated_timesheets_is_scoped_filtered_and_paginated():
    fake_query = FakeQuery(
        [
            make_entry("entry-1", "Data migration review", "2.50"),
            make_entry("entry-2", "Archived project follow-up", 1, False),
        ]
    )

    with (
        patch.object(
            SupabaseDataService,
            "get_authenticated_client",
            return_value=FakeClient(fake_query),
        ),
        patch.object(
            SupabaseDataService,
            "_assert_invoice_attachment_access_with_client",
            AsyncMock(return_value=None),
        ),
        patch.object(SupabaseDataService, "PAGE_SIZE", 1),
    ):
        result = asyncio.run(
            SupabaseDataService.fetch_aggregated_timesheets(
                "fake-jwt",
                COMPANY_ID,
                [CLIENT_ID],
                "2026-10-01",
                "2026-10-31",
            )
        )

    assert [entry["task_description"] for entry in result[0]["entries"]] == [
        "Data migration review",
        "Archived project follow-up",
    ]
    assert result[0]["used_hours"] == 3.5
    assert result[0]["difference_hours"] == 38.5

    assert ("eq", ("company_id", COMPANY_ID), {}) in fake_query.calls
    assert ("in_", ("client_id", [CLIENT_ID]), {}) in fake_query.calls
    status_filter = next(
        call for call in fake_query.calls if call[0] == "in_" and call[1][0] == "status"
    )
    assert status_filter[1][1] == ["approved", "invoiced"]
    assert [call[1] for call in fake_query.calls if call[0] == "limit"] == [
        (1,),
        (1,),
        (1,),
    ]
    assert [call[1] for call in fake_query.calls if call[0] == "gt"] == [
        ("id", "entry-1"),
        ("id", "entry-2"),
    ]


def test_entry_level_rounding_matches_the_workbook_sum():
    fake_query = FakeQuery(
        [
            make_entry("entry-1", "First", "0.005"),
            make_entry("entry-2", "Second", "0.005"),
        ]
    )

    with (
        patch.object(
            SupabaseDataService,
            "get_authenticated_client",
            return_value=FakeClient(fake_query),
        ),
        patch.object(
            SupabaseDataService,
            "_assert_invoice_attachment_access_with_client",
            AsyncMock(return_value=None),
        ),
    ):
        result = asyncio.run(
            SupabaseDataService.fetch_aggregated_timesheets(
                "fake-jwt",
                COMPANY_ID,
                [CLIENT_ID],
                "2026-10-01",
                "2026-10-31",
            )
        )

    assert [entry["hours"] for entry in result[0]["entries"]] == [0.01, 0.01]
    assert result[0]["used_hours"] == 0.02
