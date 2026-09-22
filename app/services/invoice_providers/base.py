from abc import ABC, abstractmethod
from typing import Any, Mapping


class InvoiceProvider(ABC):
    """Common contract for invoice provider integrations."""

    def __init__(self, api_key: str) -> None:
        if not api_key.strip():
            raise ValueError("An invoice provider API key is required.")
        self.api_key = api_key

    @abstractmethod
    async def create_invoice(
        self, invoice: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Create an invoice from a provider-independent invoice payload."""
        raise NotImplementedError
