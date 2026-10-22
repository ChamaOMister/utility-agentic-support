"""Recovery of abandoned turns (spec §5.5).

A turn is abandoned when the conversation's active turn has an expired lease. Recovery runs in the
caller's transaction, which already holds the conversation row lock, and follows the five spec
steps in order. It runs lazily whenever a request touches such a conversation (message submit,
duplicate replay, transcript read, a worker that lost its claim) and once at startup
(:func:`startup_sweep`).
"""

import json
import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from support_api.catalog import MESSAGES
from support_api.db import Database
from support_api.usage.budget import recover_reservations

logger = logging.getLogger("support_api.turns")

_ACTIVE = text(
    "SELECT active_turn_id, "
    "COALESCE(active_turn_lease_expires_at <= now(), true) AS expired "
    "FROM support.conversations WHERE id = :c FOR UPDATE"
)
_FENCE_OLD_WORKER = text(
    "UPDATE support.conversations SET claim_version = claim_version + 1 "
    "WHERE id = :c RETURNING claim_version"
)
_TOOL_TRACE = text(
    "SELECT name, status, error_code, attempts, duration_ms, record_count "
    "FROM support.tool_calls WHERE turn_id = :t ORDER BY created_at, id"
)
_INTERRUPT = text(
    "UPDATE support.turns SET status = 'interrupted', reply = :r, finished_at = now(), "
    "trace = CAST(:tr AS jsonb) "
    "WHERE id = :t AND conversation_id = :c AND status = 'in_progress' RETURNING id"
)
_NEXT_SEQ = text(
    "SELECT COALESCE(MAX(seq), -1) + 1 FROM support.conversation_items WHERE turn_id = :t"
)
_INSERT_ASSISTANT = text(
    "INSERT INTO support.conversation_items (conversation_id, turn_id, seq, kind, payload) "
    "VALUES (:c, :t, :q, 'assistant_message', CAST(:p AS jsonb))"
)
_ABANDON_ATTEMPTS = text(
    "UPDATE support.verification_attempts SET status = 'abandoned' "
    "WHERE turn_id = :t AND status = 'in_flight'"
)
_CLEAR_ACTIVE = text(
    "UPDATE support.conversations "
    "SET active_turn_id = NULL, active_turn_lease_expires_at = NULL WHERE id = :c"
)
_EXPIRED = text(
    "SELECT id FROM support.conversations "
    "WHERE active_turn_id IS NOT NULL AND active_turn_lease_expires_at <= now()"
)


def assistant_payload(reply: str) -> str:
    return json.dumps(
        {"type": "message", "role": "assistant", "content": reply}, ensure_ascii=False
    )


async def recover_conversation(conn: AsyncConnection, conversation_id: UUID) -> bool:
    """Recover the conversation's abandoned turn, if any. The caller holds the row lock.

    Returns ``True`` when a turn was recovered, ``False`` when there is no active turn or its lease
    has not expired (nothing is written then).
    """
    # 1. Confirm the lease is still expired; increment claim_version (fences the old worker).
    row = (await conn.execute(_ACTIVE, {"c": conversation_id})).first()
    if row is None or row.active_turn_id is None or not row.expired:
        return False
    turn_id = row.active_turn_id
    version = (
        await conn.execute(_FENCE_OLD_WORKER, {"c": conversation_id})
    ).scalar_one()

    # 2. Mark the turn interrupted with the catalog reply, stored as its assistant_message too.
    reply = MESSAGES["turn_interrupted"]
    tool_calls = [
        dict(r._mapping) for r in await conn.execute(_TOOL_TRACE, {"t": turn_id})
    ]
    trace = json.dumps({"tool_calls": tool_calls, "model_calls": []})
    interrupted = (
        await conn.execute(
            _INTERRUPT, {"t": turn_id, "c": conversation_id, "r": reply, "tr": trace}
        )
    ).first()
    if interrupted is not None:
        seq = (await conn.execute(_NEXT_SEQ, {"t": turn_id})).scalar_one()
        await conn.execute(
            _INSERT_ASSISTANT,
            {
                "c": conversation_id,
                "t": turn_id,
                "q": seq,
                "p": assistant_payload(reply),
            },
        )

    # 3. Reserved provider calls may have been sent: uncertain, live generation blocked.
    await recover_reservations(conn, conversation_id, turn_id)
    # 4. Free the turn's verification slots without counting failures.
    await conn.execute(_ABANDON_ATTEMPTS, {"t": turn_id})
    # 5. Clear the active turn; a new message may claim in the same transaction.
    await conn.execute(_CLEAR_ACTIVE, {"c": conversation_id})

    logger.warning(
        "turn_recovered",
        extra={
            "conversation_id": str(conversation_id),
            "turn_id": str(turn_id),
            "claim_version": version,
            "status": "interrupted",
        },
    )
    return True


async def startup_sweep(db: Database) -> int:
    """Recover every conversation whose active turn has an expired lease (one transaction each)."""
    async with db.begin() as conn:
        candidates = list((await conn.execute(_EXPIRED)).scalars())
    recovered = 0
    for conversation_id in candidates:
        async with db.begin() as conn:
            if await recover_conversation(conn, conversation_id):
                recovered += 1
    logger.info("startup_sweep", extra={"record_count": recovered})
    return recovered
