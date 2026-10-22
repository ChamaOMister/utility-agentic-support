import asyncio
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from support_api.turns.claim import ClaimLost, fenced
from support_api.usage.budget import (
    BudgetExceeded,
    Usage,
    actual_usd,
    mark_uncertain,
    recover_reservations,
    release,
    settle,
    worst_case_usd,
)
from support_api.usage.pricing import Rate

from tests.conftest import (
    conv_budget,
    conversation_row,
    llm_call,
    lock_conversation,
    make_claimed_conversation,
    project_budget,
    reserve_in_fenced,
    take_over,
)

D = Decimal
RATE = Rate(D("2"), D("8"), D("0.5"))
USAGE = Usage(input_tokens=1000, cached_input_tokens=400, output_tokens=500)
USAGE_COST = D("0.005400")  # 600*2 + 400*0.5 + 500*8 = 5400 per million


async def amounts(db, claim):
    """(project reserved/uncertain/spent) and (conversation reserved/uncertain/spent) now."""
    p, c = await project_budget(db), await conv_budget(db, claim)
    return (p.reserved_usd, p.uncertain_usd, p.spent_usd), (
        c.reserved_usd,
        c.uncertain_usd,
        c.spent_usd,
    )


def delta(after, before):
    return tuple(a - b for a, b in zip(after, before, strict=True))


