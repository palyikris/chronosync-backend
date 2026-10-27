from decimal import ROUND_HALF_UP, Decimal

from app.schemas.invoices import InvoiceLineItem


def calculate_line_totals(
    item: InvoiceLineItem, *, currency: str, tax_subject: int
) -> tuple[Decimal, Decimal, Decimal]:
    """Use the same Számla Agent rounding in the review and the outgoing XML."""
    quantum = Decimal("1") if currency == "HUF" else Decimal("0.01")
    rate = Decimal(item.vat_rate) if item.vat_rate[0].isdigit() else Decimal("0")
    unrounded_net = item.net_unit_price * item.quantity

    if currency == "HUF" and tax_subject == -1:
        # Profiles store net unit prices. Derive the gross price before rounding
        # for the provider's documented private-customer (B2C) calculation.
        gross = (unrounded_net * (Decimal("100") + rate) / Decimal("100")).quantize(
            quantum, rounding=ROUND_HALF_UP
        )
        vat = (gross * rate / (Decimal("100") + rate)).quantize(
            quantum, rounding=ROUND_HALF_UP
        )
        return gross - vat, vat, gross

    net = unrounded_net.quantize(quantum, rounding=ROUND_HALF_UP)
    vat = (net * rate / Decimal("100")).quantize(quantum, rounding=ROUND_HALF_UP)
    return net, vat, net + vat
