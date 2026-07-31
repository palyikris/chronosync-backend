from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from app.schemas.reports import SzamlamellekletRequest
from app.services.supabase_service import SupabaseDataService
from app.services.excel_service import ExcelReportService

router = APIRouter()
excel_service = ExcelReportService()


def extract_bearer_token(
    authorization: str = Header(..., description="Bearer JWT token from Supabase Auth")
):
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format. Expected 'Bearer <token>'",
        )
    return authorization.split("Bearer ")[1]


@router.post("/generate-szamlamelleklet")
async def generate_szamlamelleklet(
    payload: SzamlamellekletRequest, token: str = Depends(extract_bearer_token)
):
    """
    1. Extracts Supabase User JWT from Auth header.
    2. Queries Supabase using RLS-scoped user client.
    3. Aggregates logged hours per client and project.
    4. Generates multi-sheet Excel file and streams it to the user.
    """
    # Fetch and aggregate data from Supabase
    client_reports = await SupabaseDataService.fetch_aggregated_timesheets(
        user_jwt=token,
        client_codes=payload.client_codes,
        start_date=payload.start_date or "",
        end_date=payload.end_date or "",
    )

    if not client_reports:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="A megadott időszakban nem található rögzített munkaóra a kiválasztott kliensekhez.",
        )

    # Generate Excel stream
    excel_file = excel_service.generate_szamlamelleklet(
        client_reports=client_reports, period_text=payload.period_text
    )

    await SupabaseDataService.update_remaining_hours_from_reports(
        user_jwt=token, client_reports=client_reports
    )

    filename = f"szamlamelleklet_{payload.period_text.replace(' ', '_')}.xlsx"

    return StreamingResponse(
        excel_file,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )
