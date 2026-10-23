"""History assembly (spec §5.4.1): whole-turn selection, reasoning filtering, tool-pair invariant."""

from __future__ import annotations

import uuid
from copy import deepcopy

import pytest
from support_api.agent.history import (
    HistoryInvariantError,
    Item,
    TurnItems,
    check_tool_pairs,
    estimate_tokens,
    select_turns,
    to_request_input,
)
from support_api.catalog import MESSAGES

INTERRUPTED = MESSAGES["turn_interrupted"]


def _item(turn_id, seq, kind, payload, *, call_id=None, model=None) -> Item:
    return Item(
        turn_id=turn_id,
        seq=seq,
        kind=kind,
        call_id=call_id,
        model=model,
        payload=payload,
    )


def user(text, turn_id=None, seq=0) -> Item:
    payload = {"type": "message", "role": "user", "content": text}
    return _item(turn_id or uuid.uuid4(), seq, "user_message", payload)


def assistant(text, turn_id, seq) -> Item:
    payload = {"type": "message", "role": "assistant", "content": text}
    return _item(turn_id, seq, "assistant_message", payload, model="m")


def reasoning(model, turn_id=None, seq=0) -> Item:
    payload = {"type": "reasoning", "id": f"rs_{model}", "encrypted_content": "opaque"}
    return _item(turn_id or uuid.uuid4(), seq, "reasoning", payload, model=model)


def call(call_id, turn_id=None, seq=0) -> Item:
    payload = {
        "type": "function_call",
        "call_id": call_id,
        "name": "t",
        "arguments": "{}",
    }
    return _item(
        turn_id or uuid.uuid4(),
        seq,
        "function_call",
        payload,
        call_id=call_id,
        model="m",
    )


def output(call_id, turn_id=None, seq=0) -> Item:
    payload = {"type": "function_call_output", "call_id": call_id, "output": "{}"}
    return _item(
        turn_id or uuid.uuid4(), seq, "function_call_output", payload, call_id=call_id
    )


def call_dict(call_id) -> dict:
    return {"type": "function_call", "call_id": call_id, "name": "t", "arguments": "{}"}


def out_dict(call_id) -> dict:
    return {"type": "function_call_output", "call_id": call_id, "output": "{}"}


def _turn(status, build) -> TurnItems:
    tid = uuid.uuid4()
    return TurnItems(turn_id=tid, status=status, items=tuple(build(tid)))


def conv() -> list[TurnItems]:
    def t1(t):
        return [user("oi", t, 0), assistant("olá", t, 1)]

    def t2(t):
        return [
            user("minhas faturas", t, 0),
            reasoning("m", t, 1),
            call("c1", t, 2),
            output("c1", t, 3),
            reasoning("other-model", t, 4),
            call("c2", t, 5),
            output("c2", t, 6),
            assistant("aqui estão", t, 7),
        ]

    def t3(t):
        return [
            user("e a segunda via?", t, 0),
            call("c3", t, 1),
            assistant(INTERRUPTED, t, 2),
        ]

    return [
        _turn("completed", t1),
        _turn("completed", t2),
        _turn("interrupted", t3),
        _turn("completed", t1),
    ]


def kinds(items: list[dict]) -> list[str]:
    return [i["type"] for i in items]


def texts(items: list[dict]) -> list[str]:
    return [i["content"] for i in items if i.get("type") == "message"]


def size_of(turns: list[TurnItems]) -> int:
    """Same rule as select_turns: estimate over the items to_request_input emits for the status."""
    total = 0
    for t in turns:
        for i in t.items:
            if t.status == "completed" or i.kind in (
                "user_message",
                "assistant_message",
            ):
                total += estimate_tokens(i)
    return total


@pytest.mark.parametrize("mode", ["none", "current_turn", "all_turns"])
def test_no_orphans_and_interrupted_tools_omitted(mode):
    items = to_request_input(
        conv(), current=[user("e a conta de luz?")], mode=mode, model="m"
    )
    check_tool_pairs(items)
    assert not any(i.get("call_id") == "c3" for i in items)
    assert texts(items).count(INTERRUPTED) == 1
    assert (
        INTERRUPTED
        == "Não foi possível concluir sua mensagem. Envie novamente para tentar outra vez."
    )


def test_failed_turn_contributes_only_messages():
    t = _turn(
        "failed",
        lambda t: [
            user("a", t, 0),
            reasoning("m", t, 1),
            call("cf", t, 2),
            assistant("x", t, 3),
        ],
    )
    items = to_request_input([t], [], mode="all_turns", model="m")
    assert kinds(items) == ["message", "message"]


def test_reasoning_filtering_by_mode():
    assert (
        kinds(to_request_input(conv(), [user("x")], mode="none", model="m")).count(
            "reasoning"
        )
        == 0
    )
    assert (
        kinds(
            to_request_input(conv(), [user("x")], mode="current_turn", model="m")
        ).count("reasoning")
        == 0
    )
    assert (
        kinds(to_request_input(conv(), [user("x")], mode="all_turns", model="m")).count(
            "reasoning"
        )
        == 1
    )


