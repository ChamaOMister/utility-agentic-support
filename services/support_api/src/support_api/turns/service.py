"""Message submission: duplicates, serialization, the agent run and fenced completion (spec §5.5).

Steps 1-4 of :func:`submit_message` run in **one** transaction that holds the conversation row
lock from the first statement to the commit, so recovery, the duplicate lookup and the claim never
interleave with another submission. The agent then runs outside any transaction, and step 6 writes
the outcome in one fenced transaction. That step is the only place a non-recovered turn gets its
``assistant_message`` (recovery writes the other one).
"""

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from support_api.agent.core import AgentCore, TurnOutcome
from support_api.catalog import MESSAGES
from support_api.db import Database
from support_api.deadline import Deadline
from support_api.settings import Settings
from support_api.turns.claim import Claim, ClaimLost, fenced, release, try_claim
from support_api.turns.recovery import assistant_payload, recover_conversation

logger = logging.getLogger("support_api.turns")

EMPTY_TRACE: dict[str, list] = {"tool_calls": [], "model_calls": []}
# Final-response items stored before the assistant message. Only reasoning: a final response
# has no calls, so a stray tool item stored here would fail the next turn's pairing check.
_FINAL_ITEM_KINDS = frozenset({"reasoning"})


@dataclass
class SubmitResult:
    http_status: int
    body: dict


@dataclass(frozen=True)
class TurnLimits:
    max_turns: int = 30
    lease_s: float = 60
    deadline_s: float = 45

    @classmethod
    def from_settings(cls, settings: Settings) -> "TurnLimits":
        return cls(settings.max_turns, settings.turn_lease_s, settings.turn_deadline_s)


_LOCK_OWNED = text(
    "SELECT turn_count FROM support.conversations "
    "WHERE id = :c AND browser_session_id = :s FOR UPDATE"
)
_LOCK = text("SELECT 1 FROM support.conversations WHERE id = :c FOR UPDATE")
_FIND = text(
    "SELECT id, status, reply, trace, text_sha256 FROM support.turns "
    "WHERE conversation_id = :c AND client_message_id = :m"
)
_TURN = text("SELECT id, status, reply, trace FROM support.turns WHERE id = :t")
_INSERT_TURN = text(
    "INSERT INTO support.turns "
    "(id, conversation_id, client_message_id, text_sha256, status, reply, trace, "
    " correlation_id, finished_at) "
    "VALUES (:t, :c, :m, :h, :st, :r, CAST(:tr AS jsonb), :cid, "
    " CASE WHEN :st = 'in_progress' THEN NULL ELSE now() END)"
)
_INSERT_ITEM = text(
    "INSERT INTO support.conversation_items "
    "(conversation_id, turn_id, seq, kind, call_id, model, payload) "
    "VALUES (:c, :t, :q, :k, :i, :mo, CAST(:p AS jsonb))"
)
_COUNT_TURN = text(
    "UPDATE support.conversations SET turn_count = turn_count + 1 WHERE id = :c"
)
_NEXT_SEQ = text(
    "SELECT COALESCE(MAX(seq), -1) + 1 FROM support.conversation_items WHERE turn_id = :t"
)
_FINISH = text(
    "UPDATE support.turns SET status = :st, reply = :r, trace = CAST(:tr AS jsonb), "
    "finished_at = now() WHERE id = :t AND status = 'in_progress'"
)


def _in_progress() -> SubmitResult:
    return SubmitResult(
        409, {"error": "turn_in_progress", "message": MESSAGES["turn_in_progress"]}
    )


def _turn_body(row: Row) -> dict[str, Any]:
    return {
        "turn_id": str(row.id),
        "status": row.status,
        "reply": row.reply,
        "trace": row.trace if row.trace is not None else EMPTY_TRACE,
    }


def _user_payload(user_text: str) -> str:
    return json.dumps(
        {"type": "message", "role": "user", "content": user_text}, ensure_ascii=False
    )


async def _insert_item(
    conn: AsyncConnection,
    conversation_id: UUID,
    turn_id: UUID,
    seq: int,
    kind: str,
    payload: str,
    *,
    call_id: str | None = None,
    model: str | None = None,
) -> None:
    await conn.execute(
        _INSERT_ITEM,
        {
            "c": conversation_id,
            "t": turn_id,
            "q": seq,
            "k": kind,
            "i": call_id,
            "mo": model,
            "p": payload,
        },
    )


