import asyncio
import io
import json
import logging
from dataclasses import asdict
from datetime import date

import httpx
import pytest
from sqlalchemy import text
from support_api import logging as uas_logging
from support_api.checkpoints import Checkpoints, SimulatedCrash
from support_api.deadline import Deadline
from support_api.identity.attempts import IdentityLimits
from support_api.identity.normalize import lockout_key, normalize
from support_api.integrations.billing.client import BillingClient
from support_api.tools.executor import Executor, to_model_output
from support_api.tools.schemas import ToolContext, ToolError, ToolResult
from support_api.turns.claim import ClaimLost

from tests.conftest import (
    admit_in_fenced,
    assurance,
    expire_verification,
    fail_attempt,
    make_claimed_conversation,
    take_over,
)

BASE = "http://billing.test"
KEY = b"test-hash-key"
VERIFY_URL = f"{BASE}/v1/identity/verify"
ACCOUNT_URL = f"{BASE}/v1/customers/cus_a/account"
BILLS_URL = f"{BASE}/v1/customers/cus_a/bills"
CPF = "529.982.247-25"
CPF_DIGITS = "52998224725"
AUTH_ARGS = json.dumps(
    {"identifier": CPF, "birth_date": "1980-02-01", "phone_last4": "4321"}
)
ACCOUNT_JSON = {
    "customer_ref": "cus_a",
    "masked_cpf": "***.***.***-25",
    "installations": [{"installation_id": "i1", "label": "Casa"}],
    "address": {
        "street": "Rua Secreta, 100",
        "neighborhood": "Centro",
        "city": "Recife",
        "state": "PE",
    },
    "tariff_class": "residencial",
    "account_status": "active",
}
BILL_JSON = {
    "bill_id": "b1",
    "installation_id": "i1",
    "reference_month": "2026-09",
    "due_date": "2026-10-10",
    "amount_brl": "123.45",
    "status": "open",
    "consumption_kwh": 200,
    "tariff_flag": "verde",
}


@pytest.fixture
async def billing():
    client = BillingClient(BASE, "svc-key", sleep=_no_sleep, rng=lambda: 0.0)
    yield client
    await client.aclose()


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
def executor(db, billing):
    return Executor(db, billing, KEY)


@pytest.fixture
def ctx(claimed_conversation):
    return ToolContext(claimed_conversation, Deadline(45), Checkpoints())


@pytest.fixture
def verified_ctx(verified_conversation):
    return ToolContext(verified_conversation, Deadline(45), Checkpoints())


async def _slots(db, claim) -> list[str]:
    async with db.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT status FROM support.verification_attempts "
                "WHERE conversation_id = :c ORDER BY created_at"
            ),
            {"c": claim.conversation_id},
        )
        return [r.status for r in rows]


async def _tool_rows(db, claim):
    async with db.begin() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT name, status, error_code, attempts, record_count "
                    "FROM support.tool_calls WHERE turn_id = :t ORDER BY created_at"
                ),
                {"t": claim.turn_id},
            )
        ).all()


async def _snapshot(db, claim, *hashes) -> tuple:
    async with db.begin() as conn:
        slots = (
            await conn.execute(
                text("SELECT count(*) FROM support.verification_attempts")
            )
        ).scalar_one()
        lockouts = (
            await conn.execute(
                text(
                    "SELECT identifier_hash, failed_count, locked_until, window_started_at "
                    "FROM support.identifier_lockouts ORDER BY identifier_hash"
                )
            )
        ).all()
        auth = (
            await conn.execute(
                text(
                    "SELECT failed_attempts, locked_until, window_started_at "
                    "FROM support.conversation_auth WHERE conversation_id = :c"
                ),
                {"c": claim.conversation_id},
            )
        ).all()
    return slots, [tuple(r) for r in lockouts], [tuple(r) for r in auth]


# ---------------------------------------------------------------------------
# argument handling
# ---------------------------------------------------------------------------
async def test_data_tools_reject_identifier_argument(executor, verified_ctx):  # AC 3
    result, trace = await executor.run(
        "list_bills", '{"customer_ref": "cus_b"}', verified_ctx
    )
    assert result == ToolError(code="invalid_arguments")
    assert (trace.status, trace.error_code) == ("error", "invalid_arguments")


