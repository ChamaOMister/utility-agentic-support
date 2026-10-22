"""Crash recovery, fencing of stale workers and late verification outcomes (spec §5.5, §5.2)."""

import asyncio
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from support_api.catalog import MESSAGES
from support_api.checkpoints import SimulatedCrash
from support_api.identity.attempts import record_outcome
from support_api.turns.claim import Claim
from support_api.turns.recovery import recover_conversation, startup_sweep

from tests.conftest import (
    conv_auth,
    conversation_row,
    expire_lease,
    fake_call,
    fake_text,
    identifier_row,
    item_turn_ids,
    items_for,
    last_attempt,
    llm_call,
    lock_conversation,
    persona_hash,
    reserve_in_fenced,
    turn_by_mid,
    turn_id,
)


def _wrong_auth_args(persona: dict) -> dict:
    return {
        "identifier": persona["cpf"],
        "birth_date": "1970-01-01",
        "phone_last4": "0000",
    }


def _auth_args(persona: dict) -> dict:
    return {
        "identifier": persona["cpf"],
        "birth_date": persona["birth_date"],
        "phone_last4": persona["phone"][-4:],
    }


async def _active_claim(db, conversation_id) -> Claim:
    row = await conversation_row(db, conversation_id)
    assert row.active_turn_id is not None
    return Claim(conversation_id, row.active_turn_id, row.claim_version)


# --------------------------------------------------------------------------- AC 12 (Task 18)


async def test_crash_after_send(api_live_mocked, db, crash_at):  # AC 12
    from tests.conftest import live_blocked_reason, only_llm_call  # Task 18 helpers

    crash_at("after_send")
    mid = uuid4()
    with pytest.raises(SimulatedCrash):
        await api_live_mocked.send(mid, "minhas faturas")
    assert api_live_mocked.openai_routes["responses"].call_count == 1
    await expire_lease(db, api_live_mocked.conversation_id)
    replay = await api_live_mocked.send(mid, "minhas faturas")
    assert replay.status_code == 200 and replay.json()["status"] == "interrupted"
    assert replay.json()["reply"] == MESSAGES["turn_interrupted"]
    call = await only_llm_call(db, api_live_mocked.conversation_id, kind="generation")
    assert call.status == "uncertain" and call.reserved_usd > 0
    assert (
        await live_blocked_reason(db, api_live_mocked.conversation_id)
        == "interrupted_after_possible_send"
    )
    nxt = await api_live_mocked.send(uuid4(), "e agora?")
    assert nxt.json()["reply"] == MESSAGES["live_blocked"]
    assert api_live_mocked.openai_routes["responses"].call_count == 1


async def test_startup_sweep_recovers_without_request(api_live_mocked, db, crash_at):
    from tests.conftest import live_blocked_reason, only_llm_call  # Task 18 helpers

    crash_at("after_send")
    mid = uuid4()
    with pytest.raises(SimulatedCrash):
        await api_live_mocked.send(mid, "minhas faturas")
    await expire_lease(db, api_live_mocked.conversation_id)
    assert await startup_sweep(db) >= 1
    turn = await turn_by_mid(db, mid)
    assert turn.status == "interrupted" and turn.reply == MESSAGES["turn_interrupted"]
    call = await only_llm_call(db, api_live_mocked.conversation_id, kind="generation")
    assert call.status == "uncertain" and call.reserved_usd > 0
    assert (
        await live_blocked_reason(db, api_live_mocked.conversation_id)
        == "interrupted_after_possible_send"
    )


# --------------------------------------------------------------------------- crash + replay


