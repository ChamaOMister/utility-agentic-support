import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import text
from support_api.deadline import Deadline
from support_api.turns.claim import (
    Claim,
    ClaimLost,
    DeadlineExpired,
    assert_claim,
    fenced,
    pre_send,
    release,
    try_claim,
)

from tests.conftest import claim_in_tx, expire_lease, take_over


async def _row(db, conversation_id):
    async with db.begin() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT active_turn_id, claim_version, active_turn_lease_expires_at, "
                    "turn_count, now() AS db_now FROM support.conversations WHERE id = :c"
                ),
                {"c": conversation_id},
            )
        ).one()


async def test_first_claim_sets_active_turn_and_lease(db, conversation):
    turn = uuid4()
    claim = await claim_in_tx(db, conversation, turn)
    assert claim == Claim(conversation, turn, 1)
    row = await _row(db, conversation)
    assert row.active_turn_id == turn and row.claim_version == 1
    lease = (row.active_turn_lease_expires_at - row.db_now).total_seconds()
    assert 55 < lease <= 60


async def test_second_claim_while_lease_live_is_refused(db, conversation):
    first = await claim_in_tx(db, conversation, uuid4())
    assert await claim_in_tx(db, conversation, uuid4()) is None
    row = await _row(db, conversation)
    assert row.active_turn_id == first.turn_id and row.claim_version == 1


async def test_concurrent_claims_one_winner(db, conversation):
    turns = [uuid4() for _ in range(10)]
    results = await asyncio.gather(*(claim_in_tx(db, conversation, t) for t in turns))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    row = await _row(db, conversation)
    assert row.active_turn_id == winners[0].turn_id and row.claim_version == 1


async def test_claim_after_expired_lease_bumps_version(db, conversation):
    first = await claim_in_tx(db, conversation, uuid4())
    await expire_lease(db, conversation)
    second = await claim_in_tx(db, conversation, uuid4())
    assert second.claim_version == first.claim_version + 1
    row = await _row(db, conversation)
    assert row.active_turn_id == second.turn_id and row.claim_version == 2


async def test_try_claim_unknown_conversation_returns_none(db):
    assert await claim_in_tx(db, uuid4(), uuid4()) is None


async def test_fenced_write_commits_for_current_claim(db, conversation):
    claim = await claim_in_tx(db, conversation, uuid4())
    async with fenced(db, claim) as conn:
        await conn.execute(
            text("UPDATE support.conversations SET turn_count = 7 WHERE id = :c"),
            {"c": conversation},
        )
    assert (await _row(db, conversation)).turn_count == 7


async def test_fenced_write_fails_after_takeover(db, conversation):
    first = await claim_in_tx(db, conversation, uuid4())
    await expire_lease(db, conversation)
    await claim_in_tx(db, conversation, uuid4())
    with pytest.raises(ClaimLost):
        async with fenced(db, first) as conn:
            await conn.execute(
                text("UPDATE support.conversations SET turn_count = 99 WHERE id = :c"),
                {"c": conversation},
            )
    assert (await _row(db, conversation)).turn_count == 0


async def test_fenced_rolls_back_and_reraises_on_error(db, conversation):
    claim = await claim_in_tx(db, conversation, uuid4())
    with pytest.raises(RuntimeError, match="boom"):
        async with fenced(db, claim) as conn:
            await conn.execute(
                text("UPDATE support.conversations SET turn_count = 5 WHERE id = :c"),
                {"c": conversation},
            )
            raise RuntimeError("boom")
    assert (await _row(db, conversation)).turn_count == 0


async def test_fenced_holds_row_lock_against_takeover(db, conversation):
    """While a fenced block is open, a takeover (needs the row lock) cannot slip in."""
    first = await claim_in_tx(db, conversation, uuid4())
    await expire_lease(
        db, conversation
    )  # lease expired, but the worker still holds the claim
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def worker():
        async with fenced(db, first):
            entered.set()
            await proceed.wait()

    task = asyncio.create_task(worker())
    await entered.wait()
    takeover = asyncio.create_task(claim_in_tx(db, conversation, uuid4()))
    await asyncio.sleep(0.2)
    assert not takeover.done()  # blocked on the worker's FOR UPDATE lock
    proceed.set()
    await task
    assert (await takeover).claim_version == first.claim_version + 1


