from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


InvoiceLanguage = Literal[
    "hu",
    "en",
    "de",
    "it",
    "ro",
    "sk",
    "hr",
    "fr",
    "es",
    "cz",
    "pl",
    "bg",
    "nl",
    "ru",
    "si",
]

ALLOWED_VAT_CODES = {
    "0",
    "1",
    "2",
    "2.1",
    "3",
    "4",
    "4.8",
    "5",
    "5.5",
    "6",
    "7",
    "7.7",
    "8",
    "8.1",
    "9",
    "9.5",
    "10",
    "11",
    "12",
    "13",
    "13.5",
    "14",
    "15",
    "16",
    "17",
    "18",
    "19",
    "20",
    "21",
    "22",
    "23",
    "24",
    "25",
    "25.5",
    "26",
    "27",
    "TAHK",
    "TAM",
    "AAM",
    "EUT",
    "EUKT",
    "F.AFA",
    "K.AFA",
    "HO",
    "EUE",
    "EUFADE",
    "EUFAD37",
    "ATK",
    "NAM",
    "EAM",
    "KBAUK",
    "KBAET",
}


SUPPORTED_CURRENCIES = set(
    "HUF EUR CHF USD AED ALL AUD BAM BGN BRL CAD CNY CZK DKK EEK GBP HKD HRK "
    "IDR ILS INR ISK JPY KRW KWD KSH KZT LTL LVL MXN MYR NOK NZD PHP PLN "
    "RON RSD RUB SEK SGD THB TRY TWD UAH VND ZAR".split()
)


class InvoiceTextModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("*", mode="after")
    @classmethod
    def reject_invalid_xml_characters(cls, value: object) -> object:
        if isinstance(value, str) and any(
            not (
                char in "\t\n\r"
                or " " <= char <= "\ud7ff"
                or "\ue000" <= char <= "\ufffd"
                or "\U00010000" <= char <= "\U0010ffff"
            )
            for char in value
        ):
            raise ValueError("text contains characters that cannot be sent in XML")
        return value