@pytest.mark.parametrize(
    "name,raw",
    [
        ("list_bills", "not json"),
        ("list_bills", '{"limit": 13}'),
        ("list_bills", '{"limit": 0}'),
        ("list_bills", '{"limit": "5"}'),
        ("list_bills", '{"status": "bogus"}'),
        ("list_bills", "[]"),
        ("get_bill_details", "{}"),
        ("get_account_summary", '{"x": 1}'),
        ("no_such_tool", "{}"),
        ("authenticate_customer", "{}"),
        (
            "authenticate_customer",
            '{"identifier": "52998224725", "birth_date": "1980-02-01", "phone_last4": "123"}',
        ),
        (
            "authenticate_customer",
            '{"identifier": "52998224725", "birth_date": "1980-02-01", "phone_last4": "12٣4"}',
        ),
        (
            "authenticate_customer",
            '{"identifier": "52998224725", "birth_date": "01/02/1980", "phone_last4": "1234"}',
        ),
        (
            "authenticate_customer",
            '{"identifier": "123", "birth_date": "1980-02-01", "phone_last4": "1234"}',
        ),
    ],
)
async def test_invalid_arguments(executor, verified_ctx, respx_mock, name, raw):
    result, trace = await executor.run(name, raw, verified_ctx)
    assert result == ToolError(code="invalid_arguments")
    assert trace.error_code == "invalid_arguments" and trace.attempts == 0
    assert respx_mock.calls.call_count == 0


def test_empty_hash_key_rejected(db, billing):
    with pytest.raises(ValueError):
        Executor(db, billing, b"")


def test_tool_definitions_are_forbid_function_tools(executor):
    defs = {d["name"]: d for d in executor.tool_definitions()}
    assert set(defs) == {
        "authenticate_customer",
        "get_account_summary",
        "list_bills",
        "get_bill_details",
    }
    for d in defs.values():
        assert d["type"] == "function" and d["strict"] is False
        assert d["parameters"]["type"] == "object"
        assert d["parameters"]["additionalProperties"] is False
        assert "untrusted" in d["description"]
        assert "title" not in d["parameters"]
    for name in ("get_account_summary", "list_bills", "get_bill_details"):
        props = defs[name]["parameters"]["properties"]
        assert not {"customer_ref", "identifier", "cpf"} & set(props)
    assert set(defs["authenticate_customer"]["parameters"]["properties"]) == {
        "identifier",
        "birth_date",
        "phone_last4",
    }


async def test_model_output_wrapper():
    assert json.loads(to_model_output(ToolError(code="not_found"))) == {
        "untrusted_data": None,
        "status": "not_found",
    }


async def test_model_output_serializes_decimals_and_dates():
    from decimal import Decimal

    out = to_model_output(
        ToolResult(
            data=[{"amount": Decimal("10.50"), "due": date(2026, 10, 1), "n": "ç"}],
            record_count=1,
        )
    )
    assert "ç" in out  # ensure_ascii=False
    assert json.loads(out) == {
        "untrusted_data": [{"amount": "10.50", "due": "2026-10-01", "n": "ç"}],
        "status": "ok",
    }


# ---------------------------------------------------------------------------
# assurance
# ---------------------------------------------------------------------------
async def test_data_tool_without_verification(executor, ctx, respx_mock):
    result, trace = await executor.run("list_bills", "{}", ctx)
    assert result == ToolError(code="authentication_required")
    assert trace.error_code == "authentication_required"
    assert respx_mock.calls.call_count == 0


async def test_expiry_mid_turn(
    executor, verified_ctx, db, respx_mock
):  # Review Focus 3
    await expire_verification(db, verified_ctx.claim.conversation_id)
    result, trace = await executor.run("list_bills", "{}", verified_ctx)
    assert result.code == "authentication_required"
    assert trace.error_code == "verification_expired"
    assert respx_mock.calls.call_count == 0


async def test_claim_lost_before_data_call_sends_nothing(
    executor, verified_ctx, respx_mock, db
):
    await take_over(db, verified_ctx.claim.conversation_id)
    with pytest.raises(ClaimLost):
        await executor.run("list_bills", "{}", verified_ctx)
    assert respx_mock.calls.call_count == 0
    assert await _tool_rows(db, verified_ctx.claim) == []


