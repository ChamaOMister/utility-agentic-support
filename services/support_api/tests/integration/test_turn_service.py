"""Turn submission (spec §5.5): duplicates, serialization, text length, turn limit, transcript."""

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text as sql_text
from support_api.agent.core import TurnOutcome
from support_api.agent.models.base import ModelResponse
from support_api.agent.models.scripted import ASK_VERIFICATION, GREETING
from support_api.app import create_app
from support_api.catalog import MESSAGES
from support_api.turns.claim import fenced
from support_api.turns.service import EMPTY_TRACE, _complete
from support_api.usage.pricing import LiveGateClosed

from tests.conftest import (
    ORIGIN,
    conversation_row,
    items_for,
    lock_conversation,
    new_client,
    persona_creds_text,
    turn_id,
    turns,
)

IN_PROGRESS = {"error": "turn_in_progress", "message": MESSAGES["turn_in_progress"]}


async def test_duplicate_semantics(api):  # AC 11
    mid = uuid4()
    first = await api.send(mid, "olá")
    assert first.status_code == 200
    assert set(first.json()) == {"turn_id", "status", "reply", "trace"}
    assert first.json()["status"] == "completed" and first.json()["reply"] == GREETING
    again = await api.send(mid, "olá")
    assert again.status_code == 200
    assert again.json() == first.json() and api.model.requests_count == 1
    conflict = await api.send(mid, "outra")
    assert conflict.status_code == 422
    assert conflict.json() == {"error": "client_message_id_conflict"}
    assert api.model.requests_count == 1


async def test_concurrent_identical_submissions_one_turn(api, db):
    mid = uuid4()
    results = await asyncio.wait_for(
        asyncio.gather(*(api.send(mid, "olá") for _ in range(5))), 10
    )
    rows = await turns(db, api.conversation_id)
    assert len(rows) == 1 and api.model.requests_count == 1
    stored = {"turn_id": str(rows[0].id), "status": "completed", "reply": GREETING}
    for r in results:
        if r.status_code == 409:
            assert r.json() == IN_PROGRESS
        else:
            assert r.status_code == 200
            assert {k: r.json()[k] for k in stored} == stored


async def test_concurrent_different_messages_one_409(api, slow_model):
    a = asyncio.create_task(api.send(uuid4(), "olá"))
    b = asyncio.create_task(api.send(uuid4(), "bom dia"))
    await asyncio.wait_for(slow_model.entered.wait(), 10)
    done, _ = await asyncio.wait_for(
        asyncio.wait({a, b}, return_when=asyncio.FIRST_COMPLETED), 10
    )
    loser = done.pop().result()
    assert loser.status_code == 409 and loser.json() == IN_PROGRESS
    slow_model.release()
    results = await asyncio.wait_for(asyncio.gather(a, b), 10)
    assert sorted(r.status_code for r in results) == [200, 409]
    assert api.model.requests_count == 1


async def test_text_length_code_points(api):  # Review Focus 4
    assert (await api.send(uuid4(), "ç" * 2000)).status_code == 200
    assert (
        await api.send(uuid4(), "😀" * 2000)
    ).status_code == 200  # 8,000 UTF-8 bytes
    for bad in ("😀" * 2001, "ç" * 2001, ""):
        r = await api.send(uuid4(), bad)
        assert r.status_code == 422 and r.json() == {"error": "text_length"}
    assert api.model.requests_count == 2


async def test_invalid_body_is_422(api):
    path = f"/api/conversations/{api.conversation_id}/messages"
    for body in (
        {"client_message_id": "not-a-uuid", "text": "olá"},
        {"text": "olá"},
        {"client_message_id": str(uuid4())},
        {"client_message_id": str(uuid4()), "text": 5},
        ["olá"],
    ):
        r = await api.http.post(path, json=body, headers=ORIGIN)
        assert r.status_code == 422, body
    r = await api.http.post(
        path,
        content="{not json",
        headers={**ORIGIN, "Content-Type": "application/json"},
    )
    assert r.status_code == 422
    assert api.model.requests_count == 0


async def test_lone_surrogate_text_is_422(api, db):
    """JSON "\\ud800" decodes to a lone surrogate: not UTF-8 encodable, so no turn is created."""
    path = f"/api/conversations/{api.conversation_id}/messages"
    for escaped in ("\\ud800", "a\\udfffb"):
        raw = f'{{"client_message_id": "{uuid4()}", "text": "{escaped}"}}'
        r = await api.http.post(
            path, content=raw, headers={**ORIGIN, "Content-Type": "application/json"}
        )
        assert r.status_code == 422 and r.json() == {"error": "invalid_request"}, raw
    assert await turns(db, api.conversation_id) == []
    assert api.model.requests_count == 0