async def test_crashed_turn_in_progress_then_recovered_on_duplicate(api, db, crash_at):
    crash_at("after_model_response")
    mid = uuid4()
    with pytest.raises(SimulatedCrash):
        await api.send(mid, "olá")
    # Unexpired lease: the same message is still in progress; a different one is serialized.
    busy = await api.send(mid, "olá")
    assert busy.status_code == 409 and busy.json()["error"] == "turn_in_progress"
    assert (await api.send(uuid4(), "outra")).status_code == 409

    await expire_lease(db, api.conversation_id)
    replay = await api.send(mid, "olá")
    assert replay.status_code == 200
    assert replay.json()["status"] == "interrupted"
    assert replay.json()["reply"] == MESSAGES["turn_interrupted"]
    assert await items_for(db, turn_id(replay)) == ["user_message", "assistant_message"]
    conv = await conversation_row(db, api.conversation_id)
    assert conv.active_turn_id is None and conv.live_blocked_reason is None
    # Final for that client_message_id; a new id proceeds normally.
    assert (await api.send(mid, "olá")).json() == replay.json()
    nxt = await api.send(uuid4(), "olá de novo")
    assert nxt.status_code == 200 and nxt.json()["status"] == "completed"


async def test_recovery_marks_reserved_calls_uncertain(api, db, crash_at):
    crash_at("after_model_response")
    mid = uuid4()
    with pytest.raises(SimulatedCrash):
        await api.send(mid, "olá")
    claim = await _active_claim(db, api.conversation_id)
    call_id = await reserve_in_fenced(db, claim, Decimal("0.01"))
    await expire_lease(db, api.conversation_id)

    replay = await api.send(mid, "olá")
    assert replay.json()["status"] == "interrupted"
    call = await llm_call(db, call_id)
    assert call.status == "uncertain" and call.reserved_usd == Decimal("0.01")
    conv = await conversation_row(db, api.conversation_id)
    assert conv.live_blocked_reason == "interrupted_after_possible_send"
    assert conv.claim_version > claim.claim_version


async def test_transcript_read_recovers(api, db, crash_at):
    crash_at("after_model_response")
    mid = uuid4()
    with pytest.raises(SimulatedCrash):
        await api.send(mid, "olá")
    before = (await api.transcript()).json()["turns"]
    assert [t["status"] for t in before] == ["in_progress"]
    await expire_lease(db, api.conversation_id)
    after = (await api.transcript()).json()["turns"]
    assert [t["status"] for t in after] == ["interrupted"]
    assert after[0]["reply"] == MESSAGES["turn_interrupted"]
    assert after[0]["finished_at"] is not None


async def test_startup_sweep_recovers_scripted_crash(api, db, crash_at):
    crash_at("after_model_response")
    mid = uuid4()
    with pytest.raises(SimulatedCrash):
        await api.send(mid, "olá")
    await expire_lease(db, api.conversation_id)
    assert await startup_sweep(db) >= 1
    turn = await turn_by_mid(db, mid)
    assert turn.status == "interrupted" and turn.reply == MESSAGES["turn_interrupted"]
    assert await items_for(db, turn.id) == ["user_message", "assistant_message"]
    assert (await conversation_row(db, api.conversation_id)).active_turn_id is None
    # Nothing left to sweep for this conversation; a second sweep leaves it alone.
    await startup_sweep(db)
    assert await items_for(db, turn.id) == ["user_message", "assistant_message"]


async def test_lifespan_startup_runs_the_sweep(api, db, crash_at):
    crash_at("after_model_response")
    mid = uuid4()
    with pytest.raises(SimulatedCrash):
        await api.send(mid, "olá")
    await expire_lease(db, api.conversation_id)
    async with api.app.router.lifespan_context(api.app):
        assert (await turn_by_mid(db, mid)).status == "interrupted"


async def test_recover_conversation_requires_expired_lease(api, db, crash_at):
    crash_at("after_model_response")
    with pytest.raises(SimulatedCrash):
        await api.send(uuid4(), "olá")
    before = await conversation_row(db, api.conversation_id)
    async with db.begin() as conn:
        await lock_conversation(conn, api.conversation_id)
        assert await recover_conversation(conn, api.conversation_id) is False
    assert await conversation_row(db, api.conversation_id) == before
    await expire_lease(db, api.conversation_id)
    async with db.begin() as conn:
        await lock_conversation(conn, api.conversation_id)
        assert await recover_conversation(conn, api.conversation_id) is True
        assert await recover_conversation(conn, api.conversation_id) is False


