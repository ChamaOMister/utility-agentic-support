"""Executor against the live mock-billing service (subprocess) and the real support schema."""

import json

import pytest
from support_api.checkpoints import Checkpoints
from support_api.deadline import Deadline
from support_api.identity.attempts import apply_verified
from support_api.integrations.billing.client import BillingClient
from support_api.tools.executor import Executor, to_model_output
from support_api.tools.schemas import ToolContext, ToolError, ToolResult

from tests.conftest import (
    HASH,
    admit_in_fenced,
    assurance,
    make_claimed_conversation,
)

KEY = b"test-hash-key"


@pytest.fixture
async def executor(db, billing_server):
    client = BillingClient(billing_server.base_url, billing_server.api_key)
    yield Executor(db, client, KEY)
    await client.aclose()


async def _ctx(db) -> ToolContext:
    return ToolContext(await make_claimed_conversation(db), Deadline(45), Checkpoints())


async def _verified_ctx(db, persona) -> ToolContext:
    ctx = await _ctx(db)
    attempt = await admit_in_fenced(db, ctx.claim, HASH)
    assert await apply_verified(
        db, ctx.claim, attempt, persona["customer_ref"], persona["first_name"]
    )
    return ctx


def _auth_args(persona, **override) -> str:
    args = {
        "identifier": persona["cpf"],
        "birth_date": persona["birth_date"],
        "phone_last4": persona["phone"][-4:],
    }
    return json.dumps({**args, **override})


async def test_authenticate_then_read_own_data(executor, db, personas):
    a = personas[0]
    ctx = await _ctx(db)
    result, trace = await executor.run("authenticate_customer", _auth_args(a), ctx)
    assert result == ToolResult(data={"first_name": a["first_name"]}, record_count=1)
    assert (await assurance(db, ctx.claim)).customer_ref == a["customer_ref"]

    summary, _ = await executor.run("get_account_summary", "{}", ctx)
    street = a["address"]["street"]
    assert street not in to_model_output(summary)
    assert summary.data["address"] == {
        "neighborhood": a["address"]["neighborhood"],
        "city": a["address"]["city"],
    }

    bills, trace = await executor.run("list_bills", '{"status": "open"}', ctx)
    assert bills.record_count == trace.record_count == len(bills.data) >= 1
    assert all(b["status"] == "open" for b in bills.data)
    detail, _ = await executor.run(
        "get_bill_details", json.dumps({"bill_id": bills.data[0]["bill_id"]}), ctx
    )
    assert detail.data["line_items"]
    # Amounts stay the strings billing sent.
    assert isinstance(detail.data["amount_brl"], str)
    assert f'"{detail.data["amount_brl"]}"' in to_model_output(detail)


async def test_authenticate_with_formatted_and_account_identifiers(
    executor, db, personas
):
    a = personas[0]
    cpf = a["cpf"]
    formatted = f"{cpf[:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:]}"
    ctx = await _ctx(db)
    result, _ = await executor.run(
        "authenticate_customer", _auth_args(a, identifier=formatted), ctx
    )
    assert isinstance(result, ToolResult)
    ctx2 = await _ctx(db)
    result, _ = await executor.run(
        "authenticate_customer", _auth_args(a, identifier=a["account_number"]), ctx2
    )
    assert isinstance(result, ToolResult)


async def test_wrong_phone_fails_and_lock_follows(executor, db, personas):
    a = personas[0]
    ctx = await _ctx(db)
    for _ in range(3):
        result, _ = await executor.run(
            "authenticate_customer", _auth_args(a, phone_last4="0000"), ctx
        )
        assert result == ToolError(code="authentication_failed")
    # Correct answers no longer help: the identifier is locked.
    other = await _ctx(db)
    result, _ = await executor.run("authenticate_customer", _auth_args(a), other)
    assert result == ToolError(code="locked")
    assert (await assurance(db, other.claim)).level == "none"


async def test_foreign_bill_id_is_not_found(executor, db, personas):  # AC 3
    a, b = personas[0], personas[1]
    ctx_a = await _verified_ctx(db, a)
    foreign = b["bills"][0]["bill_id"]
    result, trace = await executor.run(
        "get_bill_details", json.dumps({"bill_id": foreign}), ctx_a
    )
    assert result == ToolError(code="not_found")
    assert trace.error_code == "not_found"
    # ...and A's own list never contains B's bills.
    bills, _ = await executor.run("list_bills", '{"limit": 12}', ctx_a)
    assert foreign not in {x["bill_id"] for x in bills.data}


async def test_empty_result_is_ok_not_invented(executor, db, personas):
    for p in personas:
        if not any(b["status"] == "overdue" for b in p["bills"]):
            ctx = await _verified_ctx(db, p)
            result, trace = await executor.run(
                "list_bills", '{"status": "overdue"}', ctx
            )
            assert result == ToolResult(data=[], record_count=0)
            assert trace.status == "ok" and trace.record_count == 0
            return
    pytest.skip("every persona has an overdue bill")
