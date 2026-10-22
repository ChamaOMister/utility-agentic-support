import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from support_api.identity.assurance import conversation_locked
from support_api.identity.attempts import (
    AdmissionRejected,
    apply_verified,
    record_outcome,
)

from tests.conftest import (
    HASH,
    admit_in_fenced,
    assurance,
    attempt_status,
    counters,
    expire_verification,
    fail_attempt,
    make_claimed_conversation,
    take_over,
)

OTHER_HASH = "b" * 64


async def _sql(db, statement: str, **params):
    async with db.begin() as conn:
        result = await conn.execute(text(statement), params)
        return result.all() if result.returns_rows else []


async def _rejection(db, claim, identifier_hash=HASH) -> str:
    with pytest.raises(AdmissionRejected) as info:
        await admit_in_fenced(db, claim, identifier_hash)
    return info.value.code


async def _locked(db, claim) -> bool:
    async with db.begin() as conn:
        return await conversation_locked(conn, claim.conversation_id)


async def test_three_failures_lock_identifier_and_conversation(
    db, claimed_conversation
):
    for _ in range(3):
        last = await fail_attempt(db, claimed_conversation)
    c = await counters(db, last)
    assert (c.identifier_failed, c.identifier_locked) == (3, True)
    assert (c.conversation_failed, c.conversation_locked) == (3, True)
    assert await _locked(db, claimed_conversation)
    assert await _rejection(db, claimed_conversation) == "locked"


async def test_identifier_lock_spans_conversations(db, claimed_conversation):
    for _ in range(3):
        await fail_attempt(db, claimed_conversation)
    other = await make_claimed_conversation(db)
    assert await _rejection(db, other) == "locked"
    assert not await _locked(db, other)  # the other conversation itself is not locked
    # ...and a different identifier is still fine there
    assert isinstance(await admit_in_fenced(db, other, OTHER_HASH), UUID)


async def test_conversation_lock_spans_identifiers(db, claimed_conversation):
    for h in (
        "1" * 64,
        "2" * 64,
        "3" * 64,
    ):  # one failure each: no identifier reaches 3
        await fail_attempt(db, claimed_conversation, h)
    assert await _rejection(db, claimed_conversation, OTHER_HASH) == "locked"


async def test_locked_takes_precedence_over_busy(db, claimed_conversation):
    for _ in range(3):
        await fail_attempt(db, claimed_conversation)
    other = await make_claimed_conversation(db)
    # the identifier has failed >= 3 AND would be "busy"; the answer must be locked
    assert await _rejection(db, other) == "locked"


async def test_failures_plus_in_flight_reach_the_cap_as_busy(db, claimed_conversation):
    await fail_attempt(db, claimed_conversation)
    await fail_attempt(db, claimed_conversation)
    first = await admit_in_fenced(
        db, claimed_conversation, HASH
    )  # 2 failed + 1 in flight
    assert isinstance(first, UUID)
    other = await make_claimed_conversation(db)
    assert await _rejection(db, other) == "verification_busy"
    # busy is not a failure and not a lock
    assert (await counters(db, first)).identifier_failed == 2
    assert not (await counters(db, first)).identifier_locked


async def test_conversation_busy_counts_its_own_in_flight_slots(
    db, claimed_conversation
):
    for h in ("1" * 64, "2" * 64, "3" * 64):
        await admit_in_fenced(db, claimed_conversation, h)
    assert await _rejection(db, claimed_conversation, OTHER_HASH) == "verification_busy"


async def test_rejection_leaves_no_attempt_row(db, claimed_conversation):
    for _ in range(3):
        await fail_attempt(db, claimed_conversation)
    before = await _sql(db, "SELECT count(*) FROM support.verification_attempts")
    await _rejection(db, claimed_conversation)
    assert (
        await _sql(db, "SELECT count(*) FROM support.verification_attempts") == before
    )


