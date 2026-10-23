"""Response shapes of the mock billing HTTP contract (spec §4)."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

BillStatus = Literal["open", "paid", "overdue"]


class VerifyResult(BaseModel):
    verified: bool
    customer_ref: str | None = None
    first_name: str | None = None


class Installation(BaseModel):
    installation_id: str
    label: str


class Address(BaseModel):
    street: str
    neighborhood: str
    city: str
    state: str


class Account(BaseModel):
    customer_ref: str
    masked_cpf: str
    installations: list[Installation]
    address: Address
    tariff_class: str
    account_status: str


class BillSummary(BaseModel):
    bill_id: str
    installation_id: str
    reference_month: str
    due_date: date
    amount_brl: Decimal
    status: BillStatus
    consumption_kwh: int
    tariff_flag: str


class LineItem(BaseModel):
    description: str
    amount_brl: Decimal


class BillDetail(BillSummary):
    issue_date: date
    line_items: list[LineItem]
