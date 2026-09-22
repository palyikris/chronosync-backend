from typing import Any, Mapping

from app.services.invoice_providers.base import InvoiceProvider


class SzamlazzHuProvider(InvoiceProvider):
    """Számlázz.hu provider boundary for the future XML integration."""

    async def create_invoice(
        self, invoice: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        raise NotImplementedError(
            "Számlázz.hu invoice creation is not enabled yet."
        )
