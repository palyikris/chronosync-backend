from typing import List, Dict, Any, cast
from supabase import create_client, Client
from fastapi import HTTPException, status
from app.core.config import settings


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

    @classmethod
    async def fetch_aggregated_timesheets(
        cls,
        user_jwt: str,
        company_id: str,
        start_date: str,
        end_date: str,
    ) -> List[Dict[str, Any]]:
        """
        Fetches timesheet entries for a company and aggregates hours by project for each client.
        """
        supabase = cls.get_authenticated_client(user_jwt)

        # Build query joining timesheet_entries -> projects -> clients
        query = (
            supabase.table("timesheets")
            .select(
                "hours_logged, projects(name, is_active, client_id, clients(name, is_active, invoice_attachment_language))"
            )
            .eq("company_id", company_id)
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
            client_data = project_data.get("clients") or {}

            if project_data.get("is_active") is False:
                continue

            if client_data.get("is_active") is False:
                continue

            client_code = (client_data.get("name") or "")[0:3].upper() if client_data.get("name") else "UNK"
            client_name = client_data.get("name") or "Unknown Client"
            project_name = project_data.get("name") or "General Task"
            invoice_attachment_language = (
                client_data.get("invoice_attachment_language") or "hu"
            )

            if client_code not in clients_map:
                clients_map[client_code] = {
                    "client_code": client_code,
                    "client_name": client_name,
                    "invoice_attachment_language": invoice_attachment_language,
                    "projects_map": {},
                }

            clients_map[client_code][
                "invoice_attachment_language"
            ] = invoice_attachment_language

            # Accumulate hours for the project
            proj_map = clients_map[client_code]["projects_map"]
            proj_map[project_name] = proj_map.get(project_name, 0.0) + float(hours)

        # Transform aggregated dict into structured list for Excel Service
        aggregated_reports = []
        for client_code, c_info in clients_map.items():
            formatted_entries = [
                {"project_name": proj, "hours": round(total_hrs, 2)}
                for proj, total_hrs in c_info["projects_map"].items()
            ]

            # Sort entries alphabetically by project name
            formatted_entries.sort(key=lambda x: x["project_name"])

            aggregated_reports.append(
                {
                    "client_code": c_info["client_code"],
                    "client_name": c_info["client_name"],
                    "invoice_attachment_language": c_info.get(
                        "invoice_attachment_language", "hu"
                    ),
                    "entries": formatted_entries,
                }
            )

        # Sort sheets by client code alphabetically
        aggregated_reports.sort(key=lambda x: x["client_code"])
        return aggregated_reports
