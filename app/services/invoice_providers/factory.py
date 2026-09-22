from app.services.invoice_providers.base import InvoiceProvider
from app.services.invoice_providers.billingo import BillingoProvider
from app.services.invoice_providers.szamlazz_hu import SzamlazzHuProvider


_PROVIDER_CLASSES: dict[str, type[InvoiceProvider]] = {
    "szamlazz_hu": SzamlazzHuProvider,
    "billingo": BillingoProvider,
}


def get_invoice_provider(provider_name: str, api_key: str) -> InvoiceProvider:
    """Resolve a configured provider without exposing credentials to callers."""
    normalized_name = provider_name.strip().lower()
    provider_class = _PROVIDER_CLASSES.get(normalized_name)
    if provider_class is None:
        raise ValueError(f"Unsupported invoice provider: {provider_name}")
    return provider_class(api_key)
