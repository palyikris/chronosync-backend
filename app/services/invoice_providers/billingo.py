from typing import Any, Mapping

from app.services.invoice_providers.base import InvoiceProvider


class BillingoProvider(InvoiceProvider):
    """Billingo API v3 provider boundary for the future JSON integration."""

    def request_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-KEY": self.api_key,
        }

    async def create_invoice(
        self, invoice: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        raise NotImplementedError(
            "Billingo invoice creation is not enabled yet."
        )