async def submit_message(
    db: Database,
    core: AgentCore,
    *,
    session_id: UUID,
    conversation_id: UUID,
    client_message_id: UUID,
    text: str,
    correlation_id: str,
    limits: TurnLimits = TurnLimits(),  # noqa: B008 - frozen dataclass
    clock: Callable[[], float] = time.monotonic,
) -> SubmitResult:
    text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    turn_id = uuid4()
    log_ids = {"conversation_id": str(conversation_id), "turn_id": str(turn_id)}

    # Steps 1-4: one transaction holding the conversation row lock until the commit.
    async with db.begin() as conn:
        # 1. Lock, then recover an abandoned turn (no-op unless the active lease expired).
        conv = (
            await conn.execute(_LOCK_OWNED, {"c": conversation_id, "s": session_id})
        ).first()
        if conv is None:
            return SubmitResult(404, {"error": "not_found"})
        await recover_conversation(conn, conversation_id)

        # 2. Duplicate submission rules. Returning commits any recovery done above.
        existing = (
            await conn.execute(_FIND, {"c": conversation_id, "m": client_message_id})
        ).first()
        if existing is not None:
            if existing.text_sha256 != text_sha256:
                return SubmitResult(422, {"error": "client_message_id_conflict"})
            if existing.status == "in_progress":  # its lease is unexpired (see step 1)
                return _in_progress()
            return SubmitResult(200, _turn_body(existing))

        # Turn limit: stored as a failed turn with its own messages; nothing is claimed.
        if conv.turn_count >= limits.max_turns:
            reply = MESSAGES["limit_reached"]
            await conn.execute(
                _INSERT_TURN,
                {
                    "t": turn_id,
                    "c": conversation_id,
                    "m": client_message_id,
                    "h": text_sha256,
                    "st": "failed",
                    "r": reply,
                    "tr": json.dumps(EMPTY_TRACE),
                    "cid": correlation_id,
                },
            )
            await _insert_item(
                conn, conversation_id, turn_id, 0, "user_message", _user_payload(text)
            )
            await _insert_item(
                conn,
                conversation_id,
                turn_id,
                1,
                "assistant_message",
                assistant_payload(reply),
            )
            logger.warning(
                "turn_limit_reached",
                extra={**log_ids, "status": "failed", "error_code": "limit_reached"},
            )
            return SubmitResult(
                200,
                {
                    "turn_id": str(turn_id),
                    "status": "failed",
                    "reply": reply,
                    "trace": EMPTY_TRACE,
                },
            )

        # 3. Claim, insert the turn and its user message, count it.
        claim = await try_claim(conn, conversation_id, turn_id, limits.lease_s)
        if claim is None:
            return _in_progress()
        await conn.execute(
            _INSERT_TURN,
            {
                "t": turn_id,
                "c": conversation_id,
                "m": client_message_id,
                "h": text_sha256,
                "st": "in_progress",
                "r": None,
                "tr": None,
                "cid": correlation_id,
            },
        )
        await _insert_item(
            conn, conversation_id, turn_id, 0, "user_message", _user_payload(text)
        )
        await conn.execute(_COUNT_TURN, {"c": conversation_id})
    # 4. Committed.

    started = clock()
    try:
        # 5. The agent run, outside any transaction, under the turn deadline.
        deadline = Deadline(limits.deadline_s, clock=clock)
        outcome = await core.handle_turn(claim, text, "chat", deadline)
        # 6. One fenced transaction: final items, the assistant message, the turn, release.
        async with fenced(db, claim) as conn:
            await _complete(conn, claim, outcome, core.model_name)
    except ClaimLost:
        logger.warning(
            "turn_claim_lost",
            extra={
                **log_ids,
                "claim_version": claim.claim_version,
                "error_code": "claim_lost",
            },
        )
        return await _stored_after_claim_lost(db, claim)

    logger.info(
        "turn_finished",
        extra={
            **log_ids,
            "claim_version": claim.claim_version,
            "status": outcome.status,
            "duration_ms": round((clock() - started) * 1000),
        },
    )
    return SubmitResult(
        200,
        {
            "turn_id": str(turn_id),
            "status": outcome.status,
            "reply": outcome.reply,
            "trace": outcome.trace,
        },
    )


async def _complete(
    conn: AsyncConnection, claim: Claim, outcome: TurnOutcome, model_name: str
) -> None:
    seq = (await conn.execute(_NEXT_SEQ, {"t": claim.turn_id})).scalar_one()
    for item in outcome.final_items:
        kind = item.get("type")
        if kind not in _FINAL_ITEM_KINDS:
            logger.warning(
                "final_item_skipped",
                extra={"turn_id": str(claim.turn_id), "error_code": "unknown_item"},
            )
            continue
        await _insert_item(
            conn,
            claim.conversation_id,
            claim.turn_id,
            seq,
            kind,
            json.dumps(item, ensure_ascii=False),
            model=model_name,
        )
        seq += 1
    await _insert_item(
        conn,
        claim.conversation_id,
        claim.turn_id,
        seq,
        "assistant_message",
        assistant_payload(outcome.reply),
    )
    await conn.execute(
        _FINISH,
        {
            "t": claim.turn_id,
            "st": outcome.status,
            "r": outcome.reply,
            "tr": json.dumps(outcome.trace, ensure_ascii=False),
        },
    )
    await release(conn, claim)


async def _stored_after_claim_lost(db: Database, claim: Claim) -> SubmitResult:
    """The turn's stored state once its claim is gone (recovering the conversation if due)."""
    async with db.begin() as conn:
        await conn.execute(_LOCK, {"c": claim.conversation_id})
        await recover_conversation(conn, claim.conversation_id)
        row = (await conn.execute(_TURN, {"t": claim.turn_id})).one()
    if (
        row.status == "in_progress"
    ):  # not recovered yet: someone else still owns the lease
        return _in_progress()
    return SubmitResult(200, _turn_body(row))