async def test_window_resets_after_15_minutes(db, claimed_conversation):
    await fail_attempt(db, claimed_conversation)
    await fail_attempt(db, claimed_conversation)
    await _sql(
        db,
        "UPDATE support.identifier_lockouts SET window_started_at = now() - interval '16 minutes' "
        "WHERE identifier_hash = :h",
        h=HASH,
    )
    await _sql(
        db,
        "UPDATE support.conversation_auth SET window_started_at = now() - interval '16 minutes' "
        "WHERE conversation_id = :c",
        c=claimed_conversation.conversation_id,
    )
    attempt = await fail_attempt(db, claimed_conversation)
    c = await counters(db, attempt)
    assert (c.identifier_failed, c.identifier_locked) == (1, False)
    assert (c.conversation_failed, c.conversation_locked) == (1, False)


async def test_window_inside_15_minutes_keeps_counting(db, claimed_conversation):
    await fail_attempt(db, claimed_conversation)
    await fail_attempt(db, claimed_conversation)
    await _sql(
        db,
        "UPDATE support.identifier_lockouts SET window_started_at = now() - interval '14 minutes' "
        "WHERE identifier_hash = :h",
        h=HASH,
    )
    attempt = await fail_attempt(db, claimed_conversation)
    assert (await counters(db, attempt)).identifier_failed == 3


async def test_stale_window_does_not_block_admission(db, claimed_conversation):
    # two failures long ago + one in flight now: the old failures no longer count toward the cap
    await fail_attempt(db, claimed_conversation)
    await fail_attempt(db, claimed_conversation)
    await _sql(
        db,
        "UPDATE support.identifier_lockouts SET window_started_at = now() - interval '16 minutes'",
    )
    await _sql(
        db,
        "UPDATE support.conversation_auth SET window_started_at = now() - interval '16 minutes' "
        "WHERE conversation_id = :c",
        c=claimed_conversation.conversation_id,
    )
    await admit_in_fenced(db, claimed_conversation, HASH)
    other = await make_claimed_conversation(db)
    assert isinstance(await admit_in_fenced(db, other, HASH), UUID)


async def test_lock_expires_after_15_minutes(db, claimed_conversation):
    for _ in range(3):
        await fail_attempt(db, claimed_conversation)
    await _sql(
        db,
        "UPDATE support.identifier_lockouts SET locked_until = now() - interval '1 second', "
        "window_started_at = now() - interval '16 minutes' WHERE identifier_hash = :h",
        h=HASH,
    )
    await _sql(
        db,
        "UPDATE support.conversation_auth SET locked_until = now() - interval '1 second', "
        "window_started_at = now() - interval '16 minutes' WHERE conversation_id = :c",
        c=claimed_conversation.conversation_id,
    )
    assert not await _locked(db, claimed_conversation)
    assert isinstance(await admit_in_fenced(db, claimed_conversation, HASH), UUID)


async def test_lock_lasts_15_minutes_from_the_third_failure(db, claimed_conversation):
    for _ in range(3):
        await fail_attempt(db, claimed_conversation)
    (row,) = await _sql(
        db,
        "SELECT il.locked_until - now() AS ident, ca.locked_until - now() AS conv "
        "FROM support.identifier_lockouts il, support.conversation_auth ca "
        "WHERE il.identifier_hash = :h AND ca.conversation_id = :c",
        h=HASH,
        c=claimed_conversation.conversation_id,
    )
    for remaining in (row.ident, row.conv):
        assert 14 * 60 < remaining.total_seconds() <= 15 * 60


async def test_upstream_error_frees_slot_without_counting(
    db, claimed_conversation
):  # AC 7
    for _ in range(5):
        attempt = await admit_in_fenced(db, claimed_conversation, HASH)
        assert await record_outcome(db, attempt, "upstream_error") is True
    assert await attempt_status(db, attempt) == "upstream_error"
    c = await counters(db, attempt)
    assert (c.identifier_failed, c.identifier_locked) == (0, False)
    assert (c.conversation_failed, c.conversation_locked) == (0, False)
    assert isinstance(await admit_in_fenced(db, claimed_conversation, HASH), UUID)


