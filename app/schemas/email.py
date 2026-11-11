from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class EmailFailure(BaseModel):
    id: UUID
    kind: str
    status: str
    last_error: str | None
    attempts: int


class EmailStatusResponse(BaseModel):
    configured: bool
    worker_enabled: bool
    invoice_transport: Literal["provider", "resend"]
    missing_env: list[str]
    queue: dict[str, int] = Field(default_factory=dict)
    recent_failures: list[EmailFailure] = Field(default_factory=list)
