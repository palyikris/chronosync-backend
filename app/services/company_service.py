import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Any, cast
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
    ) -> None:
        fernet = cls._get_fernet()
        if invoice_provider not in {"szamlazz_hu", "billingo"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Unsupported invoice provider.",
            )

        plaintext = bytearray(api_key.encode("utf-8")) if api_key is not None else None
        encrypted_key = ""

        try:
            if plaintext is not None:
                encrypted_key = fernet.encrypt(bytes(plaintext)).decode("ascii")
            supabase = SupabaseDataService.get_authenticated_client(user_jwt)
            supabase.rpc(
                "set_company_invoice_credentials",
                {
                    "p_company_id": str(company_id),
                    "p_invoice_provider": invoice_provider,
                    "p_encrypted_key": encrypted_key or None,
                },
            ).execute()
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
            if plaintext is not None:
                for index in range(len(plaintext)):
                    plaintext[index] = 0
                del plaintext
            del encrypted_key

    @classmethod
    async def get_invoice_settings(
        cls, user_jwt: str, company_id: UUID
    ) -> dict[str, Any]:
        try:
            supabase = SupabaseDataService.get_authenticated_client(user_jwt)
            response = (
                supabase.table("companies")
                .select("invoice_provider, invoice_api_key_encrypted")
                .eq("id", str(company_id))
                .single()
                .execute()
            )
            row = cast(dict[str, Any], response.data or {})
            provider = row.get("invoice_provider") or "szamlazz_hu"
            if provider not in {"szamlazz_hu", "billingo"}:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="The company has an unsupported invoice provider.",
                )
            return {
                "invoice_provider": provider,
                "api_key_configured": bool(row.get("invoice_api_key_encrypted")),
            }
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Failed to read invoice settings for company_id=%s.", company_id)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Could not read the company invoice settings.",
            ) from exc

    @classmethod
    @asynccontextmanager
    async def decrypted_szamlazz_agent_key(
        cls, user_jwt: str, company_id: UUID
    ) -> AsyncIterator[str]:
        """Yield a decrypted key only for the duration of the invoice request."""
        fernet = cls._get_fernet()
        plaintext = bytearray()

        try:
            supabase = SupabaseDataService.get_authenticated_client(user_jwt)
            response = (
                supabase.table("companies")
                .select(
                    "invoice_api_key_encrypted, szamlazz_agent_key_encrypted"
                )
                .eq("id", str(company_id))
                .single()
                .execute()
            )
            row = cast(dict[str, Any], response.data or {})
            encrypted_key = row.get("invoice_api_key_encrypted") or row.get(
                "szamlazz_agent_key_encrypted"
            )
            if not encrypted_key:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="No Számla Agent key is configured for this company.",
                )

            try:
                plaintext = bytearray(fernet.decrypt(str(encrypted_key).encode("ascii")))
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
            for index in range(len(plaintext)):
                plaintext[index] = 0
            del plaintext