async def test_concurrent_reservations_never_exceed_conversation_cap(
    db, claimed_conversation
):
    """AC 15: ten racing 0.03 reservations against the 0.10 cap -> exactly three win."""
    before_p = (await project_budget(db)).reserved_usd
    barrier = asyncio.Barrier(10)
    results = await asyncio.gather(
        *(
            reserve_in_fenced(db, claimed_conversation, D("0.03"), barrier=barrier)
            for _ in range(10)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(r, UUID) for r in results) == 3
    losers = [r for r in results if not isinstance(r, UUID)]
    assert len(losers) == 7 and all(
        isinstance(r, BudgetExceeded) and r.scope == "conversation" for r in losers
    )
    conv = await conv_budget(db, claimed_conversation)
    assert conv.reserved_usd == D("0.09")
    assert (conv.spent_usd, conv.uncertain_usd) == (0, 0)
    after_p = (await project_budget(db)).reserved_usd
    assert after_p - before_p == D(
        "0.09"
    )  # losers rolled back: project saw only the winners


async def test_concurrent_reservations_never_exceed_project_cap(
    db, many_claimed, project_cap_small
):
    before = await project_budget(db)
    barrier = asyncio.Barrier(len(many_claimed))
    results = await asyncio.gather(
        *(reserve_in_fenced(db, c, D("0.02"), barrier=barrier) for c in many_claimed),
        return_exceptions=True,
    )
    winners = [
        c for c, r in zip(many_claimed, results, strict=True) if isinstance(r, UUID)
    ]
    assert len(winners) == 2  # 0.05 of headroom holds two 0.02 reservations
    for r in results:
        assert isinstance(r, UUID) or (
            isinstance(r, BudgetExceeded) and r.scope == "project"
        )
    after = await project_budget(db)
    assert after.reserved_usd - before.reserved_usd == D("0.04")
    assert (
        after.uncertain_usd == before.uncertain_usd
        and after.spent_usd == before.spent_usd
    )
    for c in many_claimed:
        conv = await conv_budget(db, c)
        if c in winners:
            assert conv.reserved_usd == D("0.02")
        else:  # rolled back: no conversation row was left behind by the failed reservation
            assert conv is None


async def test_conversation_cap_failure_rolls_back_project_update(
    db, claimed_conversation
):
    await reserve_in_fenced(db, claimed_conversation, D("0.09"))
    before = await project_budget(db)
    with pytest.raises(BudgetExceeded) as exc:
        await reserve_in_fenced(db, claimed_conversation, D("0.03"))
    assert exc.value.scope == "conversation"
    after = await project_budget(db)
    assert after.reserved_usd == before.reserved_usd
    assert (await conv_budget(db, claimed_conversation)).reserved_usd == D("0.09")


async def test_project_cap_is_checked_first(
    db, claimed_conversation, project_cap_small
):
    with pytest.raises(BudgetExceeded) as exc:
        # exceeds the project headroom (0.05) and the conversation cap (0.10)
        await reserve_in_fenced(db, claimed_conversation, D("0.20"))
    assert exc.value.scope == "project"
    assert await conv_budget(db, claimed_conversation) is None


async def test_reserve_records_ledger_row_and_both_budgets(db, claimed_conversation):
    before = await project_budget(db)
    call_id = await reserve_in_fenced(
        db,
        claimed_conversation,
        D("0.0123451"),
        kind="token_count",
        model="mx",
        tier="flex",
        attempt=2,
    )
    row = await llm_call(db, call_id)
    assert (row.status, row.kind, row.model, row.service_tier, row.attempt) == (
        "reserved", "token_count", "mx", "flex", 2,
    )  # fmt: skip
    assert row.reserved_usd == D("0.012346")  # rounded up to 6 places
    assert (row.conversation_id, row.turn_id) == (
        claimed_conversation.conversation_id,
        claimed_conversation.turn_id,
    )
    conv = await conv_budget(db, claimed_conversation)
    assert conv.cap_usd == D("0.10") and conv.reserved_usd == D("0.012346")
    assert (await project_budget(db)).reserved_usd - before.reserved_usd == D(
        "0.012346"
    )


async def test_reserve_after_takeover_is_fenced_and_changes_nothing(
    db, claimed_conversation
):
    await take_over(db, claimed_conversation)
    before = await project_budget(db)
    with pytest.raises(ClaimLost):
        await reserve_in_fenced(db, claimed_conversation, D("0.01"))
    after = await project_budget(db)
    assert after.reserved_usd == before.reserved_usd
    assert await conv_budget(db, claimed_conversation) is None


@pytest.mark.parametrize("bad", [D("0"), D("-0.01"), D("NaN"), D("Infinity")])
async def test_reserve_rejects_non_positive_or_non_finite_amounts(
    db, claimed_conversation, bad
):
    with pytest.raises(ValueError):
        await reserve_in_fenced(db, claimed_conversation, bad)


async def test_settle_moves_reserved_to_spent(db, reservation):
    claim = reservation.claim
    p0, _ = await amounts(db, claim)
    assert await settle(db, reservation.id, USAGE, RATE) is True
    p1, c1 = await amounts(db, claim)
    assert delta(p1, p0) == (-reservation.amount, 0, USAGE_COST)
    assert c1 == (0, 0, USAGE_COST)
    row = await llm_call(db, reservation.id)
    assert row.status == "settled" and row.estimated_usd == USAGE_COST
    assert (row.input_tokens, row.output_tokens) == (1000, 500)
    assert row.reserved_usd == reservation.amount  # the ledger keeps what was reserved


async def test_uncertain_never_released_and_counts_against_cap(db, reservation):
    claim = reservation.claim
    async with fenced(db, claim) as conn:
        assert (
            await mark_uncertain(conn, claim, reservation.id, "unknown", None) is True
        )
    p0, c0 = await amounts(db, claim)
    assert c0 == (0, reservation.amount, 0)
    async with fenced(db, claim) as conn:
        assert await release(conn, claim, reservation.id) is False
    assert await amounts(db, claim) == (p0, c0)
    row = await llm_call(db, reservation.id)
    assert (row.status, row.outcome_class) == ("uncertain", "unknown")
    # 0.04 uncertain + 0.07 more would pass the 0.10 cap: uncertain counts against it
    with pytest.raises(BudgetExceeded) as exc:
        await reserve_in_fenced(db, claim, D("0.07"))
    assert exc.value.scope == "conversation"
    await reserve_in_fenced(db, claim, D("0.06"))  # exactly fills the cap


async def test_settle_from_uncertain(db, reservation):
    claim = reservation.claim
    async with fenced(db, claim) as conn:
        await mark_uncertain(
            conn, claim, reservation.id, "unknown", "provider_outcome_unknown"
        )
    p0, _ = await amounts(db, claim)
    assert await settle(db, reservation.id, USAGE, RATE) is True
    p1, c1 = await amounts(db, claim)
    assert delta(p1, p0) == (0, -reservation.amount, USAGE_COST)
    assert c1 == (0, 0, USAGE_COST)
    assert (await llm_call(db, reservation.id)).status == "settled"


async def test_settle_after_takeover_still_applies(db, reservation):
    claim = reservation.claim
    new_claim = await take_over(db, claim)
    async with db.begin() as conn:  # recovery as Task 17 runs it: caller holds the lock
        await lock_conversation(conn, claim)
        assert (
            await recover_reservations(conn, claim.conversation_id, claim.turn_id) == 1
        )
    assert (await llm_call(db, reservation.id)).status == "uncertain"
    assert await settle(db, reservation.id, USAGE, RATE) is True  # no claim needed
    assert (await llm_call(db, reservation.id)).status == "settled"
    assert (await conv_budget(db, new_claim)).spent_usd == USAGE_COST
    assert (await conv_budget(db, new_claim)).uncertain_usd == 0


async def test_release_and_mark_uncertain_fenced_after_takeover(db, reservation):
    await take_over(db, reservation.claim.conversation_id)
    c0 = await conv_budget(db, reservation.claim)
    for op in (
        lambda c: release(c, reservation.claim, reservation.id),
        lambda c: mark_uncertain(
            c, reservation.claim, reservation.id, "unknown", "provider_outcome_unknown"
        ),
    ):
        with pytest.raises(ClaimLost):
            async with fenced(db, reservation.claim) as conn:
                await op(conn)
    assert (
        await llm_call(db, reservation.id)
    ).status == "reserved"  # untouched until recovery
    c1 = await conv_budget(db, reservation.claim)
    assert (c1.reserved_usd, c1.uncertain_usd) == (c0.reserved_usd, c0.uncertain_usd)
    assert (await conversation_row(db, reservation.claim)).live_blocked_reason is None


async def test_release_returns_reservation_to_both_budgets(db, reservation):
    claim = reservation.claim
    p0, _ = await amounts(db, claim)
    async with fenced(db, claim) as conn:
        assert await release(conn, claim, reservation.id) is True
    p1, c1 = await amounts(db, claim)
    assert delta(p1, p0) == (-reservation.amount, 0, 0)
    assert c1 == (0, 0, 0)
    assert (await llm_call(db, reservation.id)).status == "released"


async def test_release_never_applies_to_uncertain(db, reservation):
    claim = reservation.claim
    async with fenced(db, claim) as conn:
        assert (
            await mark_uncertain(conn, claim, reservation.id, "unknown", None) is True
        )
    before = await amounts(db, claim)
    async with fenced(db, claim) as conn:
        assert await release(conn, claim, reservation.id) is False
    assert await amounts(db, claim) == before
    assert (await llm_call(db, reservation.id)).status == "uncertain"


async def test_mark_uncertain_sets_block_reason_once_and_never_overwrites(
    db, claimed_conversation
):
    claim = claimed_conversation
    first = await reserve_in_fenced(db, claim, D("0.02"))
    second = await reserve_in_fenced(db, claim, D("0.02"))
    async with fenced(db, claim) as conn:
        assert (
            await mark_uncertain(conn, claim, first, "http_429", "provider_quota")
            is True
        )
    assert (await conversation_row(db, claim)).live_blocked_reason == "provider_quota"
    async with fenced(db, claim) as conn:
        assert (
            await mark_uncertain(
                conn, claim, second, "http_5xx", "provider_outcome_unknown"
            )
            is True
        )
    assert (await conversation_row(db, claim)).live_blocked_reason == "provider_quota"


async def test_mark_uncertain_without_reason_leaves_conversation_unblocked(
    db, reservation
):
    claim = reservation.claim
    async with fenced(db, claim) as conn:
        await mark_uncertain(conn, claim, reservation.id, "http_429", None)
    assert (await conversation_row(db, claim)).live_blocked_reason is None


async def test_ledger_ops_on_another_conversations_call_are_rejected(db, reservation):
    other = await make_claimed_conversation(db)
    before = await amounts(db, reservation.claim)
    for op in (
        lambda c: release(c, other, reservation.id),
        lambda c: mark_uncertain(c, other, reservation.id, "unknown", None),
    ):
        with pytest.raises(ValueError):
            async with fenced(db, other) as conn:
                await op(conn)
    assert await amounts(db, reservation.claim) == before
    assert (await llm_call(db, reservation.id)).status == "reserved"


async def test_ledger_ops_on_unknown_call_return_false(db, claimed_conversation):
    async with fenced(db, claimed_conversation) as conn:
        assert await release(conn, claimed_conversation, uuid4()) is False
        assert (
            await mark_uncertain(conn, claimed_conversation, uuid4(), "x", None)
            is False
        )
    assert await settle(db, uuid4(), USAGE, RATE) is False


async def test_recover_reservations_uses_caller_transaction(db, reservation):
    claim = reservation.claim
    before = await amounts(db, claim)
    async with db.begin() as conn:
        await lock_conversation(conn, claim.conversation_id)
        assert (
            await recover_reservations(conn, claim.conversation_id, claim.turn_id) == 1
        )
        await conn.rollback()  # caller owns the transaction
    assert (await llm_call(db, reservation.id)).status == "reserved"
    assert await amounts(db, claim) == before
    assert (await conversation_row(db, claim)).live_blocked_reason is None


async def test_recover_reservations_moves_only_this_turns_reserved_rows(
    db, claimed_conversation
):
    claim = claimed_conversation
    a = await reserve_in_fenced(db, claim, D("0.02"))
    b = await reserve_in_fenced(db, claim, D("0.03"))
    settled = await reserve_in_fenced(db, claim, D("0.01"))
    released = await reserve_in_fenced(db, claim, D("0.01"))
    assert await settle(db, settled, USAGE, RATE) is True
    async with fenced(db, claim) as conn:
        assert await release(conn, claim, released) is True
    # a call belonging to a different turn of the same conversation is not recovered
    async with db.begin() as conn:
        other_turn = uuid4()
        await conn.execute(
            text(
                "INSERT INTO support.turns (id, conversation_id, client_message_id, "
                "text_sha256, correlation_id) VALUES (:t, :c, :m, 'x', 'corr')"
            ),
            {"t": other_turn, "c": claim.conversation_id, "m": uuid4()},
        )
        foreign = (
            await conn.execute(
                text(
                    "INSERT INTO support.llm_calls (conversation_id, turn_id, kind, model, "
                    "service_tier, status, reserved_usd) "
                    "VALUES (:c, :t, 'generation', 'm', 'default', 'reserved', 0) RETURNING id"
                ),
                {"c": claim.conversation_id, "t": other_turn},
            )
        ).scalar_one()
    p0, c0 = await amounts(db, claim)
    async with db.begin() as conn:
        await lock_conversation(conn, claim.conversation_id)
        assert (
            await recover_reservations(conn, claim.conversation_id, claim.turn_id) == 2
        )
    p1, c1 = await amounts(db, claim)
    assert delta(p1, p0) == (-D("0.05"), D("0.05"), 0)
    assert delta(c1, c0) == (-D("0.05"), D("0.05"), 0)
    assert c1[1] == D("0.05") and c1[2] == USAGE_COST
    for call_id, status in ((a, "uncertain"), (b, "uncertain"), (settled, "settled"),
                            (released, "released"), (foreign, "reserved")):  # fmt: skip
        assert (await llm_call(db, call_id)).status == status
    assert (
        await conversation_row(db, claim)
    ).live_blocked_reason == "interrupted_after_possible_send"
    # idempotent: nothing left to move
    async with db.begin() as conn:
        await lock_conversation(conn, claim.conversation_id)
        assert (
            await recover_reservations(conn, claim.conversation_id, claim.turn_id) == 0
        )
    assert await amounts(db, claim) == (p1, c1)


async def test_recover_reservations_with_nothing_to_move_does_not_block(
    db, claimed_conversation
):
    claim = claimed_conversation
    async with db.begin() as conn:
        await lock_conversation(conn, claim.conversation_id)
        assert (
            await recover_reservations(conn, claim.conversation_id, claim.turn_id) == 0
        )
    assert (await conversation_row(db, claim)).live_blocked_reason is None


async def test_recover_reservations_never_overwrites_block_reason(
    db, claimed_conversation
):
    claim = claimed_conversation
    first = await reserve_in_fenced(db, claim, D("0.02"))
    await reserve_in_fenced(db, claim, D("0.02"))
    async with fenced(db, claim) as conn:
        await mark_uncertain(conn, claim, first, "http_400", "provider_rejected")
    async with db.begin() as conn:
        await lock_conversation(conn, claim.conversation_id)
        assert (
            await recover_reservations(conn, claim.conversation_id, claim.turn_id) == 1
        )
    assert (
        await conversation_row(db, claim)
    ).live_blocked_reason == "provider_rejected"


async def test_ledger_ops_idempotent(db, reservation):
    claim = reservation.claim
    async with fenced(db, claim) as conn:
        assert await mark_uncertain(
            conn, claim, reservation.id, "unknown", "provider_outcome_unknown"
        )
    after_uncertain = await amounts(db, claim)
    async with fenced(db, claim) as conn:
        assert (
            await mark_uncertain(
                conn, claim, reservation.id, "unknown", "provider_outcome_unknown"
            )
            is False
        )
    assert await amounts(db, claim) == after_uncertain
    assert await settle(db, reservation.id, USAGE, RATE) is True
    after_settle = await amounts(db, claim)
    assert await settle(db, reservation.id, USAGE, RATE) is False
    assert await amounts(db, claim) == after_settle
    async with fenced(db, claim) as conn:
        assert (
            await mark_uncertain(conn, claim, reservation.id, "unknown", None) is False
        )
        assert await release(conn, claim, reservation.id) is False
    assert await amounts(db, claim) == after_settle


async def test_concurrent_settles_count_spend_once(db, reservation):
    claim = reservation.claim
    p0, _ = await amounts(db, claim)
    results = await asyncio.wait_for(
        asyncio.gather(*(settle(db, reservation.id, USAGE, RATE) for _ in range(8))), 10
    )
    assert results.count(True) == 1 and results.count(False) == 7
    p1, c1 = await amounts(db, claim)
    assert delta(p1, p0) == (-reservation.amount, 0, USAGE_COST)
    assert c1 == (0, 0, USAGE_COST)


async def test_concurrent_settle_and_recovery_account_exactly_once(db, reservation):
    """Whichever wins, the held amount leaves exactly one bucket and spend is counted once."""
    claim = reservation.claim
    p0, _ = await amounts(db, claim)

    async def recover():
        async with db.begin() as conn:
            await lock_conversation(conn, claim.conversation_id)
            return await recover_reservations(
                conn, claim.conversation_id, claim.turn_id
            )

    await asyncio.wait_for(
        asyncio.gather(settle(db, reservation.id, USAGE, RATE), recover()), 10
    )
    assert (await llm_call(db, reservation.id)).status == "settled"
    p1, c1 = await amounts(db, claim)
    assert delta(p1, p0) == (-reservation.amount, 0, USAGE_COST)
    assert c1 == (0, 0, USAGE_COST)


def test_worst_case_rounds_up():
    assert worst_case_usd(1, 0, Rate(D("0.0000001"), D(0), None)) == D("0.000001")


def test_worst_case_prices_input_and_full_output_allowance():
    # 1000 * 2 + 800 * 8 = 8400 per million
    assert worst_case_usd(1000, 800, RATE) == D("0.008400")


def test_actual_usd_prices_cached_input_at_cached_rate_when_set():
    assert actual_usd(USAGE, RATE) == USAGE_COST


def test_actual_usd_cached_input_falls_back_to_input_rate():
    rate = Rate(D("2"), D("8"), None)
    assert actual_usd(USAGE, rate) == D("0.006000")  # 1000*2 + 500*8


def test_actual_usd_rounds_up():
    assert actual_usd(Usage(1, 0, 0), Rate(D("0.5"), D("1"), None)) == D("0.000001")


@pytest.mark.parametrize("usage", [Usage(10, 11, 0), Usage(-1, 0, 0), Usage(1, 0, -1)])
def test_actual_usd_rejects_inconsistent_usage(usage):
    with pytest.raises(ValueError):
        actual_usd(usage, RATE)