# ---------------------------------------------------------------------------
# data tools
# ---------------------------------------------------------------------------
async def test_account_summary_hides_street(executor, verified_ctx, respx_mock, db):
    route = respx_mock.get(ACCOUNT_URL).mock(
        return_value=httpx.Response(200, json=ACCOUNT_JSON)
    )
    result, trace = await executor.run("get_account_summary", "{}", verified_ctx)
    assert route.call_count == 1
    assert isinstance(result, ToolResult)
    assert result.data == {
        "installations": [{"installation_id": "i1", "label": "Casa"}],
        "tariff_class": "residencial",
        "account_status": "active",
        "address": {"neighborhood": "Centro", "city": "Recife"},
    }
    blob = to_model_output(result)
    assert "Rua Secreta" not in blob and "cus_a" not in blob and "***" not in blob
    assert (trace.status, trace.error_code, trace.attempts) == ("ok", None, 1)
    assert await _tool_rows(db, verified_ctx.claim) == [
        ("get_account_summary", "ok", None, 1, 1)
    ]


async def test_list_bills_defaults_and_filters(executor, verified_ctx, respx_mock):
    route = respx_mock.get(BILLS_URL).mock(
        return_value=httpx.Response(200, json=[BILL_JSON])
    )
    result, trace = await executor.run("list_bills", "{}", verified_ctx)
    assert dict(route.calls.last.request.url.params) == {"limit": "6"}
    assert result.data == [
        {**BILL_JSON, "due_date": "2026-10-10", "amount_brl": "123.45"}
    ]
    assert trace.record_count == 1 and result.record_count == 1

    await executor.run("list_bills", '{"status": "overdue", "limit": 12}', verified_ctx)
    assert dict(route.calls.last.request.url.params) == {
        "limit": "12",
        "status": "overdue",
    }


async def test_empty_bill_list_is_ok_with_zero_records(
    executor, verified_ctx, respx_mock
):
    respx_mock.get(BILLS_URL).mock(return_value=httpx.Response(200, json=[]))
    result, trace = await executor.run("list_bills", "{}", verified_ctx)
    assert result == ToolResult(data=[], record_count=0)
    assert trace.status == "ok" and trace.record_count == 0


async def test_get_bill_details(executor, verified_ctx, respx_mock):
    detail = {
        **BILL_JSON,
        "issue_date": "2026-09-25",
        "line_items": [{"description": "Energia", "amount_brl": "123.45"}],
    }
    respx_mock.get(f"{BILLS_URL}/b1").mock(
        return_value=httpx.Response(200, json=detail)
    )
    result, _ = await executor.run(
        "get_bill_details", '{"bill_id": "b1"}', verified_ctx
    )
    assert result.data["line_items"] == [
        {"description": "Energia", "amount_brl": "123.45"}
    ]
    assert result.data["issue_date"] == "2026-09-25"
    assert result.record_count == 1


async def test_invalid_bill_id_is_not_found_without_request(
    executor, verified_ctx, respx_mock
):
    result, trace = await executor.run(
        "get_bill_details", '{"bill_id": "../customers/cus_b"}', verified_ctx
    )
    assert result == ToolError(code="not_found")
    assert trace.error_code == "not_found" and respx_mock.calls.call_count == 0


@pytest.mark.parametrize(
    "response,code",
    [
        (httpx.Response(404), "not_found"),
        (httpx.Response(401), "upstream_unavailable"),
        (httpx.Response(200, json={"bogus": 1}), "upstream_unavailable"),
        (httpx.ReadTimeout("slow"), "upstream_timeout"),
    ],
)
async def test_billing_errors_map_to_codes(
    executor, verified_ctx, respx_mock, response, code
):
    route = respx_mock.get(f"{BILLS_URL}/b1")
    if isinstance(response, Exception):
        route.mock(side_effect=response)
    else:
        route.mock(return_value=response)
    result, trace = await executor.run(
        "get_bill_details", '{"bill_id": "b1"}', verified_ctx
    )
    assert result == ToolError(code=code)
    assert trace.status == "error" and trace.error_code == code
    assert trace.attempts >= 1


async def test_retries_are_reported_in_trace(executor, verified_ctx, respx_mock):
    respx_mock.get(ACCOUNT_URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=ACCOUNT_JSON)]
    )
    result, trace = await executor.run("get_account_summary", "{}", verified_ctx)
    assert isinstance(result, ToolResult) and trace.attempts == 2


