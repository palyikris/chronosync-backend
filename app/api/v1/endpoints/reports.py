from fastapi import APIRouter, Depends, Header, HTTPException, Response, status

from app.schemas.reports import SzamlamellekletRequest
from app.services.excel_service import ExcelReportService
from app.services.supabase_service import SupabaseDataService

router = APIRouter()


def extract_bearer_token(
    authorization: str | None = Header(
        default=None, description="Bearer JWT token from Supabase Auth"
    ),
) -> str:
    scheme, separator, credentials = (authorization or "").partition(" ")
    token = credentials.strip()
    if separator != " " or scheme.lower() != "bearer" or not token or " " in token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format. Expected 'Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


@router.post("/generate-szamlamelleklet")
async def generate_szamlamelleklet(
    payload: SzamlamellekletRequest, token: str = Depends(extract_bearer_token)
) -> Response:
    """
    1. Extracts Supabase User JWT from Auth header.
    2. Queries Supabase using RLS-scoped user client.
    3. Collects logged hours per client as individual time entries.
    4. Generates a multi-sheet Excel file and returns it as a download.
    """

    # Fetch and aggregate data from Supabase
    client_reports = await SupabaseDataService.fetch_aggregated_timesheets(
        user_jwt=token,
        company_id=payload.company_id,
        client_ids=payload.client_ids,
        start_date=payload.start_date,
        end_date=payload.end_date,
    )

    if not client_reports:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "No approved or previously invoiced time entries were found for the "
                "selected clients and date range."
            ),
        )

    # A previously finalized month must reuse its immutable accounting snapshot,
    # including for a read-only re-download after later months changed carry-over.
    await SupabaseDataService.reconcile_finalized_periods(
        user_jwt=token,
        company_id=payload.company_id,
        start_date=payload.start_date,
        end_date=payload.end_date,
        client_reports=client_reports,
    )

    # Generate the in-memory Excel file.
    excel_service = ExcelReportService()
    excel_file = await excel_service.generate_szamlamelleklet(
        client_reports=client_reports,
        period_text=payload.period_text,
        start_date=payload.start_date,
        end_date=payload.end_date,
        user_jwt=token,
    )

    if payload.update_remaining_hours:
        await SupabaseDataService.update_remaining_hours_from_reports(
            user_jwt=token,
            company_id=payload.company_id,
            start_date=payload.start_date,
            end_date=payload.end_date,
            client_reports=client_reports,
        )

    filename = f"invoice_attachments_{payload.start_date}_{payload.end_date}.xlsx"

    return Response(
        content=excel_file.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(excel_file.getbuffer().nbytes),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )
