import calendar
from datetime import date
from typing import Self
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class SzamlamellekletRequest(BaseModel):
    company_id: UUID = Field(description="Company that owns the selected clients")
    client_ids: list[UUID] = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Selected client IDs within the company",
    )
    start_date: date = Field(description="Inclusive report start date")
    end_date: date = Field(description="Inclusive report end date")
    period_text: str | None = Field(
        default=None,
        max_length=160,
        description=(
            "Deprecated display label retained for older callers. The workbook derives "
            "a localized label from start_date and end_date for each client."
        ),
    )
    update_remaining_hours: bool = Field(
        default=False,
        description=(
            "When true, finalize a complete calendar month and persist each client's "
            "calculated carry-over exactly once."
        ),
    )

    @field_validator("client_ids")
    @classmethod
    def client_ids_must_be_unique(cls, values: list[UUID]) -> list[UUID]:
        if len(values) != len(set(values)):
            raise ValueError("client_ids must not contain duplicates")
        return values

    @model_validator(mode="after")
    def validate_date_range(self) -> Self:
        if self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date")

        if self.update_remaining_hours:
            last_day = calendar.monthrange(
                self.start_date.year, self.start_date.month
            )[1]
            is_full_calendar_month = (
                self.start_date.day == 1
                and self.end_date.year == self.start_date.year
                and self.end_date.month == self.start_date.month
                and self.end_date.day == last_day
            )
            if not is_full_calendar_month:
                raise ValueError(
                    "update_remaining_hours requires one complete calendar month"
                )

        return self