async def test_expired_slots_no_longer_count(db, claimed_conversation):
    for _ in range(3):
        await admit_in_fenced(db, claimed_conversation, HASH)
    other = await make_claimed_conversation(db)
    assert await _rejection(db, other) == "verification_busy"
    await _sql(
        db,
        "UPDATE support.verification_attempts SET expires_at = now() - interval '1 second'",
    )
    assert isinstance(await admit_in_fenced(db, other, HASH), UUID)


async def test_slot_expires_30_seconds_after_admission(db, attempt):
    (row,) = await _sql(
        db,
        "SELECT expires_at - created_at AS ttl, status FROM support.verification_attempts "
        "WHERE id = :a",
        a=attempt,
    )
    assert row.ttl.total_seconds() == 30
    assert row.status == "in_flight"


async def test_record_outcome_is_idempotent(db, attempt):
    assert await record_outcome(db, attempt, "failed") is True
    assert await record_outcome(db, attempt, "failed") is False
    assert (await counters(db, attempt)).identifier_failed == 1
    assert (await counters(db, attempt)).conversation_failed == 1


async def test_first_recorded_outcome_wins(db, attempt):
    assert await record_outcome(db, attempt, "upstream_error") is True
    assert await record_outcome(db, attempt, "failed") is False
    assert await attempt_status(db, attempt) == "upstream_error"
    assert (await counters(db, attempt)).identifier_failed == 0


async def test_record_outcome_unknown_attempt_is_a_noop(db):
    assert await record_outcome(db, uuid4(), "failed") is False


async def test_late_failure_after_slot_expired_still_counts(db, attempt):
    await _sql(
        db,
        "UPDATE support.verification_attempts SET expires_at = now() - interval '1 minute' "
        "WHERE id = :a",
        a=attempt,
    )
    assert await record_outcome(db, attempt, "failed") is True
    assert (await counters(db, attempt)).identifier_failed == 1


async def test_late_failure_after_abandoned_still_counts(db, attempt):
    await _sql(
        db,
        "UPDATE support.verification_attempts SET status = 'abandoned' WHERE id = :a",
        a=attempt,
    )
    assert await record_outcome(db, attempt, "failed") is True
    c = await counters(db, attempt)
    assert (c.identifier_failed, c.conversation_failed) == (1, 1)
    assert await attempt_status(db, attempt) == "failed"


async def test_verified_outcome_changes_no_counters_and_no_assurance(
    db, claimed_conversation, attempt
):
    assert await record_outcome(db, attempt, "verified") is True
    c = await counters(db, attempt)
    assert (c.identifier_failed, c.conversation_failed) == (0, 0)
    assert (await assurance(db, claimed_conversation)).level == "none"


async def test_concurrent_admission_caps_in_flight_at_three(
    db, ten_claimed_conversations
):  # AC 8
    barrier = asyncio.Barrier(len(ten_claimed_conversations))
    results = await asyncio.gather(
        *(
            admit_in_fenced(db, c, HASH, barrier=barrier)
            for c in ten_claimed_conversations
        ),
        return_exceptions=True,
    )
    admitted = [r for r in results if isinstance(r, UUID)]
    rejected = [r for r in results if isinstance(r, AdmissionRejected)]
    assert (
        len(admitted) == 3 and len(rejected) == 7
    )  # nothing else (no deadlock, no DB error)
    assert all(r.code == "verification_busy" for r in rejected)
    assert len(set(admitted)) == 3
    (row,) = await _sql(
        db,
        "SELECT count(*) AS n FROM support.verification_attempts WHERE identifier_hash = :h",
        h=HASH,
    )
    assert row.n == 3


async def test_concurrent_failures_and_admissions_do_not_deadlock(
    db, ten_claimed_conversations
):
    # Three slots admitted, then their failures race with admissions from the rest.
    first, rest = ten_claimed_conversations[:3], ten_claimed_conversations[3:]
    attempts = [await admit_in_fenced(db, c, HASH) for c in first]
    barrier = asyncio.Barrier(len(attempts) + len(rest))

    async def admit(c):
        try:
            return await admit_in_fenced(db, c, HASH, barrier=barrier)
        except AdmissionRejected as e:
            return e

    async def fail(a):
        await asyncio.wait_for(barrier.wait(), 10)
        return await record_outcome(db, a, "failed")

    results = await asyncio.gather(
        *(fail(a) for a in attempts), *(admit(c) for c in rest)
    )
    assert results[:3] == [True, True, True]
    assert all(
        isinstance(r, AdmissionRejected) for r in results[3:] if not isinstance(r, UUID)
    )
    c = await counters(db, attempts[0])
    assert (c.identifier_failed, c.identifier_locked) == (3, True)


