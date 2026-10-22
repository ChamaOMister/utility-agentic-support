"""History assembly (spec §5.4.1).

The unit of history is the complete turn. Selection chooses whole turns (newest first) with a local
size estimate; ``to_request_input`` flattens them into Responses API input items and filters
reasoning; ``check_tool_pairs`` is the pre-send invariant. Nothing here writes to storage, and
``to_request_input`` never mutates its inputs.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from support_api.usage.pricing import ReasoningMode

_MESSAGE_KINDS = frozenset({"user_message", "assistant_message"})
_PAST_STATUSES = ("completed", "interrupted", "failed")


@dataclass(frozen=True)
class Item:
    turn_id: UUID
    seq: int
    kind: str
    call_id: str | None
    model: str | None
    payload: dict


@dataclass(frozen=True)
class TurnItems:
    turn_id: UUID
    status: str
    items: tuple[Item, ...]


class HistoryInvariantError(Exception):
    """The assembled input breaks the tool-call pairing invariant (surfaces as internal_error)."""


def estimate_tokens(item: Item) -> int:
    """Rough local size, used only to choose turns; the provider token count is authoritative."""
    return len(json.dumps(item.payload, ensure_ascii=False)) // 3 + 4


def _emitted_items(turn: TurnItems) -> tuple[Item, ...]:
    """Items of a past turn that reach the request before reasoning filtering.

    Completed turns contribute everything; interrupted/failed turns only their messages, so a
    dangling tool call never reaches the model.
    """
    if turn.status == "completed":
        return turn.items
    return tuple(i for i in turn.items if i.kind in _MESSAGE_KINDS)


async def load_past_turns(
    conn: AsyncConnection, conversation_id: UUID, exclude_turn_id: UUID
) -> list[TurnItems]:
    """Finished turns of the conversation (oldest first) with their items in ``seq`` order. Read-only."""
    turn_rows = (
        await conn.execute(
            text(
                "SELECT id, status FROM support.turns "
                "WHERE conversation_id = :c AND id <> :x AND status = ANY(:s) "
                "ORDER BY created_at, id"
            ),
            {"c": conversation_id, "x": exclude_turn_id, "s": list(_PAST_STATUSES)},
        )
    ).all()
    if not turn_rows:
        return []
    item_rows = (
        await conn.execute(
            text(
                "SELECT turn_id, seq, kind, call_id, model, payload "
                "FROM support.conversation_items "
                "WHERE conversation_id = :c AND turn_id = ANY(:t) "
                "ORDER BY turn_id, seq"
            ),
            {"c": conversation_id, "t": [r.id for r in turn_rows]},
        )
    ).all()
    by_turn: dict[UUID, list[Item]] = {r.id: [] for r in turn_rows}
    for r in item_rows:
        by_turn[r.turn_id].append(
            Item(
                turn_id=r.turn_id,
                seq=r.seq,
                kind=r.kind,
                call_id=r.call_id,
                model=r.model,
                payload=r.payload,
            )
        )
    return [
        TurnItems(turn_id=r.id, status=r.status, items=tuple(by_turn[r.id]))
        for r in turn_rows
    ]


def select_turns(
    past: list[TurnItems],
    *,
    max_turns: int,
    token_budget: int,
    estimate: Callable[[Item], int],
) -> list[TurnItems]:
    """Newest whole turns that fit both bounds, returned oldest first.

    A turn's size is the sum of ``estimate`` over the items ``to_request_input`` would emit for its
    status (all items for completed, messages only for interrupted/failed), before reasoning
    filtering. Accumulation stops at the first turn that would exceed a bound: older turns are never
    kept once a newer one has been dropped.
    """
    kept: list[TurnItems] = []
    used = 0
    for turn in reversed(past):
        if len(kept) >= max_turns:
            break
        size = sum(estimate(i) for i in _emitted_items(turn))
        if used + size > token_budget:
            break
        used += size
        kept.append(turn)
    kept.reverse()
    return kept


def to_request_input(
    turns: list[TurnItems], current: list[Item], *, mode: ReasoningMode, model: str
) -> list[dict]:
    """Responses API input items: past turns in order, then every item of the current turn.

    Reasoning in past turns is sent only for ``all_turns`` and only if produced by ``model``.
    Reasoning in the current turn is sent unless produced by another model.
    """
    out: list[dict] = []
    for turn in turns:
        for item in _emitted_items(turn):
            if item.kind == "reasoning" and (
                mode != "all_turns" or item.model != model
            ):
                continue
            out.append(copy.deepcopy(item.payload))
    for item in current:
        if item.kind == "reasoning" and item.model != model:
            continue
        out.append(copy.deepcopy(item.payload))
    return out


def check_tool_pairs(
    input_items: list[dict], pending_call_ids: frozenset[str] = frozenset()
) -> None:
    """Every output follows a call with its id; every call has a later output unless pending."""
    open_calls: set[str] = set()
    seen_calls: set[str] = set()
    answered: set[str] = set()
    for item in input_items:
        kind = item.get("type")
        if kind == "function_call":
            call_id = item.get("call_id")
            if call_id in seen_calls:
                raise HistoryInvariantError("duplicate function_call id")
            seen_calls.add(call_id)
            open_calls.add(call_id)
        elif kind == "function_call_output":
            call_id = item.get("call_id")
            if call_id not in open_calls:
                if call_id in answered:
                    raise HistoryInvariantError("duplicate function_call_output")
                raise HistoryInvariantError(
                    "function_call_output without a preceding function_call"
                )
            open_calls.discard(call_id)
            answered.add(call_id)
    missing = open_calls - pending_call_ids
    if missing:
        raise HistoryInvariantError("function_call without an output")