def test_current_turn_reasoning_kept_in_current_turn_mode():
    current = [user("x"), reasoning("m"), call("c9"), output("c9")]
    assert (
        kinds(to_request_input([], current, mode="current_turn", model="m")).count(
            "reasoning"
        )
        == 1
    )


def test_current_turn_reasoning_from_other_model_dropped():
    current = [user("x"), reasoning("other-model")]
    for mode in ("none", "current_turn", "all_turns"):
        assert (
            kinds(to_request_input([], current, mode=mode, model="m")).count(
                "reasoning"
            )
            == 0
        )


def test_current_turn_reasoning_filtered_only_by_model():
    # Items present in the current turn are replayed whatever the mode (the mode governs past turns).
    current = [user("x"), reasoning("m")]
    for mode in ("none", "current_turn", "all_turns"):
        assert (
            kinds(to_request_input([], current, mode=mode, model="m")).count(
                "reasoning"
            )
            == 1
        )


def test_order_is_past_turns_then_current():
    items = to_request_input(conv()[:1], [user("agora")], mode="none", model="m")
    assert texts(items) == ["oi", "olá", "agora"]


def test_to_request_input_is_pure():
    turns, current = conv(), [user("x"), reasoning("m")]
    before = deepcopy(([t.items for t in turns], current))
    items = to_request_input(turns, current, mode="all_turns", model="m")
    assert ([t.items for t in turns], current) == before
    items[0]["content"] = "mutated"
    assert turns[0].items[0].payload["content"] == "oi"


def test_truncation_drops_whole_oldest_turns():
    c = conv()
    kept = select_turns(
        c, max_turns=6, token_budget=size_of(c[1:]), estimate=estimate_tokens
    )
    assert [t.turn_id for t in kept] == [t.turn_id for t in c[1:]]
    kept = select_turns(
        c, max_turns=6, token_budget=size_of(c[2:]), estimate=estimate_tokens
    )
    assert [t.turn_id for t in kept] == [t.turn_id for t in c[2:]]
    ids = {
        i.get("call_id")
        for i in to_request_input(kept, [user("x")], mode="all_turns", model="m")
    }
    assert {"c1", "c2"}.isdisjoint(ids)
    assert (
        kinds(to_request_input(kept, [user("x")], mode="all_turns", model="m")).count(
            "reasoning"
        )
        == 0
    )


def test_truncation_never_keeps_older_turn_after_skipping_newer():
    c = conv()
    kept = select_turns(
        c, max_turns=6, token_budget=size_of(c[3:]) - 1, estimate=estimate_tokens
    )
    assert kept == []


def test_older_small_turn_not_kept_after_newer_large_turn_dropped():
    def small(t):
        return [user("oi", t, 0), assistant("ok", t, 1)]

    def large(t):
        return [user("x" * 600, t, 0), assistant("y" * 600, t, 1)]

    t1, t2, t3 = (
        _turn("completed", small),
        _turn("completed", large),
        _turn("completed", small),
    )
    past = [t1, t2, t3]
    budget = size_of([t3]) + size_of([t1])  # fits T3 and T1, but not T3 + T2
    assert size_of([t3, t2]) > budget
    kept = select_turns(
        past, max_turns=6, token_budget=budget, estimate=estimate_tokens
    )
    assert [t.turn_id for t in kept] == [t3.turn_id]


def test_max_turns_bound():
    past = conv() * 3
    kept = select_turns(past, max_turns=6, token_budget=10**9, estimate=estimate_tokens)
    assert len(kept) == 6
    assert kept == past[-6:]


def test_estimate_tokens_formula():
    i = user("ção")
    assert (
        estimate_tokens(i)
        == len('{"type": "message", "role": "user", "content": "ção"}') // 3 + 4
    )


def test_check_tool_pairs_rejects_orphans():
    with pytest.raises(HistoryInvariantError):
        check_tool_pairs([out_dict("cX")])
    with pytest.raises(HistoryInvariantError):
        check_tool_pairs([call_dict("cY")])
    check_tool_pairs([call_dict("cY")], pending_call_ids=frozenset({"cY"}))


def test_check_tool_pairs_order_and_duplicates():
    check_tool_pairs([call_dict("a"), out_dict("a"), call_dict("b"), out_dict("b")])
    with pytest.raises(HistoryInvariantError):
        check_tool_pairs([out_dict("a"), call_dict("a")])
    with pytest.raises(HistoryInvariantError):
        check_tool_pairs([call_dict("a"), call_dict("a"), out_dict("a")])
    with pytest.raises(HistoryInvariantError):
        check_tool_pairs([call_dict("a"), out_dict("a"), out_dict("a")])
    # a pending id that already has an output is fine; an unrelated pending id does not excuse another
    check_tool_pairs([call_dict("a"), out_dict("a")], pending_call_ids=frozenset({"a"}))
    with pytest.raises(HistoryInvariantError):
        check_tool_pairs([call_dict("a")], pending_call_ids=frozenset({"z"}))