class InvoiceCustomer(InvoiceTextModel):
    name: str = Field(min_length=1, max_length=255)
    country: str | None = Field(default=None, max_length=100)
    postal_code: str = Field(min_length=1, max_length=32)
    city: str = Field(min_length=1, max_length=120)
    address: str = Field(min_length=1, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    send_email: bool = False
    tax_subject: Literal[-1, 0, 1, 6, 7] = 0
    tax_number: str | None = Field(default=None, max_length=32)
    eu_tax_number: str | None = Field(default=None, max_length=32)
    group_tax_number: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def require_email_when_sending(self) -> Self:
        if self.send_email and not self.email:
            raise ValueError("email is required when send_email is true")
        if self.tax_subject == 1 and not (self.tax_number or self.group_tax_number):
            raise ValueError("a Hungarian tax number is required for tax_subject 1")
        if self.tax_subject == 6 and not self.eu_tax_number:
            raise ValueError("eu_tax_number is required for tax_subject 6")
        if self.tax_subject == 7 and not self.tax_number:
            raise ValueError("tax_number is required for tax_subject 7")
        if self.tax_subject == -1 and any(
            (self.tax_number, self.eu_tax_number, self.group_tax_number)
        ):
            raise ValueError("tax numbers must be empty for a private individual")
        return self


class InvoiceLineItem(InvoiceTextModel):
    name: str = Field(min_length=1, max_length=255)
    quantity: Decimal = Field(gt=0)
    unit: str = Field(default="óra", min_length=1, max_length=32)
    net_unit_price: Decimal = Field(ge=0)
    vat_rate: str = Field(default="27", min_length=1, max_length=16)
    comment: str | None = Field(default=None, max_length=500)

    @field_validator("vat_rate", mode="before")
    @classmethod
    def normalize_vat_rate(cls, value: object) -> str:
        normalized = str(value).strip().upper()
        if normalized not in ALLOWED_VAT_CODES:
            raise ValueError("vat_rate is not supported by Számlázz.hu")
        return normalized


class InvoiceCreateRequest(InvoiceTextModel):
    issue_date: date
    fulfillment_date: date
    due_date: date
    payment_method: str = Field(default="Átutalás", min_length=1, max_length=64)
    currency: str = Field(default="HUF", pattern=r"^[A-Z]{3}$")
    exchange_rate_bank: str = Field(default="MNB", min_length=1, max_length=64)
    exchange_rate: Decimal | None = Field(default=None, gt=0)
    language: InvoiceLanguage = "hu"
    comment: str | None = Field(default=None, max_length=500)
    order_number: str = Field(min_length=1, max_length=100)
    external_id: str | None = Field(default=None, max_length=100)
    invoice_prefix: str | None = Field(default=None, max_length=32)
    e_invoice: bool = False
    download_pdf: bool = True
    preview: bool = True
    customer: InvoiceCustomer
    items: list[InvoiceLineItem] = Field(min_length=1, max_length=1000)

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> object:
        normalized = value.strip().upper() if isinstance(value, str) else value
        if not isinstance(normalized, str) or normalized not in SUPPORTED_CURRENCIES:
            raise ValueError("currency is not supported by Számlázz.hu")
        return normalized

    @model_validator(mode="after")
    def validate_dates(self) -> Self:
        if self.due_date < self.issue_date:
            raise ValueError("due_date must be on or after issue_date")
        if (
            self.currency != "HUF"
            and self.exchange_rate is None
            and self.exchange_rate_bank != "MNB"
        ):
            raise ValueError("an explicit exchange rate is required unless the bank is MNB")
        return self


class CreatedInvoice(BaseModel):
    invoice_number: str | None = None
    net_total: Decimal | None = None
    gross_total: Decimal | None = None
    outstanding_total: Decimal | None = None
    customer_account_url: str | None = None
    pdf: bytes | None = None
    preview: bool


class InvoicePreviewRequest(BaseModel):
    company_id: UUID
    invoice: InvoiceCreateRequest


class ClientInvoicePreviewRequest(BaseModel):
    company_id: UUID
    client_id: UUID
    period_start: date
    period_end: date

    @model_validator(mode="after")
    def validate_period(self) -> Self:
        if self.period_end < self.period_start:
            raise ValueError("period_end must be on or after period_start")
        if (self.period_end - self.period_start).days > 366:
            raise ValueError("invoice preview periods cannot exceed 366 days")
        return self


class ClientInvoiceReviewRequest(BaseModel):
    company_id: UUID
    client_id: UUID
    period_start: date
    period_end: date

    @model_validator(mode="after")
    def validate_period(self) -> Self:
        if self.period_end < self.period_start:
            raise ValueError("period_end must be on or after period_start")
        if (self.period_end - self.period_start).days > 366:
            raise ValueError("invoice periods cannot exceed 366 days")
        return self


class InvoiceReviewRecipient(BaseModel):
    name: str
    country: str | None = None
    postal_code: str
    city: str
    address: str
    email: str | None = None
    send_email: bool
    tax_subject: Literal[-1, 0, 1, 6, 7]
    tax_number: str | None = None
    eu_tax_number: str | None = None
    group_tax_number: str | None = None


class InvoiceReviewEntry(BaseModel):
    id: UUID
    work_date: date
    hours_logged: Decimal
    description: str
    project_name: str


class ClientInvoiceReview(BaseModel):
    company_id: UUID
    client_id: UUID
    client_code: str
    client_name: str
    period_start: date
    period_end: date
    issue_date: date
    fulfillment_date: date
    due_date: date
    payment_method: str
    language: InvoiceLanguage
    billing_model: Literal["hourly", "fixed_monthly"]
    item_name: str
    quantity: Decimal
    unit: str
    net_unit_price: Decimal
    vat_code: str
    currency: str
    exchange_rate_bank: str | None = None
    billing_profile_updated_at: datetime | None = None
    net_total: Decimal
    vat_total: Decimal
    gross_total: Decimal
    recipient: InvoiceReviewRecipient
    approved_entries: list[InvoiceReviewEntry]
    review_fingerprint: str


class ClientInvoiceIssueRequest(ClientInvoiceReviewRequest):
    review_fingerprint: str = Field(min_length=64, max_length=64)
    confirm: Literal[True]


InvoiceLedgerStatus = Literal["issuing", "issued", "uncertain", "failed"]


class InvoiceHistoryItem(BaseModel):
    id: UUID
    company_id: UUID
    client_id: UUID
    client_name: str
    period_start: date
    period_end: date
    status: InvoiceLedgerStatus
    provider: Literal["szamlazz_hu"]
    external_id: str
    recipient_name: str
    recipient_email: str | None = None
    send_email: bool
    quantity: Decimal
    unit: str
    net_unit_price: Decimal
    vat_code: str
    currency: str
    net_total: Decimal
    vat_total: Decimal
    gross_total: Decimal
    invoice_number: str | None = None
    pdf_available: bool
    provider_error_code: str | None = None
    provider_error_message: str | None = None
    attempt_count: int
    issued_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class InvoiceReconciliationRequest(BaseModel):
    company_id: UUID
    resolution: Literal["issued", "not_issued"]
    confirm: Literal[True]
    invoice_number: str | None = Field(default=None, max_length=100)
    net_total: Decimal | None = Field(default=None, ge=0)
    gross_total: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_resolution_details(self) -> Self:
        if self.resolution == "issued":
            if not self.invoice_number or not self.invoice_number.strip():
                raise ValueError("invoice_number is required for issued reconciliation")
            if self.net_total is None or self.gross_total is None:
                raise ValueError("provider totals are required for issued reconciliation")
        return self
