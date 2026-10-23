"""Whole-turn selection, recount and reasoning replay through the HTTP API (AC 18)."""

import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from support_api.agent.history import check_tool_pairs
from support_api.catalog import MESSAGES


def message(role, content):
    return {"type": "message", "role": role, "content": content}


def history_turn(marker):
    items = [("user_message", None, message("user", f"[{marker}]"))]
    if marker == "T2":
        for n, model in enumerate(("gpt-6-luna", "other-model")):
            items.append(
                (
                    "reasoning",
                    model,
                    {
                        "type": "reasoning",
                        "id": f"rs_{n}",
                        "summary": [],
                        "encrypted_content": "opaque",
                    },
                )
            )
        for call_id in ("c1", "c2"):
            items.extend(
                [
                    (
                        "function_call",
                        "gpt-6-luna",
                        {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": "list_bills",
                            "arguments": "{}",
                        },
                    ),
                    (
                        "function_call_output",
                        None,
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": "{}",
                        },
                    ),
                ]
            )
    if marker == "T3":
        items.append(
            (
                "function_call",
                "gpt-6-luna",
                {
                    "type": "function_call",
                    "call_id": "c3",
                    "name": "list_bills",
                    "arguments": "{}",
                },
            )
        )
    reply = MESSAGES["turn_interrupted"] if marker == "T3" else "Certo"
    items.append(("assistant_message", "gpt-6-luna", message("assistant", reply)))
    return items


async def seed_history(db, cid, markers):
    async with db.begin() as conn:
        for index, marker in enumerate(markers):
            tid = uuid4()
            await conn.execute(
                text(
                    "INSERT INTO support.turns (id, conversation_id, client_message_id, text_sha256, correlation_id, status, reply, created_at, finished_at) "
                    "VALUES (:t, :c, :m, 'hash', 'history-test', :s, :r, now() - make_interval(secs => :age), now())"
                ),
                {
                    "t": tid,
                    "c": cid,
                    "m": uuid4(),
                    "s": "interrupted" if marker == "T3" else "completed",
                    "r": MESSAGES["turn_interrupted"] if marker == "T3" else "Certo",
                    "age": 10 - index,
                },
            )
            for seq, (kind, model, payload) in enumerate(history_turn(marker)):
                await conn.execute(
                    text(
                        "INSERT INTO support.conversation_items (conversation_id, turn_id, seq, kind, call_id, model, payload) "
                        "VALUES (:c, :t, :q, :k, :i, :m, CAST(:p AS jsonb))"
                    ),
                    {
                        "c": cid,
                        "t": tid,
                        "q": seq,
                        "k": kind,
                        "i": payload.get("call_id"),
                        "m": model,
                        "p": json.dumps(payload),
                    },
                )


async def items_by_id(db, cid):
    async with db.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT id, turn_id, seq, kind, call_id, model, payload FROM support.conversation_items WHERE conversation_id=:c"
                ),
                {"c": cid},
            )
        ).all()
    return {
        r.id: (r.turn_id, r.seq, r.kind, r.call_id, r.model, r.payload) for r in rows
    }


def turn_markers(body):
    return [
        i["content"][1:-1]
        for i in body["input"]
        if i.get("role") == "user" and i.get("content", "").startswith("[T")
    ]


@pytest.fixture
def short_prompt(tmp_path):
    path = tmp_path / "system.txt"
    path.write_text("Responda em português.")
    return path


@pytest.mark.parametrize("mode", ["none", "current_turn", "all_turns"])
async def test_truncation_boundary_with_mocked_provider(
    mode, live_app_factory, db, openai_mock, short_prompt
):
    api = await live_app_factory(
        reasoning_items=mode, max_input_tokens=1000, system_prompt_path=short_prompt
    )
    cid = api.conversation_id
    await seed_history(db, cid, ["T1", "T2", "T3", "T4"])
    before = await items_by_id(db, cid)
    openai_mock.count_returns([1200, 900])
    response = await api.send(uuid4(), "e a minha conta?")
    assert response.json()["status"] == "completed"
    bodies = openai_mock.captured_all("input_tokens")
    assert len(bodies) == 2
    assert turn_markers(bodies[0]) == ["T1", "T2", "T3", "T4"]
    assert turn_markers(bodies[1]) == ["T2", "T3", "T4"]
    assert openai_mock.captured_all("responses")[0]["input"] == bodies[1]["input"]
    for body in bodies + openai_mock.captured_all("responses"):
        check_tool_pairs(body["input"])
        assert not any(i.get("call_id") == "c3" for i in body["input"])
        reasoning = [i for i in body["input"] if i.get("type") == "reasoning"]
        assert len(reasoning) == (1 if mode == "all_turns" else 0)
        if reasoning:
            assert reasoning[0]["id"] == "rs_0"
    after = await items_by_id(db, cid)
    assert before.keys() <= after.keys()
    assert {key: after[key] for key in before} == before
    assert {row[0] for key, row in after.items() if key not in before} == {
        UUID(response.json()["turn_id"])
    }


async def test_dropping_turn_with_tool_exchanges_removes_all_of_it(
    live_app_factory, db, openai_mock, short_prompt
):
    api = await live_app_factory(
        reasoning_items="all_turns",
        max_input_tokens=1000,
        system_prompt_path=short_prompt,
    )
    await seed_history(db, api.conversation_id, ["T2", "T3", "T4"])
    openai_mock.count_returns([1200, 900])
    assert (await api.send(uuid4(), "olá")).json()["status"] == "completed"
    first, second = openai_mock.captured_all("input_tokens")
    assert turn_markers(first) == ["T2", "T3", "T4"]
    assert turn_markers(second) == ["T3", "T4"]
    for body in [second, *openai_mock.captured_all("responses")]:
        assert {"c1", "c2", "c3"}.isdisjoint(i.get("call_id") for i in body["input"])
        assert not any(i.get("type") == "reasoning" for i in body["input"])
        check_tool_pairs(body["input"])


async def test_still_over_after_one_drop_sends_no_generation(
    live_app_factory, db, openai_mock, short_prompt
):
    api = await live_app_factory(max_input_tokens=1000, system_prompt_path=short_prompt)
    await seed_history(db, api.conversation_id, ["T1", "T4"])
    openai_mock.count_returns([1200, 1100])
    response = await api.send(uuid4(), "olá")
    assert response.json()["reply"] == MESSAGES["limit_reached"]
    assert openai_mock.route("input_tokens").call_count == 2
    assert openai_mock.route("responses").call_count == 0


async def test_current_turn_alone_too_large_sends_nothing(
    live_app_factory, openai_mock, short_prompt
):
    api = await live_app_factory(max_input_tokens=200, system_prompt_path=short_prompt)
    response = await api.send(uuid4(), "x" * 2000)
    assert response.json()["reply"] == MESSAGES["limit_reached"]
    assert openai_mock.call_count == 0
