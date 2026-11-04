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
from app.services.invoice_providers.szamlazz_hu import (
    SzamlazzHuProvider,
    SzamlazzHuRejectedError,
    SzamlazzHuRequestError,
    SzamlazzHuResponseError,
)


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
                    "enabled_modules": ["leave_attachments", "invoicing"],
                    "is_active": True,
                    "is_deleted": False,
                    "invoice_provider": self.provider,
                    "invoice_api_key_encrypted": self.encrypted_key,
                }
            )
        raise AssertionError(f"Unexpected table: {table_name}")

    def rpc(self, function_name, params):
        if function_name == "get_company_invoice_credentials":
            return FakeQuery({"invoice_provider": self.provider, "invoice_api_key_encrypted": self.encrypted_key})
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


@pytest.mark.parametrize("entered_key", [None, " NEW-Key "])
def test_connection_uses_saved_or_entered_key_without_saving(entered_key):
    encryption_key = Fernet.generate_key()
    encrypted = Fernet(encryption_key).encrypt(b"saved-key").decode()
    client = InvoiceSettingsClient(encrypted_key=encrypted)
    close_client = AsyncMock()
    ping = AsyncMock(return_value=None)

    with (
        patch.object(settings, "ENCRYPTION_MASTER_KEY", encryption_key.decode()),
        patch.object(SupabaseDataService, "get_authenticated_client", AsyncMock(return_value=client)),
        patch.object(SupabaseDataService, "_close_authenticated_client", close_client),
        patch("app.services.company_service.SzamlazzHuProvider") as provider,
    ):
        provider.return_value.test_connection = ping
        result = asyncio.run(CompanyCredentialService.test_invoice_connection(
            TOKEN, COMPANY_ID, "szamlazz_hu", entered_key
        ))

    assert result.model_dump() == {
        "success": True, "status": "connected", "provider_error_code": None
    }
    provider.assert_called_once_with("NEW-Key" if entered_key else "saved-key")
    ping.assert_awaited_once()
    close_client.assert_awaited_once_with(client)
    assert client.rpc_params is None and client.encrypted_key == encrypted


@pytest.mark.parametrize("entered_key", [None, "unsaved-key"])
def test_connection_rejects_other_company_before_contacting_provider(entered_key):
    client = InvoiceSettingsClient()
    close_client = AsyncMock()
    with (
        patch.object(settings, "ENCRYPTION_MASTER_KEY", Fernet.generate_key().decode()),
        patch.object(SupabaseDataService, "get_authenticated_client", AsyncMock(return_value=client)),
        patch.object(SupabaseDataService, "_close_authenticated_client", close_client),
        patch.object(SzamlazzHuProvider, "test_connection", AsyncMock()) as ping,
        pytest.raises(HTTPException) as error,
    ):
        asyncio.run(CompanyCredentialService.test_invoice_connection(
            TOKEN, "10000000-0000-0000-0000-000000000002", "szamlazz_hu", entered_key
        ))
    assert error.value.status_code == 403
    ping.assert_not_awaited()
    close_client.assert_awaited_once_with(client)


@pytest.mark.parametrize("provider,key,status_code", [
    ("billingo", None, 409), ("szamlazz_hu", None, 404),
])
def test_connection_never_uses_a_missing_or_other_providers_saved_key(provider, key, status_code):
    client = InvoiceSettingsClient(provider=provider, encrypted_key=key)
    with (
        patch.object(settings, "ENCRYPTION_MASTER_KEY", Fernet.generate_key().decode()),
        patch.object(SupabaseDataService, "get_authenticated_client", AsyncMock(return_value=client)),
        patch.object(SupabaseDataService, "_close_authenticated_client", AsyncMock()),
        patch.object(SzamlazzHuProvider, "test_connection", AsyncMock()) as ping,
        pytest.raises(HTTPException) as error,
    ):
        asyncio.run(CompanyCredentialService.test_invoice_connection(
            TOKEN, COMPANY_ID, "szamlazz_hu", None
        ))
    assert error.value.status_code == status_code
    ping.assert_not_awaited()


@pytest.mark.parametrize("error,expected", [
    (SzamlazzHuRejectedError("secret", code="3"), "invalid_key"),
    (SzamlazzHuRejectedError("secret", code="136"), "provider_error"),
    (SzamlazzHuRequestError("secret"), "unavailable"),
    (SzamlazzHuResponseError("secret"), "unconfirmed"),
])
def test_connection_returns_safe_distinct_failure_statuses(error, expected):
    with patch.object(SzamlazzHuProvider, "test_connection", AsyncMock(side_effect=error)):
        result = asyncio.run(CompanyCredentialService._test_szamlazz_connection("secret"))
    assert result.success is False and result.status == expected
    assert "secret" not in result.model_dump_json()


def test_connection_endpoint_authentication_validation_and_json_contract():
    import httpx
    from fastapi import FastAPI
    from app.api.v1.endpoints.company import router
    from app.schemas.company import InvoiceConnectionTestResponse

    app = FastAPI()
    app.include_router(router, prefix="/company")
    ping = AsyncMock(return_value=InvoiceConnectionTestResponse(success=True, status="connected"))

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            url = "/company/invoice-settings/test-connection"
            payload = {"company_id": COMPANY_ID, "api_key": "secret"}
            assert (await client.post(url, json=payload)).status_code == 401
            headers = {"Authorization": f"Bearer {TOKEN}"}
            assert (await client.post(url, headers=headers, json={**payload, "api_key": "  "})).status_code == 422
            response = await client.post(url, headers=headers, json=payload)
            assert response.status_code == 200
            assert response.json() == {"success": True, "status": "connected", "provider_error_code": None}

    with patch.object(CompanyCredentialService, "test_invoice_connection", ping):
        asyncio.run(run())
    ping.assert_awaited_once()
    assert str(ping.call_args.kwargs["company_id"]) == COMPANY_ID
    assert ping.call_args.kwargs["api_key"] == "secret"
