import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, cast
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, status

from app.core.config import settings
from app.services.supabase_service import SupabaseDataService

logger = logging.getLogger(__name__)


class CompanyCredentialService:
    @staticmethod
    def _get_fernet() -> Fernet:
        master_key = settings.ENCRYPTION_MASTER_KEY.strip()
        if not master_key:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Encryption is not configured on the backend.",
            )

        try:
            return Fernet(master_key.encode("ascii"))
        except (ValueError, TypeError, UnicodeEncodeError) as exc:
            logger.error("The configured encryption master key is invalid.")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Encryption is not configured correctly on the backend.",
            ) from exc

    @classmethod
    async def set_szamlazz_agent_key(
        cls, user_jwt: str, company_id: UUID, agent_key: str
    ) -> None:
        await cls.set_invoice_settings(
            user_jwt=user_jwt,
            company_id=company_id,
            invoice_provider="szamlazz_hu",
            api_key=agent_key,
        )

    @classmethod
    async def set_invoice_settings(
        cls,
        user_jwt: str,
        company_id: UUID,
        invoice_provider: str,
        api_key: str | None,
    ) -> dict[str, Any]:
        if invoice_provider not in {"szamlazz_hu", "billingo"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Unsupported invoice provider.",
            )

        normalized_key: str | None = None
        if api_key is not None:
            normalized_key = api_key.strip()
            if not normalized_key:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="The invoice provider API key must not be blank.",
                )
            if invoice_provider == "szamlazz_hu":
                # Számlázz.hu documents Agent keys as lowercase-only.
                normalized_key = normalized_key.lower()

        plaintext = (
            bytearray(normalized_key.encode("utf-8"))
            if normalized_key is not None
            else None
        )
        encrypted_key = ""
        supabase = None

        try:
            if plaintext is not None:
                encrypted_key = cls._get_fernet().encrypt(bytes(plaintext)).decode(
                    "ascii"
                )
            supabase = await SupabaseDataService.get_authenticated_client(user_jwt)
            await SupabaseDataService._assert_invoice_attachment_access_with_client(
                supabase,
                user_jwt=user_jwt,
                company_id=company_id,
            )
            current_provider, _current_encrypted_key = await cls._get_stored_config(
                supabase, company_id
            )
            if plaintext is None and current_provider != invoice_provider:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Enter the new provider's API key when changing providers.",
                )
            await supabase.rpc(
                "set_company_invoice_credentials",
                {
                    "p_company_id": str(company_id),
                    "p_invoice_provider": invoice_provider,
                    "p_encrypted_key": encrypted_key or None,
                },
            ).execute()
            saved_provider, saved_encrypted_key = await cls._get_stored_config(
                supabase, company_id
            )
            if saved_provider != invoice_provider or (
                encrypted_key and saved_encrypted_key != encrypted_key
            ):
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="The invoice provider settings could not be verified.",
                )
            return {
                "invoice_provider": invoice_provider,
                "api_key_configured": bool(saved_encrypted_key),
            }
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "Failed to store invoice provider settings for company_id=%s.",
                company_id,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Could not store the company credential.",
            ) from exc
        finally:
            if supabase is not None:
                await SupabaseDataService._close_authenticated_client(supabase)
            if plaintext is not None:
                for index in range(len(plaintext)):
                    plaintext[index] = 0
                del plaintext
            del encrypted_key

    @staticmethod
    async def _get_stored_config(
        supabase: Any, company_id: UUID
    ) -> tuple[str, str | None]:
        response = await supabase.rpc("get_company_invoice_credentials", {
            "p_company_id": str(company_id),
        }).execute()
        row = cast(dict[str, Any], response.data or {})
        return (
            str(row.get("invoice_provider") or "szamlazz_hu"),
            cast(str | None, row.get("invoice_api_key_encrypted")),
        )

    @classmethod
    async def get_invoice_settings(
        cls, user_jwt: str, company_id: UUID
    ) -> dict[str, Any]:
        supabase = None
        try:
            supabase = await SupabaseDataService.get_authenticated_client(user_jwt)
            await SupabaseDataService._assert_invoice_attachment_access_with_client(
                supabase,
                user_jwt=user_jwt,
                company_id=company_id,
            )
            provider, encrypted_key = await cls._get_stored_config(supabase, company_id)
            if provider not in {"szamlazz_hu", "billingo"}:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="The company has an unsupported invoice provider.",
                )
            return {
                "invoice_provider": provider,
                "api_key_configured": bool(encrypted_key),
            }
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "Failed to read invoice settings for company_id=%s.", company_id
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Could not read the company invoice settings.",
            ) from exc
        finally:
            if supabase is not None:
                await SupabaseDataService._close_authenticated_client(supabase)

    @classmethod
    @asynccontextmanager
    async def decrypted_szamlazz_agent_key(
        cls, user_jwt: str, company_id: UUID
    ) -> AsyncIterator[str]:
        """Yield a decrypted key only for the duration of the invoice request."""
        fernet = cls._get_fernet()
        plaintext = bytearray()
        supabase = None

        try:
            supabase = await SupabaseDataService.get_authenticated_client(user_jwt)
            await SupabaseDataService._assert_invoice_attachment_access_with_client(
                supabase,
                user_jwt=user_jwt,
                company_id=company_id,
            )
            provider, encrypted_key = await cls._get_stored_config(supabase, company_id)
            if provider != "szamlazz_hu":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Számlázz.hu is not the selected invoice provider.",
                )
            if not encrypted_key:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="No Számla Agent key is configured for this company.",
                )

            try:
                plaintext = bytearray(
                    fernet.decrypt(str(encrypted_key).encode("ascii"))
                )
            except (InvalidToken, ValueError, UnicodeEncodeError) as exc:
                logger.error(
                    "Stored Számla Agent key could not be decrypted for company_id=%s.",
                    company_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="The stored company credential could not be decrypted.",
                ) from exc

            yield plaintext.decode("utf-8")
        finally:
            if supabase is not None:
                await SupabaseDataService._close_authenticated_client(supabase)
            for index in range(len(plaintext)):
                plaintext[index] = 0
            del plaintext
