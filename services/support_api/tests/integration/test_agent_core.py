"""AgentCore bounded loop (spec §5.4, §5.4.1): limits, deadline, transcript ownership, fencing."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date

import httpx
import pytest
import respx
from sqlalchemy import text
from support_api.agent.core import AgentCore, TurnOutcome
from support_api.agent.models.base import (
    FunctionCall,
    ModelContext,
    ModelRequest,
    ModelResponse,
    ModelUnavailable,
)
from support_api.agent.models.fake import QueuedFakeModel
from support_api.agent.models.scripted import ScriptedModel
from support_api.catalog import MESSAGES
from support_api.deadline import Deadline
from support_api.integrations.billing.client import BillingClient
from support_api.tools.executor import Executor

from tests.conftest import expire_verification

KEY = b"test-hash-key"
BILLING = "http://billing.test"
BILLS_PATH = "/v1/customers/cus_a/bills"


# --------------------------------------------------------------------------- response builders


def call(call_id: str, name: str = "list_bills", args: dict | None = None):
    return FunctionCall(call_id, name, json.dumps(args or {}))


def calls_response(*calls: FunctionCall, extra: tuple[dict, ...] = ()) -> ModelResponse:
    items = tuple(
        {
            "type": "function_call",
            "call_id": c.call_id,
            "name": c.name,
            "arguments": c.arguments,
        }
        for c in calls
    )
    return ModelResponse("completed", None, tuple(calls), extra + items)


def text_response(reply: str, extra: tuple[dict, ...] = ()) -> ModelResponse:
    msg = {"type": "message", "role": "assistant", "content": reply}
    return ModelResponse("completed", reply, (), extra + (msg,))


def reasoning(rid: str) -> dict:
    return {"type": "reasoning", "id": rid, "summary": [], "encrypted_content": "enc"}


# --------------------------------------------------------------------------- DB helpers


async def start_turn(db, claim, user_text: str) -> None:
    """What the turn service (Task 17) writes before calling AgentCore: the user item, seq 0."""
    async with db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO support.conversation_items "
                "(conversation_id, turn_id, seq, kind, payload) "
                "VALUES (:c, :t, 0, 'user_message', CAST(:p AS jsonb))"
            ),
            {
                "c": claim.conversation_id,
                "t": claim.turn_id,
                "p": json.dumps(
                    {"type": "message", "role": "user", "content": user_text}
                ),
            },
        )


async def item_rows(db, turn_id):
    async with db.begin() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT seq, kind, call_id, model, payload FROM support.conversation_items "
                    "WHERE turn_id = :t ORDER BY seq"
                ),
                {"t": turn_id},
            )
        ).all()


async def items_for(db, turn_id) -> list[str]:
    return [r.kind for r in await item_rows(db, turn_id)]


async def past_completed_turn(
    db, conversation_id, items: list[tuple[str, str | None, dict]]
):
    """A finished turn created before the current one (created_at in the past)."""
    turn_id = uuid.uuid4()
    async with db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO support.turns (id, conversation_id, client_message_id, text_sha256, "
                "status, correlation_id, created_at) "
                "VALUES (:t, :c, :m, 'x', 'completed', 'corr', now() - interval '1 hour')"
            ),
            {"t": turn_id, "c": conversation_id, "m": uuid.uuid4()},
        )
        for seq, (kind, call_id, payload) in enumerate(items):
            await conn.execute(
                text(
                    "INSERT INTO support.conversation_items "
                    "(conversation_id, turn_id, seq, kind, call_id, payload) "
                    "VALUES (:c, :t, :q, :k, :i, CAST(:p AS jsonb))"
                ),
                {
                    "c": conversation_id,
                    "t": turn_id,
                    "q": seq,
                    "k": kind,
                    "i": call_id,
                    "p": json.dumps(payload),
                },
            )
    return turn_id


def msg(role: str, content: str) -> dict:
    return {"type": "message", "role": role, "content": content}


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def billing_mock():
    with respx.mock(base_url=BILLING, assert_all_called=False) as router:
        yield router


@pytest.fixture
async def billing_client(fake_clock):
    async def fake_sleep(seconds: float) -> None:
        fake_clock.advance(seconds)

    client = BillingClient(
        BILLING, "k", clock=fake_clock, sleep=fake_sleep, rng=lambda: 0.0
    )
    yield client
    await client.aclose()


@pytest.fixture
def make_core(db, settings, billing_client):
    def make(model, **setting_overrides) -> AgentCore:
        s = (
            settings.model_copy(update=setting_overrides)
            if setting_overrides
            else settings
        )
        executor = Executor(db, billing_client, KEY)
        return AgentCore(db, model, executor, s)

    return make


@pytest.fixture
async def verified_turn(db, verified_conversation):
    """``verified_conversation`` (customer_ref cus_a) with its user item written."""
    await start_turn(db, verified_conversation, "quais as minhas faturas em aberto?")
    return verified_conversation


def bills_ok(billing_mock):
    return billing_mock.get(BILLS_PATH).mock(return_value=httpx.Response(200, json=[]))


async def handle(core, claim, deadline=None, user_text="faturas") -> TurnOutcome:
    return await core.handle_turn(claim, user_text, "chat", deadline or Deadline(45))


# --------------------------------------------------------------------------- tests


async def test_tool_call_limit_ends_with_limit_message(
    make_core, db, verified_turn, billing_mock
):
    route = bills_ok(billing_mock)
    fake = QueuedFakeModel([calls_response(call(f"c{i}")) for i in range(5)])
    outcome = await handle(make_core(fake), verified_turn)
    assert outcome.status == "failed"
    assert outcome.reply == MESSAGES["limit_reached"]
    assert outcome.final_items == ()
    assert len(outcome.trace["tool_calls"]) == 4
    assert route.call_count == 4
    # The 5th call was never run and its response never stored: storage stays pair-complete.
    kinds = await items_for(db, verified_turn.turn_id)
    assert kinds.count("function_call") == kinds.count("function_call_output") == 4


async def test_model_call_limit(make_core, verified_turn, billing_mock):
    bills_ok(billing_mock)
    fake = QueuedFakeModel([calls_response(call(f"c{i}")) for i in range(6)])
    outcome = await handle(make_core(fake, max_tool_calls=10), verified_turn)
    assert len(fake.requests) == 5  # the 6th model call is never made
    assert outcome.status == "failed"
    assert outcome.reply == MESSAGES["limit_reached"]
    generations = [m for m in outcome.trace["model_calls"] if m["kind"] == "generation"]
    assert len(generations) == 5


class _SlowFake(QueuedFakeModel):
    """Each generation takes 10 s of fake time, but never more than the deadline allows."""

    def __init__(self, responses, clock):
        super().__init__(responses)
        self._clock = clock

    async def generate(self, request: ModelRequest, ctx: ModelContext) -> ModelResponse:
        self._clock.advance(min(10.0, ctx.deadline.remaining()))
        return await super().generate(request, ctx)


async def test_deadline_respected_on_slow_billing(
    make_core, verified_turn, billing_mock, fake_clock
):
    def hang(request: httpx.Request):
        # A server that never answers: each attempt burns its whole timeout.
        fake_clock.advance(request.extensions["timeout"]["read"])
        raise httpx.ReadTimeout("slow", request=request)

    billing_mock.get(BILLS_PATH).mock(side_effect=hang)
    fake = _SlowFake([calls_response(call(f"c{i}")) for i in range(4)], fake_clock)
    started = fake_clock.now
    outcome = await handle(
        make_core(fake), verified_turn, Deadline(45, clock=fake_clock)
    )
    assert fake_clock.now - started <= 45
    assert outcome.status == "failed"
    assert outcome.reply == MESSAGES["limit_reached"]
    assert [t["error_code"] for t in outcome.trace["tool_calls"]] == [
        "upstream_timeout",
        "upstream_timeout",
    ]


async def test_billing_errors_give_recoverable_message_no_fabrication(
    make_core, db, claimed_conversation, billing_mock, personas
):
    p = personas[0]
    birth = date.fromisoformat(p["birth_date"]).strftime("%d/%m/%Y")
    user_text = f"CPF {p['cpf']}, nasci em {birth}, telefone final {p['phone'][-4:]}"
    await start_turn(db, claimed_conversation, user_text)
    billing_mock.post("/v1/identity/verify").mock(return_value=httpx.Response(500))
    outcome = await handle(
        make_core(ScriptedModel()), claimed_conversation, user_text=user_text
    )
    assert outcome.reply == MESSAGES["upstream_unavailable"]
    assert [t["error_code"] for t in outcome.trace["tool_calls"]] == [
        "upstream_unavailable"
    ]
    assert "R$" not in outcome.reply


async def test_current_turn_too_large_sends_no_model_request(
    make_core, db, claimed_conversation
):
    huge = "a" * 40_000
    await start_turn(db, claimed_conversation, huge)
    fake = QueuedFakeModel([text_response("nunca")])
    outcome = await handle(make_core(fake), claimed_conversation, user_text=huge)
    assert fake.requests == [] and fake.count_requests == []
    assert outcome.status == "failed"
    assert outcome.reply == MESSAGES["limit_reached"]


async def test_output_items_persisted_in_order(
    make_core, db, verified_turn, billing_mock
):
    bills_ok(billing_mock)
    interim = msg("assistant", "Vou verificar...")
    fake = QueuedFakeModel(
        [
            calls_response(call("c1"), extra=(reasoning("r1"), interim)),
            text_response("Certo!", extra=(reasoning("r2"),)),
        ]
    )
    outcome = await handle(make_core(fake), verified_turn)
    assert outcome.status == "completed"
    assert outcome.reply == "Certo!"
    assert outcome.final_items == (reasoning("r2"),)
    rows = await item_rows(db, verified_turn.turn_id)
    assert [(r.seq, r.kind) for r in rows] == [
        (0, "user_message"),
        (1, "reasoning"),
        (2, "function_call"),
        (3, "function_call_output"),
    ]
    assert rows[1].payload == reasoning("r1") and rows[1].model == "queued-fake"
    assert rows[2].call_id == rows[3].call_id == "c1" and rows[2].model == "queued-fake"
    assert rows[3].payload == {
        "type": "function_call_output",
        "call_id": "c1",
        "output": json.dumps({"untrusted_data": [], "status": "ok"}),
    }
    assert rows[3].model is None
    # The follow-up request carried every current-turn item, interim message excluded.
    follow_up = fake.requests[1].input
    assert [i["type"] for i in follow_up] == [
        "message",
        "reasoning",
        "function_call",
        "function_call_output",
    ]
    assert interim not in follow_up


async def test_final_items_keep_only_reasoning(make_core, verified_turn):
    """M3: a stray tool item in a final response is not a final item (it would poison history)."""
    stray = {"type": "function_call_output", "call_id": "ghost", "output": "{}"}
    fake = QueuedFakeModel(
        [text_response("Certo!", extra=(reasoning("r1"), stray, msg("user", "x")))]
    )
    outcome = await handle(make_core(fake), verified_turn)
    assert outcome.status == "completed" and outcome.reply == "Certo!"
    assert outcome.final_items == (reasoning("r1"),)


async def test_core_never_writes_assistant_message(
    make_core, db, verified_turn, billing_mock
):
    bills_ok(billing_mock)
    fake = QueuedFakeModel([calls_response(call("c1")), text_response("Certo!")])
    outcome = await handle(make_core(fake), verified_turn)
    assert "assistant_message" not in await items_for(db, verified_turn.turn_id)
    assert outcome.reply == "Certo!"
    assert outcome.status == "completed"


async def test_expired_verification_uses_expired_message(
    make_core, db, verified_turn, billing_mock
):
    route = bills_ok(billing_mock)
    await expire_verification(db, verified_turn)
    fake = QueuedFakeModel(
        [calls_response(call("c1")), text_response("Por favor, informe seu CPF.")]
    )
    outcome = await handle(make_core(fake), verified_turn)
    assert outcome.status == "failed"
    assert outcome.reply == MESSAGES["verification_expired"]
    assert outcome.final_items == ()
    assert outcome.trace["tool_calls"][0]["error_code"] == "verification_expired"
    assert route.call_count == 0


async def test_reauthentication_in_same_turn_clears_expired_override(
    make_core, db, verified_turn, billing_mock
):
    """The override follows the LAST tool trace: re-verifying mid-turn lets the answer through."""
    route = bills_ok(billing_mock)
    billing_mock.post("/v1/identity/verify").mock(
        return_value=httpx.Response(
            200, json={"verified": True, "customer_ref": "cus_a", "first_name": "Ana"}
        )
    )
    await expire_verification(db, verified_turn)
    creds = {
        "identifier": "12345678909",
        "birth_date": "1980-02-01",
        "phone_last4": "4321",
    }
    fake = QueuedFakeModel(
        [
            calls_response(call("c1")),  # -> verification_expired
            calls_response(call("c2", "authenticate_customer", creds)),
            calls_response(call("c3")),  # -> ok
            text_response("Não encontrei faturas em aberto."),
        ]
    )
    outcome = await handle(make_core(fake), verified_turn)
    assert [t["error_code"] for t in outcome.trace["tool_calls"]] == [
        "verification_expired",
        None,
        None,
    ]
    assert route.call_count == 1
    assert outcome.status == "completed"
    assert outcome.reply == "Não encontrei faturas em aberto."


async def test_live_blocked_conversation_sends_nothing(make_core, db, verified_turn):
    async with db.begin() as conn:
        await conn.execute(
            text(
                "UPDATE support.conversations SET live_blocked_reason = "
                "'interrupted_after_possible_send' WHERE id = :c"
            ),
            {"c": verified_turn.conversation_id},
        )
    fake = QueuedFakeModel([text_response("nunca")], live=True)
    outcome = await handle(make_core(fake), verified_turn)
    assert outcome.status == "failed"
    assert outcome.reply == MESSAGES["live_blocked"]
    assert fake.requests == [] and fake.count_requests == []


async def test_live_block_ignored_for_scripted_models(make_core, db, verified_turn):
    async with db.begin() as conn:
        await conn.execute(
            text(
                "UPDATE support.conversations SET live_blocked_reason = 'x' WHERE id = :c"
            ),
            {"c": verified_turn.conversation_id},
        )
    fake = QueuedFakeModel([text_response("Olá!")])
    outcome = await handle(make_core(fake), verified_turn)
    assert outcome.status == "completed" and outcome.reply == "Olá!"


async def test_tool_pair_violation_sends_no_request(
    make_core, db, verified_turn, caplog
):
    # A completed past turn with a dangling call: assembly must refuse to send it.
    await past_completed_turn(
        db,
        verified_turn.conversation_id,
        [
            ("user_message", None, msg("user", "oi")),
            (
                "function_call",
                "old",
                {
                    "type": "function_call",
                    "call_id": "old",
                    "name": "list_bills",
                    "arguments": "{}",
                },
            ),
            ("assistant_message", None, msg("assistant", "ok")),
        ],
    )
    fake = QueuedFakeModel([text_response("nunca")])
    with caplog.at_level(logging.INFO, logger="support_api.agent"):
        outcome = await handle(make_core(fake), verified_turn)
    assert fake.requests == [] and fake.count_requests == []
    assert outcome.status == "failed"
    assert outcome.reply == MESSAGES["limit_reached"]
    assert any(
        getattr(r, "error_code", None) == "internal_error" for r in caplog.records
    )


async def test_duplicate_call_id_in_response_is_not_executed(
    make_core, db, verified_turn, billing_mock
):
    route = bills_ok(billing_mock)
    fake = QueuedFakeModel([calls_response(call("c1"), call("c1"))])
    outcome = await handle(make_core(fake), verified_turn)
    assert outcome.status == "failed" and outcome.reply == MESSAGES["limit_reached"]
    assert route.call_count == 0
    assert await items_for(db, verified_turn.turn_id) == ["user_message"]


async def test_tool_budget_checked_per_whole_response(
    make_core, db, verified_turn, billing_mock
):
    route = bills_ok(billing_mock)
    fake = QueuedFakeModel(
        [
            calls_response(call("c1"), call("c2"), call("c3")),
            calls_response(call("c4"), call("c5")),  # 3 + 2 > 4: not persisted, not run
        ]
    )
    outcome = await handle(make_core(fake), verified_turn)
    assert outcome.status == "failed" and outcome.reply == MESSAGES["limit_reached"]
    assert route.call_count == 3
    rows = await item_rows(db, verified_turn.turn_id)
    assert [r.call_id for r in rows if r.kind == "function_call"] == ["c1", "c2", "c3"]
    assert [r.call_id for r in rows if r.kind == "function_call_output"] == [
        "c1",
        "c2",
        "c3",
    ]


async def test_over_count_drops_oldest_turn_once_then_limits(
    make_core, db, verified_turn
):
    await past_completed_turn(
        db,
        verified_turn.conversation_id,
        [
            ("user_message", None, msg("user", "antigo")),
            ("assistant_message", None, msg("assistant", "ok")),
        ],
    )
    fake = QueuedFakeModel([text_response("nunca")], token_count=9000)
    outcome = await handle(make_core(fake), verified_turn)
    assert outcome.status == "failed" and outcome.reply == MESSAGES["limit_reached"]
    assert fake.requests == []
    assert len(fake.count_requests) == 2
    first, second = (r.input for r in fake.count_requests)
    assert msg("user", "antigo") in first and msg("user", "antigo") not in second


async def test_history_and_instructions_reach_the_model(make_core, db, verified_turn):
    await past_completed_turn(
        db,
        verified_turn.conversation_id,
        [
            ("user_message", None, msg("user", "antigo")),
            ("assistant_message", None, msg("assistant", "ok")),
        ],
    )
    fake = QueuedFakeModel([text_response("Olá!")])
    outcome = await handle(make_core(fake), verified_turn)
    assert outcome.status == "completed"
    request = fake.requests[0]
    assert "Vértice Energia" in request.instructions
    assert request.input[0] == msg("user", "antigo")
    assert request.input[-1] == msg("user", "quais as minhas faturas em aberto?")
    assert {t["name"] for t in request.tools} >= {"authenticate_customer", "list_bills"}
    kinds = [m["kind"] for m in outcome.trace["model_calls"]]
    assert kinds == ["token_count", "generation"]


async def test_model_unavailable_maps_to_catalog(make_core, verified_turn):
    fake = QueuedFakeModel([ModelUnavailable("upstream_unavailable")])
    outcome = await handle(make_core(fake), verified_turn)
    assert outcome.status == "failed"
    assert outcome.reply == MESSAGES["upstream_unavailable"]


async def test_unexpected_error_is_upstream_unavailable(
    make_core, verified_turn, caplog
):
    fake = QueuedFakeModel([RuntimeError("boom")])
    with caplog.at_level(logging.INFO, logger="support_api.agent"):
        outcome = await handle(make_core(fake), verified_turn)
    assert outcome.status == "failed"
    assert outcome.reply == MESSAGES["upstream_unavailable"]
    assert any(
        getattr(r, "error_code", None) == "internal_error" for r in caplog.records
    )


async def test_incomplete_final_response_is_not_shown(make_core, verified_turn):
    partial = ModelResponse(
        "incomplete", "Sua fatura é de R$", (), (msg("assistant", "x"),)
    )
    outcome = await handle(make_core(QueuedFakeModel([partial])), verified_turn)
    assert outcome.status == "failed" and outcome.reply == MESSAGES["limit_reached"]


async def test_lost_claim_propagates_before_any_tool_runs(
    make_core, db, verified_turn, billing_mock
):
    from support_api.turns.claim import ClaimLost

    from tests.conftest import take_over

    route = bills_ok(billing_mock)

    class _TakeOverFake(QueuedFakeModel):
        async def generate(self, request, ctx):
            response = await super().generate(request, ctx)
            await take_over(db, verified_turn)
            return response

    fake = _TakeOverFake([calls_response(call("c1"))])
    with pytest.raises(ClaimLost):
        await handle(make_core(fake), verified_turn)
    assert route.call_count == 0
    assert await items_for(db, verified_turn.turn_id) == ["user_message"]


async def test_model_trace_entries_not_duplicated_when_model_records(
    make_core, verified_turn
):
    class _Recording(QueuedFakeModel):
        async def count_input_tokens(self, request, ctx):
            ctx.trace.append(
                {"kind": "token_count", "model": "rich", "input_tokens": 1}
            )
            return await super().count_input_tokens(request, ctx)

    fake = _Recording([text_response("Olá!")])
    outcome = await handle(make_core(fake), verified_turn)
    assert outcome.trace["model_calls"] == [
        {"kind": "token_count", "model": "rich", "input_tokens": 1},
        {"kind": "generation", "model": "queued-fake", "status": "ok"},
    ]


async def test_expired_deadline_sends_no_model_request(
    make_core, verified_turn, fake_clock
):
    deadline = Deadline(45, clock=fake_clock)
    fake_clock.advance(45)
    fake = QueuedFakeModel([text_response("nunca")])
    outcome = await handle(make_core(fake), verified_turn, deadline)
    assert outcome.status == "failed" and outcome.reply == MESSAGES["limit_reached"]
    assert fake.requests == [] and fake.count_requests == []