async def test_late_failure_after_claim_lost_counts_on_both(
    db, claimed_conversation_with_two_failures
):
    attempt = await admit_in_fenced(db, claimed_conversation_with_two_failures, HASH)
    await take_over(db, claimed_conversation_with_two_failures)
    assert await record_outcome(db, attempt, "failed") is True
    c = await counters(db, attempt)
    assert c.identifier_failed == 3 and c.identifier_locked
    assert c.conversation_failed == 3 and c.conversation_locked


async def test_apply_verified_grants_kba_for_30_minutes(db, claimed_conversation):
    attempt = await admit_in_fenced(db, claimed_conversation, HASH)
    assert (
        await apply_verified(db, claimed_conversation, attempt, "cus_a", "Ana") is True
    )
    assert await attempt_status(db, attempt) == "verified"
    a = await assurance(db, claimed_conversation)
    assert (a.level, a.customer_ref, a.expired) == ("kba", "cus_a", False)
    (row,) = await _sql(
        db,
        "SELECT first_name, verified_expires_at - verified_at AS ttl, "
        "verified_at = now() AS stamped FROM support.conversation_auth WHERE conversation_id = :c",
        c=claimed_conversation.conversation_id,
    )
    assert row.first_name == "Ana" and row.ttl.total_seconds() == 1800


async def test_apply_verified_keeps_existing_failure_counters(db, claimed_conversation):
    await fail_attempt(db, claimed_conversation)
    attempt = await admit_in_fenced(db, claimed_conversation, HASH)
    assert await apply_verified(db, claimed_conversation, attempt, "cus_a", "Ana")
    c = await counters(db, attempt)
    assert (c.identifier_failed, c.conversation_failed) == (1, 1)


async def test_apply_verified_after_claim_lost_leaves_assurance(
    db, claimed_conversation
):
    attempt = await admit_in_fenced(db, claimed_conversation, HASH)
    await take_over(db, claimed_conversation)
    assert (
        await apply_verified(db, claimed_conversation, attempt, "cus_a", "Ana") is False
    )
    assert await attempt_status(db, attempt) == "verified"
    a = await assurance(db, claimed_conversation)
    assert (a.level, a.customer_ref) == ("none", None)


async def test_apply_verified_replay_does_not_regrant(db, claimed_conversation):
    attempt = await admit_in_fenced(db, claimed_conversation, HASH)
    assert (
        await apply_verified(db, claimed_conversation, attempt, "cus_a", "Ana") is True
    )
    await expire_verification(db, claimed_conversation)
    assert (
        await apply_verified(db, claimed_conversation, attempt, "cus_a", "Ana") is False
    )
    assert (await assurance(db, claimed_conversation)).expired is True


async def test_assurance_defaults_to_none(db, claimed_conversation):
    a = await assurance(db, claimed_conversation)
    assert (a.level, a.customer_ref, a.expired) == ("none", None, False)


async def test_assurance_expires_after_30_minutes(db, verified_conversation):
    assert (await assurance(db, verified_conversation)).level == "kba"
    await expire_verification(db, verified_conversation)
    a = await assurance(db, verified_conversation)
    assert (a.level, a.customer_ref, a.expired) == ("none", None, True)


async def test_assurance_uses_db_time(db, verified_conversation):
    await _sql(
        db,
        "UPDATE support.conversation_auth SET verified_expires_at = now() + interval '1 minute' "
        "WHERE conversation_id = :c",
        c=verified_conversation.conversation_id,
    )
    assert (await assurance(db, verified_conversation)).level == "kba"


async def test_conversation_locked_defaults_to_false(db, claimed_conversation):
    assert not await _locked(db, claimed_conversation)
