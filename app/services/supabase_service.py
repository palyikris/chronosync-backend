import logging
import tempfile
import urllib.parse
from typing import List, Dict, Any, Optional, cast

import httpx
from fastapi import HTTPException, status
from supabase import Client, create_client

from app.core.config import settings

logger = logging.getLogger(__name__)

class SupabaseDataService:
    @staticmethod
    def get_authenticated_client(user_jwt: str) -> Client:
        """
        Instantiates a Supabase client scoped to the authenticated user's JWT.
        Row Level Security (RLS) policies are automatically enforced.
        """
        if not settings.SUPABASE_URL or not settings.SUPABASE_ANON_KEY:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Supabase credentials not configured on backend.",
            )

        client = create_client(settings.SUPABASE_URL, settings.SUPABASE_ANON_KEY)
        client.postgrest.auth(user_jwt)
        return client

    @staticmethod
    def _detect_image_extension(file_bytes: bytes) -> str:
        if file_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if file_bytes.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if file_bytes.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if file_bytes.startswith(b"<svg") or b"<svg" in file_bytes[:100]:
            return ".svg"
        return ".png"

    @staticmethod
    def _write_temp_logo(file_bytes: bytes, extension: str) -> Optional[str]:
        try:
            with tempfile.NamedTemporaryFile(
                delete=False, suffix=extension
            ) as tmp_file:
                tmp_file.write(file_bytes)
                return tmp_file.name
        except IOError as exc:
            logger.error("Failed to write temporary logo file: %s", exc)
            return None

    @classmethod
    async def fetch_company_logo_path(
        cls, user_jwt: str, company_id: str
    ) -> Optional[str]:
        """Download a company logo from Supabase Storage and persist it as a temp file."""
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
                response = await client.get(download_url, headers=headers)
                response.raise_for_status()
                file_bytes = response.content
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Supabase HTTP error: status=%d, response=%s",
                exc.response.status_code,
                exc.response.text,
            )
            return None
        except httpx.RequestError as exc:
            logger.error("Network error while downloading logo: %s", exc)
            return None

        if not file_bytes:
            return None

        extension = cls._detect_image_extension(file_bytes)
        return cls._write_temp_logo(file_bytes, extension)

    @classmethod
    async def fetch_aggregated_timesheets(
        cls,
        user_jwt: str,
        client_codes: List[str],
        start_date: str,
        end_date: str,
    ) -> List[Dict[str, Any]]:
        """
        Fetches timesheet entries and aggregates hours by project for selected clients.
        """

        supabase = cls.get_authenticated_client(user_jwt)
        requested_client_codes = {
            code.strip().upper() for code in client_codes if code and code.strip()
        }
        if not requested_client_codes:
            return []

        # Build query joining timesheet_entries -> projects -> clients
        query = supabase.table("timesheets").select(
            "hours_logged, projects(name, is_active, client_id, clients(client_code, name, company_id, is_active, invoice_attachment_language, available_hours_per_month, hours_from_previous_month))"
        )

        if start_date:
            query = query.gte("work_date", start_date)
        if end_date:
            query = query.lte("work_date", end_date)

        response = query.execute()
        raw_entries = response.data

        if not raw_entries:
            return []

        # Data Aggregation Engine: Group by Client Code -> Project Name -> Sum(Hours)
        clients_map: Dict[str, Dict[str, Any]] = {}

        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue

            entry = cast(Dict[str, Any], entry)
            hours = entry.get("hours_logged") or 0.0
            project_data = entry.get("projects") or {}
            if not isinstance(project_data, dict):
                continue

            client_data = project_data.get("clients") or {}
            if not isinstance(client_data, dict):
                continue

            client_id = project_data.get("client_id")
            company_id = client_data.get("company_id")

            if client_data.get("is_active") is False:
                continue

            client_code = (client_data.get("client_code") or "").strip().upper()
            if not client_code:
                continue

            if client_code not in requested_client_codes:
                continue

            client_name = client_data.get("name") or "Unknown Client"
            invoice_attachment_language = (
                client_data.get("invoice_attachment_language") or "hu"
            )
            available_hours_per_month = float(
                client_data.get("available_hours_per_month") or 0.0
            )
            hours_from_previous_month = float(
                client_data.get("hours_from_previous_month") or 0.0
            )

            if client_code not in clients_map:
                clients_map[client_code] = {
                    "company_id": company_id,
                    "client_id": client_id,
                    "client_code": client_code,
                    "client_name": client_name,
                    "invoice_attachment_language": invoice_attachment_language,
                    "available_hours_per_month": available_hours_per_month,
                    "hours_from_previous_month": hours_from_previous_month,
                    "projects_map": {},
                }

            clients_map[client_code][
                "invoice_attachment_language"
            ] = invoice_attachment_language
            clients_map[client_code][
                "available_hours_per_month"
            ] = available_hours_per_month
            clients_map[client_code][
                "hours_from_previous_month"
            ] = hours_from_previous_month
            clients_map[client_code]["company_id"] = company_id
            clients_map[client_code]["client_id"] = client_id

            if project_data.get("is_active") is False:
                continue

            project_name = project_data.get("name") or "General Task"

            # Accumulate hours for the project
            proj_map = clients_map[client_code]["projects_map"]
            proj_map[project_name] = proj_map.get(project_name, 0.0) + float(hours)

        # Transform aggregated dict into structured list for Excel Service
        aggregated_reports = []
        for client_code, c_info in clients_map.items():
            if not c_info["projects_map"]:
                continue

            total_logged_hours = round(sum(c_info["projects_map"].values()), 2)
            difference_hours = round(
                c_info.get("available_hours_per_month", 0.0)
                - total_logged_hours
                + c_info.get("hours_from_previous_month", 0.0),
                2,
            )
            formatted_entries = [
                {"project_name": proj, "hours": round(total_hrs, 2)}
                for proj, total_hrs in c_info["projects_map"].items()
            ]

            # Sort entries alphabetically by project name
            formatted_entries.sort(key=lambda x: x["project_name"])

            aggregated_reports.append(
                {
                    "company_id": c_info.get("company_id"),
                    "client_code": c_info["client_code"],
                    "client_name": c_info["client_name"],
                    "client_id": c_info.get("client_id"),
                    "invoice_attachment_language": c_info.get(
                        "invoice_attachment_language", "hu"
                    ),
                    "used_hours": total_logged_hours,
                    "available_hours_per_month": round(
                        c_info.get("available_hours_per_month", 0.0), 2
                    ),
                    "hours_from_previous_month": round(
                        c_info.get("hours_from_previous_month", 0.0), 2
                    ),
                    "difference_hours": difference_hours,
                    "entries": formatted_entries,
                }
            )

        # Sort sheets by client code alphabetically
        aggregated_reports.sort(key=lambda x: x["client_code"])
        return aggregated_reports

    @classmethod
    async def update_remaining_hours_from_reports(
        cls, user_jwt: str, client_reports: List[Dict[str, Any]]
    ) -> None:
        """
        Updates each client's hours_from_previous_month to the newly calculated difference.
        """
        supabase = cls.get_authenticated_client(user_jwt)

        for report in client_reports:
            client_id = report.get("client_id")
            if not client_id:
                continue

            difference_hours = float(report.get("difference_hours") or 0.0)
            supabase.table("clients").update(
                {"hours_from_previous_month": difference_hours}
            ).eq("id", client_id).execute()
