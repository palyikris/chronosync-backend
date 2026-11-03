from typing import Any, Literal
from uuid import UUID

from fastapi import HTTPException, status

ModuleId = Literal["leave_attachments", "invoicing"]
MODULE_DEPENDENCIES: dict[ModuleId, tuple[ModuleId, ...]] = {
    "leave_attachments": (),
    "invoicing": ("leave_attachments",),
}


async def require_company_module(
    supabase: Any, company_id: UUID | str, module: ModuleId
) -> None:
    """Check the authorized caller's company through the same RLS-scoped client."""
    response = await (
        supabase.table("companies")
        .select("enabled_modules, is_active, is_deleted")
        .eq("id", str(company_id))
        .limit(1)
        .execute()
    )
    data = response.data
    company = data[0] if isinstance(data, list) and data else data
    if not isinstance(company, dict):
        company = {}
    enabled = company.get("enabled_modules") or []
    if (
        company.get("is_active") is not True
        or company.get("is_deleted") is not False
        or module not in enabled
        or any(required not in enabled for required in MODULE_DEPENDENCIES[module])
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"The {module} module is not enabled for this company.",
        )
