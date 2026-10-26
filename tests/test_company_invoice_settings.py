import asyncio
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from app.api.v1.endpoints.company import set_invoice_settings
from app.core.config import settings
from app.schemas.company import InvoiceProviderConfigRequest
from app.services.company_service import CompanyCredentialService
from app.services.supabase_service import SupabaseDataService


COMPANY_ID = "10000000-0000-0000-0000-000000000001"
USER_ID = "30000000-0000-0000-0000-000000000001"
TOKEN = jwt.encode(
    {"sub": USER_ID},
    "test-key-with-at-least-thirty-two-bytes",
)


class FakeResponse:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, data, on_execute=None):
        self.data = data
        self.on_execute = on_execute

    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: self

    async def execute(self):
        if self.on_execute:
            self.on_execute()
        return FakeResponse(self.data)


class InvoiceSettingsClient:
    def __init__(self, *, provider="szamlazz_hu", encrypted_key="stored"):
        self.provider = provider
        self.encrypted_key = encrypted_key
        self.rpc_params = None

    def table(self, table_name):
        if table_name == "profiles":
            return FakeQuery(
                [
                    {
                        "id": USER_ID,
                        "company_id": COMPANY_ID,
                        "role": "company_admin",
                        "is_active": True,
                    }
                ]
            )
        if table_name == "companies":
            return FakeQuery(
                {
                    "invoice_provider": self.provider,
                    "invoice_api_key_encrypted": self.encrypted_key,
                }
            )
        raise AssertionError(f"Unexpected table: {table_name}")

    def rpc(self, function_name, params):
        assert function_name == "set_company_invoice_credentials"
        self.rpc_params = params

        def save_settings():
            self.provider = params["p_invoice_provider"]
            if params["p_encrypted_key"] is not None:
                self.encrypted_key = params["p_encrypted_key"]

        return FakeQuery(None, on_execute=save_settings)


def test_invoice_settings_save_encrypts_lowercase_key_and_returns_configuration():
    encryption_key = Fernet.generate_key()
    client = InvoiceSettingsClient(encrypted_key=None)
    close_client = AsyncMock()

    with (
        patch.object(settings, "ENCRYPTION_MASTER_KEY", encryption_key.decode()),
        patch.object(
            SupabaseDataService,
            "get_authenticated_client",
            AsyncMock(return_value=client),
        ) as get_client,
        patch.object(
            SupabaseDataService,
            "_close_authenticated_client",
            close_client,
        ),
    ):
        result = asyncio.run(
            CompanyCredentialService.set_invoice_settings(
                TOKEN,
                COMPANY_ID,
                "szamlazz_hu",
                "  ABCdef123  ",
            )
        )

    get_client.assert_awaited_once_with(TOKEN)
    close_client.assert_awaited_once_with(client)
    assert result == {
        "invoice_provider": "szamlazz_hu",
        "api_key_configured": True,
    }
    assert client.rpc_params["p_company_id"] == COMPANY_ID
    encrypted = client.rpc_params["p_encrypted_key"]
    assert Fernet(encryption_key).decrypt(encrypted.encode()) == b"abcdef123"


def test_invoice_settings_requires_a_new_key_when_switching_provider():
    client = InvoiceSettingsClient(provider="szamlazz_hu")

    with (
        patch.object(
            SupabaseDataService,
            "get_authenticated_client",
            AsyncMock(return_value=client),
        ),
        patch.object(
            SupabaseDataService,
            "_close_authenticated_client",
            AsyncMock(),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        asyncio.run(
            CompanyCredentialService.set_invoice_settings(
                TOKEN,
                COMPANY_ID,
                "billingo",
                None,
            )
        )

    assert exc_info.value.status_code == 422
    assert client.rpc_params is None


def test_invoice_settings_endpoint_returns_json_expected_by_frontend():
    saved = {
        "invoice_provider": "szamlazz_hu",
        "api_key_configured": True,
    }
    save_settings = AsyncMock(return_value=saved)

    with patch.object(
        CompanyCredentialService,
        "set_invoice_settings",
        save_settings,
    ):
        response = asyncio.run(
            set_invoice_settings(
                InvoiceProviderConfigRequest(
                    company_id=COMPANY_ID,
                    invoice_provider="szamlazz_hu",
                    api_key="secret",
                ),
                token=TOKEN,
            )
        )

    assert response.model_dump() == saved
