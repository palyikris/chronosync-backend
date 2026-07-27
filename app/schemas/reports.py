from pydantic import BaseModel, Field
from typing import Optional


class SzamlamellekletRequest(BaseModel):
    company_id: str = Field(..., description="UUID of the target company")
    start_date: Optional[str] = Field(
        default=None, description="ISO start date filter"
    )
    end_date: Optional[str] = Field(
        default=None, description="ISO end date filter"
    )
    period_text: str = Field(
        "2026 január", description="Human-readable month string for sheet header"
    )
