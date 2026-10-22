"""load_past_turns against the real schema: ordering, exclusions, read-only."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from support_api.agent.history import load_past_turns

from tests.conftest import make_conversation

BASE = datetime(2026, 1, 1, tzinfo=UTC)


async def _turn(conn, conversation_id, status, minutes, items):
    turn_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO support.turns (id, conversation_id, client_message_id, text_sha256, "
            "status, correlation_id, created_at) VALUES (:t, :c, :m, 'x', :s, 'corr', :at)"
        ),
        {
            "t": turn_id,
            "c": conversation_id,
            "m": uuid.uuid4(),
            "s": status,
            "at": BASE + timedelta(minutes=minutes),
        },
    )
    # inserted out of seq order on purpose
    for seq, kind, call_id, model, payload in items:
        await conn.execute(
            text(
                "INSERT INTO support.conversation_items "
                "(conversation_id, turn_id, seq, kind, call_id, model, payload) "
                "VALUES (:c, :t, :q, :k, :i, :m, CAST(:p AS jsonb))"
            ),
            {
                "c": conversation_id,
                "t": turn_id,
                "q": seq,
                "k": kind,
                "i": call_id,
                "m": model,
                "p": json.dumps(payload),
            },
        )
    return turn_id


def _msg(role, content):
    return {"type": "message", "role": role, "content": content}


async def _dump(conn, conversation_id):
    rows = await conn.execute(
        text(
            "SELECT id, turn_id, seq, kind, call_id, model, payload "
            "FROM support.conversation_items WHERE conversation_id = :c ORDER BY id"
        ),
        {"c": conversation_id},
    )
    return [tuple(r) for r in rows]


async def test_load_past_turns_orders_excludes_and_never_writes(db):
    conv_id = await make_conversation(db)
    other_conv = await make_conversation(db)
    async with db.begin() as conn:
        newer = await _turn(
            conn, conv_id, "interrupted", 20,
            [(1, "assistant_message", None, "m", _msg("assistant", "b")), (0, "user_message", None, None, _msg("user", "a"))],
        )  # fmt: skip
        older = await _turn(
            conn, conv_id, "completed", 10,
            [
                (2, "function_call_output", "c1", None, {"type": "function_call_output", "call_id": "c1", "output": "{}"}),
                (0, "user_message", None, None, _msg("user", "q")),
                (1, "function_call", "c1", "m", {"type": "function_call", "call_id": "c1", "name": "t", "arguments": "{}"}),
            ],
        )  # fmt: skip
        failed = await _turn(
            conn,
            conv_id,
            "failed",
            30,
            [(0, "user_message", None, None, _msg("user", "f"))],
        )
        await _turn(
            conn,
            conv_id,
            "in_progress",
            40,
            [(0, "user_message", None, None, _msg("user", "ip"))],
        )
        current = await _turn(
            conn,
            conv_id,
            "completed",
            50,
            [(0, "user_message", None, None, _msg("user", "cur"))],
        )
        await _turn(
            conn,
            other_conv,
            "completed",
            5,
            [(0, "user_message", None, None, _msg("user", "other"))],
        )

    async with db.begin() as conn:
        before = await _dump(conn, conv_id)
        turns = await load_past_turns(conn, conv_id, current)
        after = await _dump(conn, conv_id)

    assert [t.turn_id for t in turns] == [older, newer, failed]
    assert [t.status for t in turns] == ["completed", "interrupted", "failed"]
    assert [[i.seq for i in t.items] for t in turns] == [[0, 1, 2], [0, 1], [0]]
    assert turns[0].items[1].call_id == "c1"
    assert turns[0].items[1].model == "m"
    assert turns[0].items[0].payload == _msg("user", "q")
    assert all(i.turn_id == t.turn_id for t in turns for i in t.items)
    assert before == after
