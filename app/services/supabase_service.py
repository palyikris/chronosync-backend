import logging
import urllib.parse
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, cast
from uuid import UUID

import httpx
import jwt
from fastapi import HTTPException, status
from postgrest.exceptions import APIError
from supabase import AsyncClient, acreate_client

from app.core.config import settings

logger = logging.getLogger(__name__)


class SupabaseDataService:
    REPORTABLE_TIMESHEET_STATUSES = (
        "approved",
        "invoiced",
    )
    PAGE_SIZE = 1000
    MAX_REPORT_ROWS = 50_000
    MAX_LOGO_BYTES = 2 * 1024 * 1024

    @staticmethod
    async def get_authenticated_client(user_jwt: str) -> AsyncClient:
        """
        Instantiates a Supabase client scoped to the authenticated user's JWT.
        Row Level Security (RLS) policies are automatically enforced.
        """
        if not settings.SUPABASE_URL or not settings.SUPABASE_ANON_KEY:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Supabase credentials not configured on backend.",
            )

        client = await acreate_client(
            settings.SUPABASE_URL, settings.SUPABASE_ANON_KEY
        )
        client.postgrest.auth(user_jwt)
        return client

    @staticmethod
    async def _close_authenticated_client(client: Any) -> None:
        postgrest = getattr(client, "postgrest", None)
        close_postgrest = getattr(postgrest, "aclose", None)
        if close_postgrest is not None:
            await close_postgrest()

        auth = getattr(client, "auth", None)
        close_auth = getattr(auth, "close", None)
        if close_auth is not None:
            await close_auth()

    @classmethod
    async def fetch_company_logo_bytes(
        cls, user_jwt: str, company_id: str
    ) -> bytes | None:
        """Download a bounded company logo using the caller's RLS-scoped token."""
        if not company_id:
            return None

        supabase_url = getattr(settings, "SUPABASE_URL", None)
        supabase_key = getattr(settings, "SUPABASE_ANON_KEY", None)
        if not supabase_url or not supabase_key:
            logger.error("Supabase credentials missing for logo download.")
            return None

        normalized_company_id = str(company_id).strip("/")
        relative_path = f"{normalized_company_id}/logo/current"
        encoded_path = urllib.parse.quote(relative_path, safe="/")
        download_url = (
            f"{supabase_url.rstrip('/')}/storage/v1/object/company-logos/{encoded_path}"
        )
        logger.debug("Attempting logo download from: %s", download_url)

        headers = {
            "apikey": supabase_key,
            "Authorization": f"Bearer {user_jwt}",
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                async with client.stream(
                    "GET", download_url, headers=headers
                ) as response:
                    response.raise_for_status()
                    content_length = response.headers.get("Content-Length")
                    if content_length:
                        try:
                            is_oversized = int(content_length) > cls.MAX_LOGO_BYTES
                        except ValueError:
                            is_oversized = False
                        if is_oversized:
                            logger.warning(
                                "Ignoring oversized company logo for company_id=%s",
                                company_id,
                            )
                            return None

                    logo_buffer = bytearray()
                    async for chunk in response.aiter_bytes():
                        logo_buffer.extend(chunk)
                        if len(logo_buffer) > cls.MAX_LOGO_BYTES:
                            logger.warning(
                                "Ignoring oversized company logo for company_id=%s",
                                company_id,
                            )
                            return None
                    file_bytes = bytes(logo_buffer)
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Supabase logo download failed: status=%d",
                exc.response.status_code,
            )
            return None
        except httpx.RequestError as exc:
            logger.error("Network error while downloading logo: %s", exc)
            return None

        if not file_bytes:
            return None

        return file_bytes

    @classmethod
    async def fetch_aggregated_timesheets(
        cls,
        user_jwt: str,
        company_id: UUID | str,
        client_ids: list[UUID | str],
        start_date: date | str,
        end_date: date | str,
    ) -> list[dict[str, Any]]:
        supabase = await cls.get_authenticated_client(user_jwt)
        try:
            await cls._assert_invoice_attachment_access_with_client(
                supabase,
                user_jwt=user_jwt,
                company_id=company_id,
            )
            return await cls._fetch_aggregated_timesheets_with_client(
                supabase,
                company_id=company_id,
                client_ids=client_ids,
                start_date=start_date,
                end_date=end_date,
            )
        finally:
            await cls._close_authenticated_client(supabase)

    @classmethod
    async def _assert_invoice_attachment_access_with_client(
        cls,
        supabase: Any,
        user_jwt: str,
        company_id: UUID | str,
    ) -> None:
        """Require an active company administrator before returning invoice data."""
        try:
            # The claims only identify which profile row to request. Authorization
            # still comes from the original JWT validated by PostgREST plus RLS.
            claims = jwt.decode(
                user_jwt,
                options={
                    "verify_signature": False,
                    "verify_exp": False,
                    "verify_aud": False,
                },
            )
            user_id = str(UUID(str(claims.get("sub") or "")))
        except (jwt.PyJWTError, TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication token.",
                headers={"WWW-Authenticate": "Bearer"},
            ) from None

        try:
            response = await (
                supabase.table("profiles")
                .select("id, company_id, role, is_active")
                .eq("id", user_id)
                .limit(1)
                .execute()
            )
        except APIError as exc:
            if exc.code in {"PGRST301", "PGRST302"}:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or expired authentication token.",
                    headers={"WWW-Authenticate": "Bearer"},
                ) from exc
            raise
        rows = response.data or []
        profile = rows[0] if isinstance(rows, list) and rows else None
        if not isinstance(profile, dict):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invoice attachments are restricted to company administrators.",
            )

        role = str(profile.get("role") or "")
        same_company = str(profile.get("company_id") or "") == str(company_id)
        if (
            profile.get("is_active") is not True
            or role not in {"company_admin", "super_admin"}
            or (role != "super_admin" and not same_company)
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invoice attachments are restricted to company administrators.",
            )

    @classmethod
    async def _fetch_aggregated_timesheets_with_client(
        cls,
        supabase: Any,
        company_id: UUID | str,
        client_ids: list[UUID | str],
        start_date: date | str,
        end_date: date | str,
    ) -> list[dict[str, Any]]:
        """
        Fetch eligible timesheet entries and return them per selected client.

        Filtering is performed by immutable IDs and company at Supabase, then every
        page is retrieved explicitly so the Data API row limit cannot truncate an
        invoice attachment silently.
        """
        normalized_company_id = str(company_id)
        requested_client_ids = {str(client_id) for client_id in client_ids}
        if not requested_client_ids:
            return []

        raw_entries: list[Any] = []
        cursor_id: str | None = None
        while True:
            query = (
                supabase.table("timesheets")
                .select(
                    "id, company_id, client_id, work_date, hours_logged, "
                    "description, projects(name), "
                    "clients!inner(id, client_code, name, company_id, is_active, "
                    "invoice_attachment_language, available_hours_per_month, "
                    "hours_from_previous_month)"
                )
                .eq("company_id", normalized_company_id)
                .in_("client_id", sorted(requested_client_ids))
                .in_("status", list(cls.REPORTABLE_TIMESHEET_STATUSES))
                .eq("clients.is_active", True)
                .gte("work_date", str(start_date))
                .lte("work_date", str(end_date))
                .order("id")
                .limit(cls.PAGE_SIZE)
            )
            if cursor_id is not None:
                query = query.gt("id", cursor_id)

            response = await query.execute()
            page = response.data or []
            if not isinstance(page, list):
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Supabase returned an invalid timesheet response.",
                )

            if len(raw_entries) + len(page) > cls.MAX_REPORT_ROWS:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=(
                        "The selected report contains more than 50,000 entries. "
                        "Choose a shorter date range or fewer clients."
                    ),
                )
            raw_entries.extend(page)
            # The project's Data API cap can be lower than our requested limit.
            # Only an empty page proves the cursor has reached the end.
            if not page:
                break

            last_row = page[-1]
            next_cursor = (
                str(last_row.get("id") or "")
                if isinstance(last_row, dict)
                else ""
            )
            if not next_cursor or next_cursor == cursor_id:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Supabase returned an invalid timesheet page cursor.",
                )
            cursor_id = next_cursor

        if not raw_entries:
            return []

        clients_map: dict[str, dict[str, Any]] = {}

        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue

            entry = cast(dict[str, Any], entry)
            project_data = cls._unwrap_relation(entry.get("projects")) or {}
            client_data = cls._unwrap_relation(entry.get("clients")) or {}
            if not isinstance(client_data, dict):
                continue

            client_id = str(entry.get("client_id") or client_data.get("id") or "")
            entry_company_id = str(
                entry.get("company_id") or client_data.get("company_id") or ""
            )

            if (
                not client_id
                or client_id not in requested_client_ids
                or entry_company_id != normalized_company_id
                or str(client_data.get("company_id") or "") != normalized_company_id
                or client_data.get("is_active") is not True
            ):
                continue

            client_code = (client_data.get("client_code") or "").strip().upper()
            if not client_code:
                continue

            raw_hours = cls._decimal_or_none(entry.get("hours_logged"))
            if raw_hours is None or raw_hours <= 0:
                logger.error(
                    "Approved timesheet has invalid hours for entry_id=%s",
                    entry.get("id"),
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "An approved or invoiced time entry contains invalid hours. "
                        "Correct the entry before generating the attachment."
                    ),
                )

            client_name = client_data.get("name") or "Unknown Client"
            invoice_attachment_language = (
                client_data.get("invoice_attachment_language") or "hu"
            )
            available_hours_per_month = cls._quantize_hours(
                cls._decimal_or_zero(client_data.get("available_hours_per_month"))
            )
            hours_from_previous_month = cls._quantize_hours(
                cls._decimal_or_zero(client_data.get("hours_from_previous_month"))
            )
            rounded_hours = cls._quantize_hours(raw_hours)

            if client_id not in clients_map:
                clients_map[client_id] = {
                    "company_id": entry_company_id,
                    "client_id": client_id,
                    "client_code": client_code,
                    "client_name": client_name,
                    "invoice_attachment_language": invoice_attachment_language,
                    "available_hours_per_month": available_hours_per_month,
                    "hours_from_previous_month": hours_from_previous_month,
                    "entries": [],
                    "used_hours": Decimal("0"),
                }

            clients_map[client_id][
                "invoice_attachment_language"
            ] = invoice_attachment_language
            clients_map[client_id][
                "available_hours_per_month"
            ] = available_hours_per_month
            clients_map[client_id][
                "hours_from_previous_month"
            ] = hours_from_previous_month
            clients_map[client_id]["company_id"] = entry_company_id

            project_name = project_data.get("name") or "General Task"
            task_description = (
                entry.get("description")
                or entry.get("task_description")
                or entry.get("task_name")
                or entry.get("task")
                or project_name
            )

            raw_work_date = entry.get("work_date")
            client_entries = clients_map[client_id]["entries"]
            client_entries.append(
                {
                    "project_name": project_name,
                    "task_description": task_description,
                    "hours": float(rounded_hours),
                    "work_date": raw_work_date,
                }
            )
            clients_map[client_id]["used_hours"] = (
                clients_map[client_id]["used_hours"] + rounded_hours
            )

        report_rows = []
        for c_info in clients_map.values():
            if not c_info["entries"]:
                continue

            entries = sorted(
                c_info["entries"],
                key=lambda item: (
                    str(item.get("work_date") or ""),
                    str(item.get("project_name") or ""),
                ),
            )
            total_logged_hours = cls._decimal_or_zero(c_info.get("used_hours"))
            available_hours = cls._decimal_or_zero(
                c_info.get("available_hours_per_month")
            )
            previous_hours = cls._decimal_or_zero(
                c_info.get("hours_from_previous_month")
            )
            difference_hours = available_hours - total_logged_hours + previous_hours
            report_rows.append(
                {
                    "company_id": c_info.get("company_id"),
                    "client_code": c_info["client_code"],
                    "client_name": c_info["client_name"],
                    "client_id": c_info.get("client_id"),
                    "invoice_attachment_language": c_info.get(
                        "invoice_attachment_language", "hu"
                    ),
                    "used_hours": cls._round_decimal(total_logged_hours),
                    "available_hours_per_month": cls._round_decimal(
                        available_hours
                    ),
                    "hours_from_previous_month": cls._round_decimal(previous_hours),
                    "difference_hours": cls._round_decimal(difference_hours),
                    "entries": entries,
                }
            )

        # Sort sheets by client code alphabetically
        report_rows.sort(key=lambda x: x["client_code"])
        return report_rows

    @classmethod
    async def reconcile_finalized_periods(
        cls,
        user_jwt: str,
        company_id: UUID | str,
        start_date: date | str,
        end_date: date | str,
        client_reports: list[dict[str, Any]],
    ) -> None:
        if not client_reports:
            return

        supabase = await cls.get_authenticated_client(user_jwt)
        try:
            await cls._reconcile_finalized_periods_with_client(
                supabase,
                company_id=company_id,
                start_date=start_date,
                end_date=end_date,
                client_reports=client_reports,
            )
        finally:
            await cls._close_authenticated_client(supabase)

    @classmethod
    async def _reconcile_finalized_periods_with_client(
        cls,
        supabase: Any,
        company_id: UUID | str,
        start_date: date | str,
        end_date: date | str,
        client_reports: list[dict[str, Any]],
    ) -> None:
        """
        Reuse an existing finalization snapshot on an idempotent retry.

        If billable hours or the monthly allowance changed after finalization, fail
        instead of returning a workbook whose carry-over disagrees with accounting.
        """
        client_ids = [str(report["client_id"]) for report in client_reports]
        query = (
            supabase.table("invoice_attachment_periods")
            .select(
                "client_id, starting_hours, available_hours, used_hours, "
                "remaining_hours"
            )
            .eq("company_id", str(company_id))
            .eq("period_start", str(start_date))
            .eq("period_end", str(end_date))
            .in_("client_id", client_ids)
        )
        response = await query.execute()
        if not isinstance(response.data, list):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Supabase returned an invalid finalization snapshot response.",
            )
        snapshots = {
            str(row["client_id"]): row
            for row in response.data
            if isinstance(row, dict) and row.get("client_id")
        }

        for report in client_reports:
            snapshot = snapshots.get(str(report.get("client_id")))
            if not snapshot:
                continue

            if not cls._same_hours(
                report.get("used_hours"), snapshot.get("used_hours")
            ) or not cls._same_hours(
                report.get("available_hours_per_month"),
                snapshot.get("available_hours"),
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "This invoice period was already finalized and its hours or "
                        "monthly allowance have since changed. Correct the source data "
                        "or use the originally archived attachment."
                    ),
                )

            report["hours_from_previous_month"] = cls._round_decimal(
                cls._decimal_or_zero(snapshot.get("starting_hours"))
            )
            report["difference_hours"] = cls._round_decimal(
                cls._decimal_or_zero(snapshot.get("remaining_hours"))
            )

    @classmethod
    async def update_remaining_hours_from_reports(
        cls,
        user_jwt: str,
        company_id: UUID | str,
        start_date: date | str,
        end_date: date | str,
        client_reports: list[dict[str, Any]],
    ) -> None:
        """Atomically and idempotently finalize carry-over for one calendar month."""
        updates = [
            {
                "client_id": str(report["client_id"]),
                "starting_hours": report["hours_from_previous_month"],
                "available_hours": report["available_hours_per_month"],
                "used_hours": report["used_hours"],
                "remaining_hours": report["difference_hours"],
            }
            for report in client_reports
            if report.get("client_id")
        ]
        if not updates:
            return

        supabase = await cls.get_authenticated_client(user_jwt)
        try:
            rpc = supabase.rpc(
                "finalize_invoice_attachment_period",
                {
                    "p_company_id": str(company_id),
                    "p_period_start": str(start_date),
                    "p_period_end": str(end_date),
                    "p_client_updates": updates,
                },
            )
            try:
                response = await rpc.execute()
            except APIError as exc:
                if exc.code == "42501":
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=(
                            "You are not authorized to finalize invoice attachments "
                            "for this company."
                        ),
                    ) from exc
                if exc.code in {"22023", "40001", "PT409"}:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            "Invoice data changed or became invalid before finalization. "
                            "Refresh the dashboard and generate the attachment again."
                        ),
                    ) from exc
                raise
            rows = response.data
            expected_client_ids = {update["client_id"] for update in updates}
            returned_client_ids = (
                {
                    str(row.get("client_id"))
                    for row in rows
                    if isinstance(row, dict) and row.get("client_id")
                }
                if isinstance(rows, list)
                else set()
            )
            if returned_client_ids != expected_client_ids:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Supabase returned an incomplete finalization response.",
                )
        finally:
            await cls._close_authenticated_client(supabase)

    @staticmethod
    def _unwrap_relation(value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            return value
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value[0]
        return None

    @staticmethod
    def _decimal_or_none(value: Any) -> Decimal | None:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
        return result if result.is_finite() else None

    @classmethod
    def _decimal_or_zero(cls, value: Any) -> Decimal:
        return cls._decimal_or_none(value) or Decimal("0")

    @staticmethod
    def _quantize_hours(value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @classmethod
    def _round_decimal(cls, value: Decimal) -> float:
        return float(cls._quantize_hours(value))

    @classmethod
    def _same_hours(cls, left: Any, right: Any) -> bool:
        return abs(cls._decimal_or_zero(left) - cls._decimal_or_zero(right)) < Decimal(
            "0.005"
        )