async def test_expired_turn_deadline_maps_to_upstream_timeout(
    executor, verified_conversation, respx_mock, fake_clock
):
    ctx = ToolContext(
        verified_conversation, Deadline(45, clock=fake_clock), Checkpoints()
    )
    fake_clock.advance(46)
    result, trace = await executor.run("list_bills", "{}", ctx)
    assert result == ToolError(code="upstream_timeout")
    assert trace.error_code == "upstream_timeout"
    assert respx_mock.calls.call_count == 0


async def test_unexpected_exception_is_internal_error(
    executor, verified_ctx, respx_mock, monkeypatch
):
    async def boom(*a, **k):
        raise RuntimeError("secret detail 52998224725")

    monkeypatch.setattr(executor._billing, "get_account", boom)
    result, trace = await executor.run("get_account_summary", "{}", verified_ctx)
    assert result == ToolError(code="internal_error")
    assert trace.error_code == "internal_error"


async def test_base_exception_propagates(executor, verified_ctx, monkeypatch):
    async def crash(*a, **k):
        raise SimulatedCrash("x")

    monkeypatch.setattr(executor._billing, "get_account", crash)
    with pytest.raises(SimulatedCrash):
        await executor.run("get_account_summary", "{}", verified_ctx)


# ---------------------------------------------------------------------------
# authenticate_customer
# ---------------------------------------------------------------------------
def _verify_ok():
    return httpx.Response(
        200, json={"verified": True, "customer_ref": "cus_a", "first_name": "Ana"}
    )


async def test_authenticate_success_grants_kba(executor, ctx, respx_mock, db):
    route = respx_mock.post(VERIFY_URL).mock(return_value=_verify_ok())
    result, trace = await executor.run("authenticate_customer", AUTH_ARGS, ctx)
    assert result == ToolResult(data={"first_name": "Ana"}, record_count=1)
    assert (trace.status, trace.attempts, trace.record_count) == ("ok", 1, 1)
    a = await assurance(db, ctx.claim)
    assert (a.level, a.customer_ref) == ("kba", "cus_a")
    assert await _slots(db, ctx.claim) == ["verified"]
    # The model never sees the customer_ref.
    assert "cus_a" not in to_model_output(result)
    assert route.call_count == 1


async def test_verify_receives_normalized_digits(executor, ctx, respx_mock):
    route = respx_mock.post(VERIFY_URL).mock(return_value=_verify_ok())
    await executor.run("authenticate_customer", AUTH_ARGS, ctx)
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "identifier": CPF_DIGITS,
        "birth_date": "1980-02-01",
        "phone_last4": "4321",
    }


async def test_account_number_identifier_is_accepted(executor, ctx, respx_mock):
    route = respx_mock.post(VERIFY_URL).mock(return_value=_verify_ok())
    args = json.dumps(
        {"identifier": "98512 69616", "birth_date": "1994-12-25", "phone_last4": "3015"}
    )
    result, _ = await executor.run("authenticate_customer", args, ctx)
    assert isinstance(result, ToolResult)
    assert json.loads(route.calls.last.request.content)["identifier"] == "9851269616"


async def test_wrong_answers_return_authentication_failed_and_count(
    executor, ctx, respx_mock, db
):
    route = respx_mock.post(VERIFY_URL).mock(
        return_value=httpx.Response(200, json={"verified": False})
    )
    for _ in range(3):
        result, trace = await executor.run("authenticate_customer", AUTH_ARGS, ctx)
        assert result == ToolError(code="authentication_failed")
        assert trace.error_code == "authentication_failed" and trace.attempts == 1
    assert route.call_count == 3
    assert await _slots(db, ctx.claim) == ["failed"] * 3
    assert (await assurance(db, ctx.claim)).level == "none"
    # Fourth attempt: locked, no upstream call.
    result, trace = await executor.run("authenticate_customer", AUTH_ARGS, ctx)
    assert result == ToolError(code="locked") and route.call_count == 3
    assert trace.attempts == 0


