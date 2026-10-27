import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CompanyInvitationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    email: str = Field(min_length=3, max_length=254)
    full_name: str = Field(min_length=1, max_length=120)
    company_id: UUID
    role: Literal['regular', 'company_admin']

    @field_validator('email')
    @classmethod
    def normalize_email(cls, value: str) -> str:
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
            raise ValueError('A valid email address is required')
        return value.lower()


class CompanyInvitationResponse(BaseModel):
    id: UUID
    email: str
    status: Literal['sent'] = 'sent'