async def test_nul_text_is_422(api, db):
    """M2: NUL cannot be stored in jsonb or text: rejected as invalid, never a 500."""
    path = f"/api/conversations/{api.conversation_id}/messages"
    for escaped in ("\\u0000", "ol\\u0000á"):
        raw = f'{{"client_message_id": "{uuid4()}", "text": "{escaped}"}}'
        r = await api.http.post(
            path, content=raw, headers={**ORIGIN, "Content-Type": "application/json"}
        )
        assert r.status_code == 422 and r.json() == {"error": "invalid_request"}, raw
    assert await turns(db, api.conversation_id) == []
    assert api.model.requests_count == 0
    # The conversation is unaffected: a normal message still completes.
    assert (await api.send(uuid4(), "olá")).json()["status"] == "completed"


def _final(reply: str, *extra: dict) -> ModelResponse:
    msg = {"type": "message", "role": "assistant", "content": reply}
    return ModelResponse("completed", reply, (), (*extra, msg))


async def test_model_reply_with_nul_completes_without_nul(api_fake_model, db):
    """M2: a model reply or reasoning item carrying NUL never 500s or strands the turn."""
    api = api_fake_model
    nul_reasoning = {
        "type": "reasoning",
        "id": "r_nul",
        "summary": [{"type": "summary_text", "text": "a\x00b"}],
    }
    api.model.push(_final("Ol\x00á!", nul_reasoning))
    r = await api.send(uuid4(), "olá")
    assert r.status_code == 200
    assert r.json()["status"] == "completed" and r.json()["reply"] == "Olá!"
    (row,) = await turns(db, api.conversation_id)
    assert (row.status, row.reply) == ("completed", "Olá!")
    # The NUL-bearing reasoning item is not stored (jsonb cannot hold it).
    assert await items_for(db, row.id) == ["user_message", "assistant_message"]


async def test_model_reply_of_only_nul_is_limit_reached(api_fake_model, db):
    api = api_fake_model
    api.model.push(_final("\x00\x00"))
    r = await api.send(uuid4(), "olá")
    assert r.status_code == 200
    assert r.json()["status"] == "failed"
    assert r.json()["reply"] == MESSAGES["limit_reached"]
    (row,) = await turns(db, api.conversation_id)
    assert row.status == "failed"


async def test_complete_stores_only_reasoning_final_items(db, claimed_conversation):
    """M3: _complete stores reasoning, never a stray function_call/function_call_output."""
    claim = claimed_conversation
    final_items = (
        {"type": "reasoning", "id": "r1", "summary": [], "encrypted_content": "enc"},
        {"type": "function_call_output", "call_id": "ghost", "output": "{}"},
        {"type": "function_call", "call_id": "ghost2", "name": "x", "arguments": "{}"},
    )
    outcome = TurnOutcome("completed", "Certo!", EMPTY_TRACE, final_items)
    async with fenced(db, claim) as conn:
        await _complete(conn, claim, outcome, "queued-fake")
    assert await items_for(db, claim.turn_id) == ["reasoning", "assistant_message"]


async def test_stray_tool_output_does_not_poison_next_turn(api_fake_model, db):
    """M3 end to end: the next turn's history check passes and the turn completes."""
    api = api_fake_model
    stray = {"type": "function_call_output", "call_id": "ghost", "output": "{}"}
    api.model.push(_final("Primeira.", stray), _final("Segunda."))
    first = await api.send(uuid4(), "olá")
    second = await api.send(uuid4(), "e agora?")
    assert first.json()["status"] == second.json()["status"] == "completed"
    assert second.json()["reply"] == "Segunda."
    rows = await turns(db, api.conversation_id)
    assert await items_for(db, rows[0].id) == ["user_message", "assistant_message"]


async def test_turn_limit_30(api, db):
    for i in range(30):
        r = await api.send(uuid4(), f"olá {i}")
        assert r.status_code == 200 and r.json()["status"] == "completed"
    mid = uuid4()
    over = await api.send(mid, "olá de novo")
    assert over.status_code == 200
    assert over.json()["status"] == "failed"
    assert over.json()["reply"] == MESSAGES["limit_reached"]
    assert api.model.requests_count == 30  # the 31st turn never reaches the model
    assert await items_for(db, turn_id(over)) == ["user_message", "assistant_message"]
    rows = await turns(db, api.conversation_id)
    assert len(rows) == 31 and rows[-1].status == "failed"
    # Stored like any other turn: a duplicate replays it.
    assert (await api.send(mid, "olá de novo")).json() == over.json()