async def test_locked_identifier_makes_no_upstream_call(executor, ctx, respx_mock, db):
    key = lockout_key(normalize(CPF), KEY)
    for _ in range(3):
        await fail_attempt(db, ctx.claim, key)
    other = ToolContext(
        await make_claimed_conversation(db), Deadline(45), Checkpoints()
    )
    result, _ = await executor.run("authenticate_customer", AUTH_ARGS, other)
    assert result == ToolError(code="locked")
    assert respx_mock.calls.call_count == 0


async def test_slots_full_is_verification_busy(executor, ctx, respx_mock, db):
    key = lockout_key(normalize(CPF), KEY)
    for _ in range(3):  # three in-flight slots for this identifier
        await admit_in_fenced(db, await make_claimed_conversation(db), key)
    result, trace = await executor.run("authenticate_customer", AUTH_ARGS, ctx)
    assert result == ToolError(code="verification_busy")
    assert trace.error_code == "verification_busy"
    assert respx_mock.calls.call_count == 0


async def test_rejected_admission_leaves_no_committed_state(
    executor, ctx, respx_mock, db
):
    for h in (
        "1" * 64,
        "2" * 64,
        "3" * 64,
    ):  # lock the conversation via other identifiers
        await fail_attempt(db, ctx.claim, h)
    before = await _snapshot(db, ctx.claim)
    result, _ = await executor.run("authenticate_customer", AUTH_ARGS, ctx)
    assert result == ToolError(code="locked")
    assert await _snapshot(db, ctx.claim) == before
    key = lockout_key(normalize(CPF), KEY)
    async with (
        db.begin() as conn
    ):  # not even the identifier row created before the rejection
        assert (
            await conn.execute(
                text(
                    "SELECT count(*) FROM support.identifier_lockouts "
                    "WHERE identifier_hash = :h"
                ),
                {"h": key},
            )
        ).scalar_one() == 0


@pytest.mark.parametrize(
    "response,code",
    [
        (httpx.Response(503), "upstream_unavailable"),
        (httpx.ReadTimeout("slow"), "upstream_timeout"),
        (httpx.Response(200, json={"verified": True}), "upstream_unavailable"),
    ],
)
async def test_upstream_error_frees_slot_and_does_not_count(
    executor, ctx, respx_mock, db, response, code
):
    route = respx_mock.post(VERIFY_URL)
    if isinstance(response, Exception):
        route.mock(side_effect=[response])
    else:
        route.mock(return_value=response)
    result, trace = await executor.run("authenticate_customer", AUTH_ARGS, ctx)
    assert result == ToolError(code=code) and trace.error_code == code
    assert route.call_count == 1  # verify is never retried
    assert await _slots(db, ctx.claim) == ["upstream_error"]
    async with db.begin() as conn:
        failed = (
            await conn.execute(
                text(
                    "SELECT coalesce(sum(failed_count), 0) FROM support.identifier_lockouts"
                )
            )
        ).scalar_one()
    assert failed == 0
    assert (await assurance(db, ctx.claim)).level == "none"


async def test_claim_lost_before_verify_sent_records_no_outcome(
    executor, ctx, respx_mock, db, pause_at
):
    gate = pause_at("after_verify_admit")
    ctx.checkpoints = gate.checkpoints
    task = asyncio.create_task(executor.run("authenticate_customer", AUTH_ARGS, ctx))
    await asyncio.wait_for(gate.reached.wait(), 10)
    await take_over(db, ctx.claim.conversation_id)
    gate.release()
    with pytest.raises(ClaimLost):
        await task
    assert respx_mock.calls.call_count == 0
    assert await _slots(db, ctx.claim) == ["in_flight"]  # recovery abandons it later
    assert (await assurance(db, ctx.claim)).level == "none"
    assert await _tool_rows(db, ctx.claim) == []


async def test_claim_lost_after_verify_records_outcome_and_raises(
    executor, ctx, respx_mock, db
):
    async def verified_then_takeover(request):
        await take_over(db, ctx.claim.conversation_id)
        return _verify_ok()

    respx_mock.post(VERIFY_URL).mock(side_effect=verified_then_takeover)
    with pytest.raises(ClaimLost):
        await executor.run("authenticate_customer", AUTH_ARGS, ctx)
    assert await _slots(db, ctx.claim) == ["verified"]
    assert (await assurance(db, ctx.claim)).level == "none"  # assurance never granted


