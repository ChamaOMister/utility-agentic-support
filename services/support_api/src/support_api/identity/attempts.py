"""Attempt slots, the attempt-outcome transaction and applying a verification (spec §5.2).

Row-lock order in every transaction here: ``conversations`` (held by ``fenced``, where used) ->
``verification_attempts`` -> ``identifier_lockouts`` -> ``conversation_auth``. Tables a transaction
does not touch are skipped. Failure counters change only in :func:`_record_in` and are keyed by
attempt id, so replays never double-count.
"""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from support_api.db import Database
from support_api.settings import Settings
from support_api.turns.claim import Claim, ClaimLost, fenced

Outcome = Literal["failed", "verified", "upstream_error"]


@dataclass(frozen=True)
class IdentityLimits:
    """The identity numbers of §5.2; defaults match ``Settings``."""

    max_failures: int = 3
    window_s: int = 900
    lock_s: int = 900
    slot_ttl_s: int = 30
    verification_ttl_s: int = 1800

    @classmethod
    def from_settings(cls, settings: Settings) -> "IdentityLimits":
        return cls(
            settings.identity_max_failures,
            settings.identity_window_s,
            settings.identity_lock_s,
            settings.verification_slot_ttl_s,
            settings.verification_ttl_s,
        )


DEFAULT_LIMITS = IdentityLimits()


class AdmissionRejected(Exception):
    def __init__(self, code: Literal["locked", "verification_busy"]) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _Counter:
    """One failure counter: table, key column and count column. The key column is also the
    ``verification_attempts`` column its in-flight slots are counted by. Fixed identifiers only."""

    table: str
    key: str
    count: str


_IDENTIFIER = _Counter("support.identifier_lockouts", "identifier_hash", "failed_count")
_CONVERSATION = _Counter(
    "support.conversation_auth", "conversation_id", "failed_attempts"
)


async def _lock_counter(
    conn: AsyncConnection, counter: _Counter, key: object, limits: IdentityLimits
):
    """Create the counter row if missing, lock it, and reset its window if it has expired.

    Returns the locked row's ``failed`` count and whether it is currently locked.
    """
    c = counter
    await conn.execute(
        text(f"INSERT INTO {c.table} ({c.key}) VALUES (:k) ON CONFLICT DO NOTHING"),
        {"k": key},
    )
    row = (
        await conn.execute(
            text(
                f"SELECT {c.count} AS failed, COALESCE(locked_until > now(), false) AS locked, "
                "(window_started_at IS NULL "
                " OR window_started_at <= now() - make_interval(secs => :w)) AS stale "
                f"FROM {c.table} WHERE {c.key} = :k FOR UPDATE"
            ),
            {"k": key, "w": limits.window_s},
        )
    ).one()
    if row.stale:
        await conn.execute(
            text(
                f"UPDATE {c.table} SET {c.count} = 0, window_started_at = now() "
                f"WHERE {c.key} = :k"
            ),
            {"k": key},
        )
        return 0, row.locked
    return row.failed, row.locked


async def _check_admission(
    conn: AsyncConnection, counter: _Counter, key: object, limits: IdentityLimits
) -> None:
    failed, locked = await _lock_counter(conn, counter, key, limits)
    if locked:
        raise AdmissionRejected("locked")
    in_flight = (
        await conn.execute(
            text(
                "SELECT count(*) FROM support.verification_attempts "
                f"WHERE {counter.key} = :k "
                "AND status = 'in_flight' AND expires_at > now()"
            ),
            {"k": key},
        )
    ).scalar_one()
    if failed + in_flight >= limits.max_failures:
        raise AdmissionRejected("verification_busy")