async def test_exactly_one_assistant_message_per_turn(api, db, personas):
    for text in ["my bills", persona_creds_text(personas[0]), "and the paid ones?"]:
        r = await api.send(uuid4(), text)
        assert r.status_code == 200 and r.json()["status"] == "completed"
    assert api.billing_routes["verify"].call_count == 1
    assert api.billing_routes["list_bills"].call_count == 1
    rows = await turns(db, api.conversation_id)
    assert len(rows) == 3
    for turn in rows:
        assert (await items_for(db, turn.id)).count("assistant_message") == 1
    tool_turn = await items_for(db, rows[1].id)
    assert tool_turn[0] == "user_message" and tool_turn[-1] == "assistant_message"
    assert (
        tool_turn.count("function_call") == tool_turn.count("function_call_output") == 2
    )


async def test_completed_turn_state(api, db):
    r = await api.send(uuid4(), "my bills")
    assert r.json()["reply"] == ASK_VERIFICATION
    (row,) = await turns(db, api.conversation_id)
    assert row.status == "completed" and row.reply == ASK_VERIFICATION
    assert row.finished_at is not None and row.correlation_id
    assert row.trace == r.json()["trace"]
    assert set(row.trace) == {"tool_calls", "model_calls"}
    conv = await conversation_row(db, api.conversation_id)
    assert conv.turn_count == 1
    assert conv.active_turn_id is None and conv.active_turn_lease_expires_at is None


async def test_transcript_lists_turns_without_payloads(api, personas):
    first = await api.send(m1 := uuid4(), "my bills")
    second = await api.send(m2 := uuid4(), persona_creds_text(personas[0]))
    got = await api.transcript()
    assert got.status_code == 200
    listed = got.json()["turns"]
    assert [t["turn_id"] for t in listed] == [
        first.json()["turn_id"],
        second.json()["turn_id"],
    ]
    assert [UUID(t["client_message_id"]) for t in listed] == [m1, m2]
    for t, sent in zip(listed, (first, second), strict=True):
        assert set(t) == {
            "turn_id", "client_message_id", "text", "status", "reply", "trace",
            "created_at", "finished_at", "correlation_id",
        }  # fmt: skip
        assert t["reply"] == sent.json()["reply"] and t["trace"] == sent.json()["trace"]
        assert t["correlation_id"] == sent.headers["X-Correlation-ID"]
    assert [t["text"] for t in listed] == [
        "my bills",
        persona_creds_text(personas[0]),
    ]
    # Trace is metadata only: no tool arguments or outputs (no CPF, no customer data).
    raw = got.text
    assert personas[0]["cpf"] not in raw and personas[0]["customer_ref"] not in raw


def test_openai_provider_without_live_approval_fails_at_startup(settings):
    with pytest.raises(LiveGateClosed, match="live_disabled"):
        create_app(settings.model_copy(update={"llm_provider": "openai"}))


async def test_other_session_cannot_post(api):
    api.app.state.test_clients = []
    other = new_client(api.app)
    try:
        r = await other.post(
            f"/api/conversations/{api.conversation_id}/messages",
            json={"client_message_id": str(uuid4()), "text": "olá"},
            headers=ORIGIN,
        )
        assert r.status_code == 404 and r.json() == {"error": "not_found"}
    finally:
        await other.aclose()
    assert api.model.requests_count == 0


async def test_lease_is_measured_from_the_claim_not_transaction_start(
    api, slow_model, db
):
    """The 60 s lease starts at the claim, even when step 1 waited on the row lock."""
    async with db.begin() as conn:
        await lock_conversation(conn, api.conversation_id)
        task = asyncio.create_task(api.send(uuid4(), "olá"))
        await asyncio.sleep(
            1.0
        )  # the submission's step-1 transaction waits on the row lock
        assert not task.done()
        released_at = (
            await conn.execute(sql_text("SELECT clock_timestamp()"))
        ).scalar_one()
    try:
        await asyncio.wait_for(slow_model.entered.wait(), 10)
        row = await conversation_row(db, api.conversation_id)
    finally:
        slow_model.release()
    assert (await asyncio.wait_for(task, 10)).status_code == 200
    lease_s = (row.active_turn_lease_expires_at - released_at).total_seconds()
    assert lease_s >= 60 - 0.05, lease_s