async def test_guard_deadline_expired_on_verify_frees_slot(
    executor, ctx, respx_mock, db, fake_clock
):
    ctx.deadline = Deadline(45, clock=fake_clock)

    class Advance(Checkpoints):
        async def hit(self, name):
            if name == "after_verify_admit":
                fake_clock.advance(46)

    ctx.checkpoints = Advance()
    result, trace = await executor.run("authenticate_customer", AUTH_ARGS, ctx)
    assert result == ToolError(code="upstream_timeout")
    assert trace.error_code == "upstream_timeout"
    assert respx_mock.calls.call_count == 0
    assert await _slots(db, ctx.claim) == ["upstream_error"]
    # slot freed and nothing counted: a fresh attempt is admitted
    ctx.deadline = Deadline(45)
    ctx.checkpoints = Checkpoints()
    respx_mock.post(VERIFY_URL).mock(return_value=_verify_ok())
    result, _ = await executor.run("authenticate_customer", AUTH_ARGS, ctx)
    assert isinstance(result, ToolResult)


async def test_crash_after_admit_propagates_and_leaves_slot(
    executor, ctx, respx_mock, db, crash_at
):
    ctx.checkpoints = crash_at("after_verify_admit")
    with pytest.raises(SimulatedCrash):
        await executor.run("authenticate_customer", AUTH_ARGS, ctx)
    assert await _slots(db, ctx.claim) == ["in_flight"]
    assert respx_mock.calls.call_count == 0


async def test_custom_limits_are_used(db, billing, ctx, respx_mock):
    executor = Executor(db, billing, KEY, limits=IdentityLimits(max_failures=1))
    respx_mock.post(VERIFY_URL).mock(
        return_value=httpx.Response(200, json={"verified": False})
    )
    first, _ = await executor.run("authenticate_customer", AUTH_ARGS, ctx)
    second, _ = await executor.run("authenticate_customer", AUTH_ARGS, ctx)
    assert first == ToolError(code="authentication_failed")
    assert second == ToolError(code="locked")


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------
async def test_logs_contain_no_arguments_or_data(executor, ctx, respx_mock):
    stream = io.StringIO()
    handler = uas_logging.configure(stream)
    try:
        respx_mock.post(VERIFY_URL).mock(return_value=_verify_ok())
        await executor.run("authenticate_customer", AUTH_ARGS, ctx)
        respx_mock.post(VERIFY_URL).mock(
            return_value=httpx.Response(200, json={"verified": False})
        )
        await executor.run("authenticate_customer", AUTH_ARGS, ctx)
    finally:
        logging.getLogger().removeHandler(handler)
    out = stream.getvalue()
    tool_lines = [
        json.loads(line) for line in out.splitlines() if "support_api.tools" in line
    ]
    assert tool_lines and all(set(l) <= uas_logging.ALLOWED_KEYS for l in tool_lines)
    assert {l["tool_name"] for l in tool_lines} == {"authenticate_customer"}
    for line in map(json.loads, out.splitlines()):
        (
            line.pop("ts"),
            line.pop("duration_ms", None),
        )  # numbers there could match by chance
        for secret in (CPF_DIGITS, CPF, "1980", "4321", "Ana", "cus_a"):
            assert secret not in json.dumps(line)


async def test_unknown_tool_name_is_replaced_everywhere(executor, ctx, respx_mock, db):
    """M1: a model-supplied unknown tool name never reaches trace, DB row or log."""
    hostile = f"lookup_{CPF}_{CPF_DIGITS}"
    stream = io.StringIO()
    handler = uas_logging.configure(stream)
    try:
        result, trace = await executor.run(hostile, "{}", ctx)
    finally:
        logging.getLogger().removeHandler(handler)
    assert result == ToolError(code="invalid_arguments")
    assert trace.name == "unknown"
    assert (trace.status, trace.error_code) == ("error", "invalid_arguments")
    rows = await _tool_rows(db, ctx.claim)
    assert [r.name for r in rows] == ["unknown"]
    tool_lines = [
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if "support_api.tools" in line
    ]
    assert [line["tool_name"] for line in tool_lines] == ["unknown"]
    for blob in (json.dumps(asdict(trace)), stream.getvalue(), repr(rows)):
        assert CPF not in blob and CPF_DIGITS not in blob and "lookup_" not in blob
    assert respx_mock.calls.call_count == 0