# --------------------------------------------------------------------------- AC 13


async def test_stale_worker_cannot_write(
    api_verified_fake_model, db, pause_at
):  # AC 13
    api = api_verified_fake_model
    # Turn A's first response is a list_bills call; turn B's response is plain text.
    api.model.push(fake_call("call_a", "list_bills"), fake_text("Resposta de B"))
    gate = pause_at("after_model_response", nth=1)
    a = asyncio.create_task(api.send(uuid4(), "minhas faturas"))
    await asyncio.wait_for(gate.reached.wait(), 10)
    await expire_lease(db, api.conversation_id)
    b = await api.send(uuid4(), "outra pergunta")  # B recovers A's turn and claims
    gate.release()
    a_result = await asyncio.wait_for(a, 10)
    assert a_result.json()["status"] == "interrupted"
    assert a_result.json()["reply"] == MESSAGES["turn_interrupted"]
    assert b.json()["status"] == "completed" and b.json()["reply"] == "Resposta de B"
    assert (
        api.billing_routes["list_bills"].call_count == 0
    )  # A's fenced persist failed first
    # A sent exactly one generation request (before the takeover); B sent one.
    assert len(api.model.requests) == 2
    assert await items_for(db, turn_id(a_result)) == [
        "user_message",
        "assistant_message",
    ]
    assert await items_for(db, turn_id(b)) == ["user_message", "assistant_message"]
    assert await item_turn_ids(db, api.conversation_id) == {
        turn_id(a_result),
        turn_id(b),
    }


async def test_stale_worker_cannot_complete_after_final_response(
    api_verified_fake_model, db, pause_at
):
    """Takeover after the final response: the fenced completion (step 6) writes nothing."""
    api = api_verified_fake_model
    api.model.push(fake_text("Resposta atrasada de A"))
    gate = pause_at("after_model_response", nth=1)
    a = asyncio.create_task(api.send(mid_a := uuid4(), "olá"))
    await asyncio.wait_for(gate.reached.wait(), 10)
    await expire_lease(db, api.conversation_id)
    async with db.begin() as conn:
        await lock_conversation(conn, api.conversation_id)
        assert await recover_conversation(conn, api.conversation_id) is True
    gate.release()
    r = await asyncio.wait_for(a, 10)
    assert r.json()["status"] == "interrupted"
    assert r.json()["reply"] == MESSAGES["turn_interrupted"]
    turn = await turn_by_mid(db, mid_a)
    assert turn.status == "interrupted" and turn.reply == MESSAGES["turn_interrupted"]
    assert await items_for(db, turn.id) == ["user_message", "assistant_message"]


# --------------------------------------------------------------------------- AC 14


async def _two_failures_then_held_third(api, db, personas, billing_gate, response=None):
    """Two completed turns with wrong answers, then a third whose verify is held while the test
    recovers the turn. Returns the third message's client_message_id."""
    a = personas[0]
    for n in (1, 2):
        api.model.push(
            fake_call(f"auth_{n}", "authenticate_customer", _wrong_auth_args(a)),
            fake_text("Não foi possível confirmar."),
        )
        r = await api.send(uuid4(), "verificar")
        assert r.json()["status"] == "completed"
    assert (await conv_auth(db, api.conversation_id)).failed_attempts == 2

    api.model.push(fake_call("auth_3", "authenticate_customer", _wrong_auth_args(a)))
    billing_gate.hold(response)
    mid_a = uuid4()
    task = asyncio.create_task(api.send(mid_a, "verificar"))
    await asyncio.wait_for(billing_gate.request_seen.wait(), 10)
    await expire_lease(db, api.conversation_id)
    async with db.begin() as conn:
        await lock_conversation(conn, api.conversation_id)
        assert await recover_conversation(conn, api.conversation_id) is True
    assert (await last_attempt(db, api.conversation_id)).status == "abandoned"
    billing_gate.release()
    r = await asyncio.wait_for(task, 10)
    assert r.status_code == 200 and r.json()["status"] == "interrupted"
    return mid_a


