from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import re
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class InvoiceEmailAttachment:
    filename: str
    content: bytes = field(repr=False)
    media_type: str = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def __post_init__(self) -> None:
        if (not re.fullmatch(r"[A-Za-z0-9_-]{1,120}\.xlsx", self.filename)
                or not isinstance(self.content, bytes)
                or not 0 < len(self.content) <= 2_000_000
                or not self.content.startswith(b"PK\x03\x04")
                or self.media_type != "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"):
            raise ValueError("Invalid invoice report attachment.")


class InvoiceProvider(ABC):
    """Common contract for invoice provider integrations."""

    def __init__(self, api_key: str) -> None:
        if not api_key.strip():
            raise ValueError("An invoice provider API key is required.")
        self.api_key = api_key

    @abstractmethod
    async def create_invoice(
        self, invoice: Mapping[str, Any], *, attachments: Sequence[InvoiceEmailAttachment] = ()
    ) -> Mapping[str, Any]:
        """Create an invoice from a provider-independent invoice payload."""
        raise NotImplementedError
