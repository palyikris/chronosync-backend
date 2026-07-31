from pydantic import BaseModel, Field
from typing import List, Optional

class SzamlamellekletRequest(BaseModel):
    client_codes: List[str] = Field(
        ..., min_length=1, description="List of target client codes (e.g. COS, ABC)"
    )
    start_date: Optional[str] = Field(
        default=None, description="ISO start date filter"
    )
    end_date: Optional[str] = Field(
        default=None, description="ISO end date filter"
    )
    period_text: str = Field(
        "2026 január", description="Human-readable month string for sheet header"
    )
