"""Turn claims, fencing and the pre-send gate (spec §5.5).

A turn worker holds ``Claim(conversation_id, turn_id, claim_version)`` as its fencing token. Every
state write it makes runs inside :func:`fenced`, whose first statement locks the conversation row
under the three-column predicate; zero rows means the claim was lost (taken over, released or
recovered) and nothing is written. :func:`pre_send` is the gate run immediately before every
outbound request.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from support_api.checkpoints import Checkpoints
from support_api.db import Database
from support_api.deadline import Deadline


@dataclass(frozen=True)
class Claim:
    conversation_id: UUID
    turn_id: UUID
    claim_version: int


class ClaimLost(Exception):
    """The worker's fencing token no longer matches the conversation row."""


class DeadlineExpired(Exception):
    """The turn deadline passed before an outbound request could start."""


_CLAIM = text(
    "UPDATE support.conversations "
    "SET active_turn_id = :t, claim_version = claim_version + 1, "
    "active_turn_lease_expires_at = clock_timestamp() + make_interval(secs => :lease) "
    "WHERE id = :c AND (active_turn_id IS NULL OR active_turn_lease_expires_at IS NULL "
    "OR active_turn_lease_expires_at <= clock_timestamp()) "
    "RETURNING claim_version"
)

_FENCE = text(
    "SELECT 1 FROM support.conversations "
    "WHERE id = :c AND active_turn_id = :t AND claim_version = :v FOR UPDATE"
)

_RELEASE = text(
    "UPDATE support.conversations "
    "SET active_turn_id = NULL, active_turn_lease_expires_at = NULL "
    "WHERE id = :c AND active_turn_id = :t AND claim_version = :v"
)


async def try_claim(
    conn: AsyncConnection,
    conversation_id: UUID,
    turn_id: UUID,
    lease_s: float = 60,
) -> Claim | None:
    """Claim the conversation for ``turn_id`` if it is free or its lease has expired.

    Runs in the caller's transaction; the caller commits. Returns ``None`` when another turn holds
    an unexpired lease (or the conversation does not exist).
    """
    version = (
        await conn.execute(
            _CLAIM, {"c": conversation_id, "t": turn_id, "lease": lease_s}
        )
    ).scalar_one_or_none()
    if version is None:
        return None
    return Claim(conversation_id, turn_id, version)


@asynccontextmanager
async def fenced(db: Database, claim: Claim) -> AsyncIterator[AsyncConnection]:
    """A transaction that holds the conversation row lock for the whole block.

    Zero matching rows rolls back and raises :class:`ClaimLost`. An exception in the block rolls
    back and propagates. Otherwise the transaction commits.
    """
    async with db.begin() as conn:
        row = (
            await conn.execute(
                _FENCE,
                {
                    "c": claim.conversation_id,
                    "t": claim.turn_id,
                    "v": claim.claim_version,
                },
            )
        ).first()
        if row is None:
            raise ClaimLost(
                f"claim lost for conversation {claim.conversation_id} "
                f"(turn {claim.turn_id}, version {claim.claim_version})"
            )
        yield conn


async def release(conn: AsyncConnection, claim: Claim) -> None:
    """Clear the active turn and lease for this claim. Call inside :func:`fenced`.

    ``claim_version`` is left alone: it only ever grows, so a released claim stays fenced out.
    """
    await conn.execute(
        _RELEASE,
        {"c": claim.conversation_id, "t": claim.turn_id, "v": claim.claim_version},
    )


async def assert_claim(db: Database, claim: Claim) -> None:
    """Raise :class:`ClaimLost` unless the claim is current."""
    async with fenced(db, claim):
        pass


async def pre_send(
    db: Database, claim: Claim, deadline: Deadline, checkpoints: Checkpoints
) -> None:
    """Gate run immediately before every outbound request.

    The DB claim check, the test-only checkpoint, then a local deadline check with no I/O. Callers
    send at once after this returns; nothing may be awaited between the gate and the request.
    """
    await assert_claim(db, claim)
    await checkpoints.hit("after_claim_check")
    if deadline.remaining() <= 0:
        raise DeadlineExpired