async def test_late_verification_failure_after_takeover(
    api_fake_model, db, billing_gate, personas
):
    api = api_fake_model
    mid_a = await _two_failures_then_held_third(api, db, personas, billing_gate)

    attempt = await last_attempt(db, api.conversation_id)
    assert attempt.status == "failed"
    ident = await identifier_row(db, persona_hash(personas[0]))
    auth = await conv_auth(db, api.conversation_id)
    assert ident.failed_count == 3 and ident.locked_until is not None
    assert auth.failed_attempts == 3 and auth.locked_until is not None
    assert auth.assurance_level == "none"
    turn = await turn_by_mid(db, mid_a)
    assert turn.status == "interrupted"
    # The function_call was stored by AgentCore before the tool ran (Task 16 transcript
    # ownership); the stale worker wrote nothing after the takeover: no output, no tool row.
    assert await items_for(db, turn.id) == [
        "user_message",
        "function_call",
        "assistant_message",
    ]
    async with db.begin() as conn:
        tool_rows = (
            await conn.execute(
                text("SELECT count(*) FROM support.tool_calls WHERE turn_id = :t"),
                {"t": turn.id},
            )
        ).scalar_one()
    assert tool_rows == 0

    assert await record_outcome(db, attempt.id, "failed") is False  # idempotent replay
    assert (await identifier_row(db, persona_hash(personas[0]))).failed_count == 3


async def test_late_verification_success_after_takeover(
    api_fake_model, db, billing_gate, personas
):
    api = api_fake_model
    a = personas[0]
    verified = {
        "verified": True,
        "customer_ref": a["customer_ref"],
        "first_name": a["first_name"],
    }
    mid_a = await _two_failures_then_held_third(
        api, db, personas, billing_gate, response=verified
    )

    assert (await last_attempt(db, api.conversation_id)).status == "verified"
    auth = await conv_auth(db, api.conversation_id)
    assert auth.assurance_level == "none" and auth.customer_ref is None
    assert auth.failed_attempts == 2
    turn = await turn_by_mid(db, mid_a)
    assert turn.status == "interrupted"
    assert (await items_for(db, turn.id)).count("assistant_message") == 1


async def test_takeover_during_after_verify_admit_sends_no_verify(
    api_fake_model, db, pause_at, personas
):
    api = api_fake_model
    api.model.push(
        fake_call("auth_1", "authenticate_customer", _auth_args(personas[0]))
    )
    gate = pause_at("after_verify_admit", nth=1)
    a = asyncio.create_task(api.send(mid_a := uuid4(), "verificar"))
    await asyncio.wait_for(gate.reached.wait(), 10)
    await expire_lease(db, api.conversation_id)
    async with db.begin() as conn:
        await lock_conversation(conn, api.conversation_id)
        assert await recover_conversation(conn, api.conversation_id) is True
    gate.release()
    r = await asyncio.wait_for(a, 10)
    assert r.json()["status"] == "interrupted"
    assert api.billing_routes["verify"].call_count == 0
    attempt = await last_attempt(db, api.conversation_id)
    assert attempt.status == "abandoned"
    assert (await identifier_row(db, persona_hash(personas[0]))).failed_count == 0
    assert (await conv_auth(db, api.conversation_id)).failed_attempts == 0
    assert await items_for(db, (await turn_by_mid(db, mid_a)).id) == [
        "user_message",
        "function_call",
        "assistant_message",
    ]
