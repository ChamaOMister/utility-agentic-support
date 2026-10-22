from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from .auth import ApiError, require_api_key
from .db import get_conn
from .models import bill_line_items, bills, customers, installations

BillStatus = Literal["open", "paid", "overdue"]

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])
Conn = Annotated[AsyncConnection, Depends(get_conn)]


class VerifyRequest(BaseModel):
    identifier: str = Field(max_length=64)
    birth_date: date
    phone_last4: str = Field(pattern=r"^\d{4}$")


class VerifyResponse(BaseModel):
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
    amount_brl: str
    status: BillStatus
    consumption_kwh: int
    tariff_flag: str


class LineItem(BaseModel):
    description: str
    amount_brl: str


class BillDetail(BillSummary):
    issue_date: date
    line_items: list[LineItem]


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def _summary(row) -> dict:
    return {
        "bill_id": row.bill_id,
        "installation_id": row.installation_id,
        "reference_month": row.reference_month,
        "due_date": row.due_date,
        "amount_brl": _money(row.amount_brl),
        "status": row.status,
        "consumption_kwh": row.consumption_kwh,
        "tariff_flag": row.tariff_flag,
    }


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isascii() and ch.isdigit())


@router.post("/identity/verify", response_model=VerifyResponse, response_model_exclude_none=True)
async def verify(body: VerifyRequest, conn: Conn) -> VerifyResponse:
    digits = _digits(body.identifier)
    column = {11: customers.c.cpf, 10: customers.c.account_number}.get(len(digits))
    row = None
    if column is not None:
        row = (await conn.execute(select(customers).where(column == digits))).first()
    # Unknown identifier and wrong answers produce the identical response.
    if row is None or row.birth_date != body.birth_date or _digits(row.phone)[-4:] != body.phone_last4:
        return VerifyResponse(verified=False)
    return VerifyResponse(verified=True, customer_ref=row.customer_ref, first_name=row.first_name)


async def _customer(conn: AsyncConnection, ref: str):
    row = (await conn.execute(select(customers).where(customers.c.customer_ref == ref))).first()
    if row is None:
        raise ApiError(404, "not_found")
    return row


@router.get("/customers/{ref}/account", response_model=Account)
async def get_account(ref: str, conn: Conn) -> Account:
    c = await _customer(conn, ref)
    insts = (
        await conn.execute(
            select(installations)
            .where(installations.c.customer_ref == ref)
            .order_by(installations.c.installation_id)
        )
    ).all()
    return Account(
        customer_ref=c.customer_ref,
        masked_cpf=f"***.***.***-{c.cpf[-2:]}",
        installations=[Installation(installation_id=i.installation_id, label=i.label) for i in insts],
        address=Address(street=c.street, neighborhood=c.neighborhood, city=c.city, state=c.state),
        tariff_class=c.tariff_class,
        account_status=c.account_status,
    )


@router.get("/customers/{ref}/bills", response_model=list[BillSummary])
async def list_bills(
    ref: str,
    conn: Conn,
    status: BillStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=12)] = 12,
) -> list[dict]:
    await _customer(conn, ref)
    stmt = select(bills).where(bills.c.customer_ref == ref)
    if status is not None:
        stmt = stmt.where(bills.c.status == status)
    stmt = stmt.order_by(
        bills.c.reference_month.desc(), bills.c.due_date.desc(), bills.c.bill_id
    ).limit(limit)
    return [_summary(r) for r in (await conn.execute(stmt)).all()]


@router.get("/customers/{ref}/bills/{bill_id}", response_model=BillDetail)
async def get_bill(ref: str, bill_id: str, conn: Conn) -> dict:
    # Ownership is enforced by path scoping: a bill of another customer is simply not found,
    # exactly like a nonexistent one.
    row = (
        await conn.execute(select(bills).where(bills.c.bill_id == bill_id, bills.c.customer_ref == ref))
    ).first()
    if row is None:
        raise ApiError(404, "not_found")
    items = (
        await conn.execute(
            select(bill_line_items)
            .where(bill_line_items.c.bill_id == bill_id)
            .order_by(bill_line_items.c.position)
        )
    ).all()
    return {
        **_summary(row),
        "issue_date": row.issue_date,
        "line_items": [{"description": i.description, "amount_brl": _money(i.amount_brl)} for i in items],
    }