async def admit(
    conn: AsyncConnection,
    claim: Claim,
    identifier_hash: str,
    *,
    limits: IdentityLimits = DEFAULT_LIMITS,
) -> UUID:
    """Reserve an attempt slot (§5.2 step 1). Call inside :func:`fenced`; the caller commits.

    Locks the identifier row (serialising admission per identifier), then the conversation's, and
    raises :class:`AdmissionRejected` (``locked`` before ``verification_busy``) when either is
    locked or full. Rejection raises, so the surrounding transaction rolls back and no slot exists.
    """
    await _check_admission(conn, _IDENTIFIER, identifier_hash, limits)
    await _check_admission(conn, _CONVERSATION, claim.conversation_id, limits)
    return (
        await conn.execute(
            text(
                "INSERT INTO support.verification_attempts "
                "(identifier_hash, conversation_id, turn_id, status, expires_at) "
                "VALUES (:h, :c, :t, 'in_flight', now() + make_interval(secs => :ttl)) "
                "RETURNING id"
            ),
            {
                "h": identifier_hash,
                "c": claim.conversation_id,
                "t": claim.turn_id,
                "ttl": limits.slot_ttl_s,
            },
        )
    ).scalar_one()


async def _increment(
    conn: AsyncConnection, counter: _Counter, key: object, limits: IdentityLimits
) -> None:
    """Window-aware failure increment; sets ``locked_until`` when the count reaches the cap."""
    failed, _ = await _lock_counter(conn, counter, key, limits)
    c = counter
    await conn.execute(
        text(
            f"UPDATE {c.table} SET {c.count} = :n, "
            "locked_until = CASE WHEN :n >= :max "
            "THEN now() + make_interval(secs => :lock) ELSE locked_until END "
            f"WHERE {c.key} = :k"
        ),
        {
            "k": key,
            "n": failed + 1,
            "max": limits.max_failures,
            "lock": limits.lock_s,
        },
    )


async def _record_in(
    conn: AsyncConnection,
    attempt_id: UUID,
    outcome: Outcome,
    limits: IdentityLimits,
) -> bool:
    """The attempt-outcome transaction body, shared by every path that records an outcome.

    Zero rows from the conditional update means the outcome was already recorded: nothing else
    happens. Only ``failed`` touches counters. Never writes assurance.
    """
    row = (
        await conn.execute(
            text(
                "UPDATE support.verification_attempts SET status = :o "
                "WHERE id = :a AND status IN ('in_flight', 'abandoned') "
                "RETURNING identifier_hash, conversation_id"
            ),
            {"o": outcome, "a": attempt_id},
        )
    ).first()
    if row is None:
        return False
    if outcome == "failed":
        await _increment(conn, _IDENTIFIER, row.identifier_hash, limits)
        await _increment(conn, _CONVERSATION, row.conversation_id, limits)
    return True


async def record_outcome(
    db: Database,
    attempt_id: UUID,
    outcome: Outcome,
    *,
    limits: IdentityLimits = DEFAULT_LIMITS,
) -> bool:
    """Record a verification outcome. Unfenced and idempotent: ``False`` if already recorded."""
    async with db.begin() as conn:
        return await _record_in(conn, attempt_id, outcome, limits)


async def apply_verified(
    db: Database,
    claim: Claim,
    attempt_id: UUID,
    customer_ref: str,
    first_name: str,
    *,
    limits: IdentityLimits = DEFAULT_LIMITS,
) -> bool:
    """Record ``verified`` and grant kba assurance, in one fenced transaction.

    ``True``: both committed under the claim. ``False``: the claim was lost, so only the outcome
    is recorded (assurance untouched), or the attempt's outcome had already been recorded by
    someone else, in which case nothing is granted.
    """
    try:
        async with fenced(db, claim) as conn:
            if not await _record_in(conn, attempt_id, "verified", limits):
                return False
            await conn.execute(
                text(
                    "INSERT INTO support.conversation_auth "
                    "(conversation_id, assurance_level, customer_ref, first_name, "
                    " verified_at, verified_expires_at) "
                    "VALUES (:c, 'kba', :ref, :name, now(), now() + make_interval(secs => :ttl)) "
                    "ON CONFLICT (conversation_id) DO UPDATE SET "
                    "assurance_level = 'kba', customer_ref = EXCLUDED.customer_ref, "
                    "first_name = EXCLUDED.first_name, verified_at = EXCLUDED.verified_at, "
                    "verified_expires_at = EXCLUDED.verified_expires_at"
                ),
                {
                    "c": claim.conversation_id,
                    "ref": customer_ref,
                    "name": first_name,
                    "ttl": limits.verification_ttl_s,
                },
            )
            return True
    except ClaimLost:
        await record_outcome(db, attempt_id, "verified", limits=limits)
        return False
