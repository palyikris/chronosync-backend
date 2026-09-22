from uuid import UUID

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr, field_validator


InvoiceProviderName = Literal["szamlazz_hu", "billingo"]


class InvoiceProviderConfigRequest(BaseModel):
    company_id: UUID = Field(..., description="Company receiving the credential")
    invoice_provider: InvoiceProviderName = Field(
        default="szamlazz_hu", description="Selected invoice provider"
    )
    api_key: SecretStr | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        description="Provider API key; write-only and never returned",
    )

    @field_validator("api_key")
    @classmethod
    def reject_blank_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return value
        if not value.get_secret_value().strip():
            raise ValueError("api_key must not be blank")
        return value


class InvoiceProviderConfigResponse(BaseModel):
    invoice_provider: InvoiceProviderName
    api_key_configured: bool


class SzamlazzKeyRequest(BaseModel):
    company_id: UUID = Field(..., description="Company receiving the credential")
    agent_key: SecretStr = Field(
        ...,
        min_length=1,
        max_length=512,
        description="Számla Agent key; write-only and never returned",
    )

    @field_validator("agent_key")
    @classmethod
    def reject_blank_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("agent_key must not be blank")
        return value