async def test_release_clears_active_turn_only(db, conversation):
    claim = await claim_in_tx(db, conversation, uuid4())
    async with fenced(db, claim) as conn:
        await release(conn, claim)
    row = await _row(db, conversation)
    assert row.active_turn_id is None and row.active_turn_lease_expires_at is None
    assert row.claim_version == claim.claim_version
    again = await claim_in_tx(db, conversation, uuid4())
    assert again.claim_version == claim.claim_version + 1


async def test_release_by_stale_claim_leaves_new_owner(db, conversation):
    first = await claim_in_tx(db, conversation, uuid4())
    second = await take_over(db, conversation)
    with pytest.raises(ClaimLost):
        async with fenced(db, first) as conn:
            await release(conn, first)
    row = await _row(db, conversation)
    assert row.active_turn_id == second.turn_id
    assert row.claim_version == second.claim_version


async def test_assert_claim_after_takeover(db, conversation):
    first = await claim_in_tx(db, conversation, uuid4())
    await assert_claim(db, first)  # no exception
    await expire_lease(db, conversation)
    await claim_in_tx(db, conversation, uuid4())
    with pytest.raises(ClaimLost):
        await assert_claim(db, first)


async def test_assert_claim_after_release(db, conversation):
    claim = await claim_in_tx(db, conversation, uuid4())
    async with fenced(db, claim) as conn:
        await release(conn, claim)
    with pytest.raises(ClaimLost):
        await assert_claim(db, claim)


async def test_pre_send_passes_within_deadline(db, conversation, fake_clock):
    claim = await claim_in_tx(db, conversation, uuid4())
    await pre_send(db, claim, Deadline(45, clock=fake_clock), _Counting())


async def test_pre_send_raises_claim_lost_before_checkpoint(
    db, conversation, fake_clock
):
    first = await claim_in_tx(db, conversation, uuid4())
    await take_over(db, conversation)
    checkpoints = _Counting()
    with pytest.raises(ClaimLost):
        await pre_send(db, first, Deadline(45, clock=fake_clock), checkpoints)
    assert checkpoints.names == []


async def test_pre_send_hits_checkpoint_once(db, conversation, fake_clock):
    claim = await claim_in_tx(db, conversation, uuid4())
    checkpoints = _Counting()
    await pre_send(db, claim, Deadline(45, clock=fake_clock), checkpoints)
    assert checkpoints.names == ["after_claim_check"]


async def test_pre_send_expired_deadline_raises(db, conversation, fake_clock):
    claim = await claim_in_tx(db, conversation, uuid4())
    deadline = Deadline(45, clock=fake_clock)
    fake_clock.advance(45)  # remaining() == 0 -> not > 0
    with pytest.raises(DeadlineExpired):
        await pre_send(db, claim, deadline, _Counting())


async def test_pre_send_local_gate_catches_stall_after_db_check(
    db, conversation, fake_clock, pause_at
):
    claim = await claim_in_tx(db, conversation, uuid4())
    deadline = Deadline(45, clock=fake_clock)
    gate = pause_at("after_claim_check")
    t = asyncio.create_task(pre_send(db, claim, deadline, gate.checkpoints))
    await gate.reached.wait()
    fake_clock.advance(46)  # stall past the deadline, after the DB check passed
    gate.release()
    with pytest.raises(DeadlineExpired):
        await t


async def test_pre_send_crash_checkpoint_propagates(
    db, conversation, fake_clock, crash_at
):
    from support_api.checkpoints import SimulatedCrash

    claim = await claim_in_tx(db, conversation, uuid4())
    with pytest.raises(SimulatedCrash):
        await pre_send(
            db, claim, Deadline(45, clock=fake_clock), crash_at("after_claim_check")
        )


async def test_try_claim_runs_in_callers_transaction(db, conversation):
    """The caller owns the commit: a rolled-back transaction leaves no claim."""
    turn = uuid4()
    async with db.engine.connect() as conn, conn.begin() as tx:
        claim = await try_claim(conn, conversation, turn)
        assert claim is not None
        await tx.rollback()
    assert (await _row(db, conversation)).active_turn_id is None


class _Counting:
    def __init__(self) -> None:
        self.names: list[str] = []

    async def hit(self, name: str) -> None:
        self.names.append(name)
