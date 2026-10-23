"""Real SDK + mocked HTTP + real ledger (AC 13, 16, 17, 20)."""

import asyncio
import json
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import httpx
import openai
import pytest
from sqlalchemy import text
from support_api.agent.models.base import ModelContext, ModelRequest, ModelUnavailable
from support_api.catalog import MESSAGES
from support_api.deadline import Deadline
from support_api.turns.claim import ClaimLost

from tests.conftest import (
    expire_lease,
    live_blocked_reason,
    llm_calls,
    only_llm_call,
    provider_response,
    take_over,
)


@pytest.fixture
async def model(db, verified_live_config, openai_mock):
    from support_api.agent.models.openai_model import OpenAIResponsesModel

    client = openai.AsyncOpenAI(
        api_key="sk-test-not-real", max_retries=0, http_client=httpx.AsyncClient()
    )
    model = OpenAIResponsesModel(
        client,
        db=db,
        model="gpt-6-luna",
        tier="default",
        rate=verified_live_config[2].rate("gpt-6-luna", "default"),
        max_output_tokens=800,
        pricing=verified_live_config[2],
        conversation_cap_usd=Decimal("0.10"),
    )
    yield model
    await model.aclose()


@pytest.fixture
def ctx(claimed_conversation, fake_clock, checkpoint_hub):
    return ModelContext(
        claimed_conversation, Deadline(45, fake_clock), checkpoint_hub, []
    )


@pytest.fixture
def request_body():
    return ModelRequest(
        "Responda em português.",
        ({"type": "message", "role": "user", "content": "olá"},),
        (),
    )


async def generate(model, request, ctx):
    await model.count_input_tokens(request, ctx)
    return await model.generate(request, ctx)


def error_response(status, *, error_type=None, code=None, retry_after=None):
    return httpx.Response(
        status,
        json={
            "error": {"type": error_type, "code": code, "message": "synthetic error"}
        },
        headers={
            "x-request-id": "req_error",
            **({"Retry-After": retry_after} if retry_after else {}),
        },
    )


async def test_count_and_generation_share_fields(
    model, request_body, ctx, openai_mock, db
):
    response = await generate(model, request_body, ctx)
    count = openai_mock.captured_all("input_tokens")[0]
    gen = openai_mock.captured_all("responses")[0]
    for field in ("model", "instructions", "input", "tools", "tool_choice"):
        assert count[field] == gen[field]
    assert count["tool_choice"] == "auto"
    assert not {"max_output_tokens", "store", "service_tier", "include"} & count.keys()
    assert gen["max_output_tokens"] == 800 and gen["store"] is False
    assert gen["service_tier"] == "default" and gen["include"] == [
        "reasoning.encrypted_content"
    ]
    assert response.text == "Certo!" and response.status == "completed"
    call = await only_llm_call(db, ctx.claim)
    assert call.status == "settled" and call.reserved_usd == Decimal("0.008125")
    assert call.estimated_usd == Decimal(
        "0.000203"
    )  # 80*1.25 + 20*.125 + 10*10, /1m, ceil6
    assert (call.input_tokens, call.output_tokens) == (100, 10)
    assert call.outcome_class == "settled" and call.provider_request_id == "req_test"
    assert call.latency_ms >= 0
    assert await llm_calls(db, ctx.claim, kind="token_count") == []


@pytest.mark.parametrize(
    "failure", ["500", "timeout", "malformed", "negative", "boolean"]
)
async def test_count_failure_sends_no_generation(
    api_live_mocked, openai_mock, db, failure
):
    route = openai_mock.route("input_tokens")
    if failure == "500":
        route.mock(return_value=error_response(500))
    elif failure == "timeout":
        route.mock(side_effect=httpx.ReadTimeout("count timeout"))
    else:
        route.respond(
            200,
            json={
                "object": "response.input_tokens",
                **(
                    {}
                    if failure == "malformed"
                    else {"input_tokens": -1 if failure == "negative" else True}
                ),
            },
        )
    result = await api_live_mocked.send(uuid4(), "olá")
    assert result.json()["reply"] == MESSAGES["upstream_unavailable"]
    assert route.call_count == 1 and openai_mock.route("responses").call_count == 0
    assert await llm_calls(db, api_live_mocked.conversation_id) == []


async def test_count_time_charged_to_turn_deadline(
    model, request_body, ctx, openai_mock, fake_clock
):
    def count(request):
        assert request.extensions["timeout"]["read"] == 10
        fake_clock.advance(40)
        return httpx.Response(200, json={"input_tokens": 100})

    def respond(request):
        assert request.extensions["timeout"]["read"] == 5
        return httpx.Response(200, json=provider_response())

    openai_mock.route("input_tokens").mock(side_effect=count)
    openai_mock.route("responses").mock(side_effect=respond)
    await generate(model, request_body, ctx)
    assert ctx.deadline.remaining() == 5


@pytest.mark.parametrize(
    "status,error_type,code,reason",
    [
        (400, None, None, "provider_rejected"),
        (401, None, None, "provider_rejected"),
        (403, None, None, "provider_rejected"),
        (404, None, None, "provider_rejected"),
        (429, "insufficient_quota", None, "provider_quota"),
        (429, None, None, "provider_quota"),
        (500, None, None, "provider_outcome_unknown"),
    ],
)
async def test_no_retry_rows(
    model, request_body, ctx, openai_mock, db, status, error_type, code, reason
):
    openai_mock.route("responses").mock(
        return_value=error_response(status, error_type=error_type, code=code)
    )
    with pytest.raises(ModelUnavailable):
        await generate(model, request_body, ctx)
    call = await only_llm_call(db, ctx.claim)
    assert call.status == "uncertain" and call.reserved_usd > 0
    assert call.provider_request_id == "req_error" and call.latency_ms >= 0
    assert await live_blocked_reason(db, ctx.claim) == reason
    assert openai_mock.route("responses").call_count == 1


async def test_transient_429_retry_within_deadline(
    model, request_body, ctx, openai_mock, db
):
    openai_mock.route("responses").mock(
        side_effect=[
            error_response(429, error_type="rate_limit_error", retry_after="1"),
            httpx.Response(200, json=provider_response()),
        ]
    )
    await generate(model, request_body, ctx)
    rows = await llm_calls(db, ctx.claim)
    assert [r.status for r in rows] == ["uncertain", "settled"]
    assert [r.attempt for r in rows] == [1, 2] and rows[0].id != rows[1].id
    assert await live_blocked_reason(db, ctx.claim) is None
    assert openai_mock.route("responses").call_count == 2


async def test_transient_429_beyond_deadline_not_retried(
    model, request_body, ctx, openai_mock, db
):
    openai_mock.route("responses").mock(
        return_value=error_response(429, code="slow_down", retry_after="60")
    )
    with pytest.raises(ModelUnavailable, match="upstream_unavailable"):
        await generate(model, request_body, ctx)
    assert (await only_llm_call(db, ctx.claim)).status == "uncertain"
    assert await live_blocked_reason(db, ctx.claim) is None
    assert openai_mock.route("responses").call_count == 1


@pytest.mark.parametrize(
    "error", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout]
)
async def test_connect_error_released_and_retried(
    model, request_body, ctx, openai_mock, db, error
):
    openai_mock.route("responses").mock(
        side_effect=[
            error("before send"),
            httpx.Response(200, json=provider_response()),
        ]
    )
    await generate(model, request_body, ctx)
    rows = await llm_calls(db, ctx.claim)
    assert [r.status for r in rows] == ["released", "settled"]
    assert rows[0].outcome_class == "connect_failed"
    assert await live_blocked_reason(db, ctx.claim) is None


async def test_connect_failure_retried_only_once(
    model, request_body, ctx, openai_mock, db
):
    openai_mock.route("responses").mock(side_effect=httpx.ConnectError("before send"))
    with pytest.raises(ModelUnavailable):
        await generate(model, request_body, ctx)
    assert [r.status for r in await llm_calls(db, ctx.claim)] == [
        "released",
        "released",
    ]
    assert openai_mock.route("responses").call_count == 2


@pytest.mark.parametrize(
    "scenario", ["timeout", "interrupted_read", "no_usage", "malformed", "bad_usage"]
)
async def test_unknown_outcome_uncertain_and_blocks(
    model, request_body, ctx, openai_mock, db, scenario
):
    route = openai_mock.route("responses")
    if scenario in ("timeout", "interrupted_read"):
        route.mock(
            side_effect=(
                httpx.ReadTimeout("read timeout")
                if scenario == "timeout"
                else httpx.RemoteProtocolError("read interrupted")
            )
        )
    else:
        body = provider_response(usage=False) if scenario == "no_usage" else {}
        if scenario == "bad_usage":
            body = provider_response()
            body["usage"]["input_tokens"] = -1
        route.respond(200, json=body)
    with pytest.raises(ModelUnavailable):
        await generate(model, request_body, ctx)
    assert route.call_count == 1
    call = await only_llm_call(db, ctx.claim)
    assert call.status == "uncertain" and call.estimated_usd is None
    assert await live_blocked_reason(db, ctx.claim) == "provider_outcome_unknown"


async def test_unpriced_service_tier_in_response_blocks(
    model, request_body, ctx, openai_mock, db
):
    openai_mock.route("responses").respond(
        200, json=provider_response(service_tier="priority")
    )
    with pytest.raises(ModelUnavailable):
        await generate(model, request_body, ctx)
    assert (await only_llm_call(db, ctx.claim)).status == "uncertain"
    assert await live_blocked_reason(db, ctx.claim) == "unpriced_tier"


async def test_actual_priced_service_tier_used(
    model, request_body, ctx, openai_mock, db, verified_live_config
):
    verified_live_config[0]["models"]["gpt-6-luna"]["priority"] = {
        **verified_live_config[0]["models"]["gpt-6-luna"]["default"],
        "input_per_mtok": 2,
        "cached_input_per_mtok": 1,
        "output_per_mtok": 20,
    }
    openai_mock.route("responses").respond(
        200, json=provider_response(service_tier="priority")
    )
    await generate(model, request_body, ctx)
    call = await only_llm_call(db, ctx.claim)
    assert call.status == "settled" and call.service_tier == "priority"
    assert call.estimated_usd == Decimal("0.000380")


async def test_incomplete_with_usage_settles_and_returns_limit(
    api_live_mocked, openai_mock, db
):
    openai_mock.route("responses").respond(
        200, json=provider_response(status="incomplete")
    )
    result = await api_live_mocked.send(uuid4(), "olá")
    assert result.json()["reply"] == MESSAGES["limit_reached"]
    assert (
        await only_llm_call(db, api_live_mocked.conversation_id)
    ).status == "settled"


async def test_count_after_takeover_sends_nothing(
    model, request_body, ctx, openai_mock, db
):
    await take_over(db, ctx.claim)
    with pytest.raises(ClaimLost):
        await model.count_input_tokens(request_body, ctx)
    assert openai_mock.call_count == 0


async def test_retry_after_takeover_not_sent(model, request_body, ctx, openai_mock, db):
    async def fail(request):
        await take_over(db, ctx.claim)
        raise httpx.ConnectError("before send", request=request)

    openai_mock.route("responses").mock(side_effect=fail)
    with pytest.raises(ClaimLost):
        await generate(model, request_body, ctx)
    assert openai_mock.route("responses").call_count == 1
    assert (await only_llm_call(db, ctx.claim)).status == "reserved"


async def test_usage_settles_after_takeover(model, request_body, ctx, openai_mock, db):
    async def respond(request):
        await take_over(db, ctx.claim)
        return httpx.Response(200, json=provider_response())

    openai_mock.route("responses").mock(side_effect=respond)
    await generate(model, request_body, ctx)
    assert (await only_llm_call(db, ctx.claim)).status == "settled"


async def test_expired_pre_send_releases_unsent_reservation(
    model, request_body, ctx, openai_mock, db, fake_clock, pause_at
):
    await model.count_input_tokens(request_body, ctx)
    gate = pause_at("after_reserve")
    task = asyncio.create_task(model.generate(request_body, ctx))
    await asyncio.wait_for(gate.reached.wait(), 10)
    fake_clock.advance(46)
    gate.release()
    with pytest.raises(ModelUnavailable, match="limit_reached"):
        await asyncio.wait_for(task, 10)
    assert (await only_llm_call(db, ctx.claim)).status == "released"
    assert openai_mock.route("responses").call_count == 0


@pytest.mark.parametrize(
    "checkpoint,nth", [("after_reserve", 1), ("after_claim_check", 2)]
)
async def test_takeover_before_send_sends_no_generation(
    api_live_mocked, db, pause_at, fake_clock, checkpoint, nth
):
    api = api_live_mocked
    gate = pause_at(checkpoint, nth=nth)
    a = asyncio.create_task(api.send(uuid4(), "minhas faturas"))
    await asyncio.wait_for(gate.reached.wait(), 10)
    fake_clock.advance(61)
    await expire_lease(db, api.conversation_id)
    b = await api.send(uuid4(), "outra pergunta")
    gate.release()
    assert (await asyncio.wait_for(a, 10)).json()["status"] == "interrupted"
    assert b.json()["reply"] == MESSAGES["live_blocked"]
    assert api.openai_routes["responses"].call_count == 0
    call = await only_llm_call(db, api.conversation_id)
    assert call.status == "uncertain" and call.reserved_usd > 0


async def test_generate_requires_matching_successful_count(
    model, request_body, ctx, openai_mock, db
):
    with pytest.raises(ModelUnavailable):
        await model.generate(request_body, ctx)
    await model.count_input_tokens(request_body, ctx)
    with pytest.raises(ModelUnavailable):
        await model.generate(replace(request_body, instructions="Changed"), ctx)
    openai_mock.route("input_tokens").mock(return_value=error_response(500))
    with pytest.raises(ModelUnavailable):
        await model.count_input_tokens(request_body, ctx)
    with pytest.raises(ModelUnavailable):
        await model.generate(request_body, ctx)
    assert openai_mock.route("responses").call_count == 0
    assert await llm_calls(db, ctx.claim) == []


async def test_tool_call_round_trip_and_usage_settlement(
    api_live_mocked, openai_mock, db
):
    reasoning = {
        "id": "rs_test",
        "type": "reasoning",
        "summary": [],
        "encrypted_content": "opaque",
    }
    call = {
        "id": "fc_test",
        "type": "function_call",
        "call_id": "c1",
        "name": "list_bills",
        "arguments": "{}",
        "status": "completed",
    }
    openai_mock.route("responses").mock(
        side_effect=[
            httpx.Response(200, json=provider_response(output=[reasoning, call])),
            httpx.Response(200, json=provider_response()),
        ]
    )
    api = api_live_mocked
    result = (await api.send(uuid4(), "minhas faturas")).json()
    assert result["status"] == "completed" and result["reply"] == "Certo!"
    assert result["trace"]["tool_calls"][0]["error_code"] == "authentication_required"
    second = openai_mock.captured_all("responses")[1]["input"]
    assert next(i for i in second if i["type"] == "reasoning") == reasoning
    output = next(i for i in second if i["type"] == "function_call_output")
    assert (
        output["call_id"] == "c1"
        and json.loads(output["output"])["status"] == "authentication_required"
    )
    assert [r.status for r in await llm_calls(db, api.conversation_id)] == [
        "settled",
        "settled",
    ]
    async with db.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT kind, payload FROM support.conversation_items WHERE turn_id=:t ORDER BY seq"
                ),
                {"t": result["turn_id"]},
            )
        ).all()
    assert [r.kind for r in rows] == [
        "user_message",
        "reasoning",
        "function_call",
        "function_call_output",
        "assistant_message",
    ]
    assert rows[1].payload == reasoning


async def test_malformed_429_metadata_is_uncertain_without_retry(
    model, request_body, ctx, openai_mock, db
):
    openai_mock.route("responses").respond(
        429, json={"error": {"type": [], "code": {}}}
    )
    with pytest.raises(ModelUnavailable):
        await generate(model, request_body, ctx)
    assert (await only_llm_call(db, ctx.claim)).status == "uncertain"
    assert await live_blocked_reason(db, ctx.claim) == "provider_quota"
    assert openai_mock.route("responses").call_count == 1


@pytest.mark.parametrize("phase", ["input_tokens", "responses"])
async def test_total_wall_clock_timeout_bounds_provider_request(
    model, request_body, ctx, openai_mock, db, phase
):
    async def stalled(request):
        await asyncio.sleep(1)
        return httpx.Response(
            200,
            json=provider_response() if phase == "responses" else {"input_tokens": 100},
        )

    model._count_timeout_s = model._attempt_timeout_s = 0.02
    openai_mock.route(phase).mock(side_effect=stalled)
    with pytest.raises(ModelUnavailable, match="upstream_unavailable"):
        await asyncio.wait_for(generate(model, request_body, ctx), 0.5)
    if phase == "responses":
        assert (await only_llm_call(db, ctx.claim)).status == "uncertain"
        assert await live_blocked_reason(db, ctx.claim) == "provider_outcome_unknown"
    else:
        assert await llm_calls(db, ctx.claim) == []
        assert openai_mock.route("responses").call_count == 0


async def test_rate_limit_backoff_takeover_prevents_retry(
    model, request_body, ctx, openai_mock, db
):
    takeover_task = None

    async def take_over_after_response():
        await asyncio.sleep(0.03)
        await take_over(db, ctx.claim)

    def limited(request):
        nonlocal takeover_task
        takeover_task = asyncio.create_task(take_over_after_response())
        return error_response(429, error_type="rate_limit_error", retry_after="0.2")

    openai_mock.route("responses").mock(side_effect=limited)
    with pytest.raises(ClaimLost):
        await generate(model, request_body, ctx)
    await asyncio.wait_for(takeover_task, 10)
    assert (await only_llm_call(db, ctx.claim)).status == "uncertain"
    assert openai_mock.route("responses").call_count == 1


async def test_expired_count_gate_sends_nothing(
    model, request_body, ctx, openai_mock, fake_clock
):
    fake_clock.advance(46)
    with pytest.raises(ModelUnavailable, match="limit_reached"):
        await model.count_input_tokens(request_body, ctx)
    assert openai_mock.call_count == 0


async def test_tool_round_trip_through_billing(
    api_live_mocked, openai_mock, personas, billing_mock, db
):
    persona = personas[0]
    auth = {
        "type": "function_call",
        "call_id": "auth",
        "name": "authenticate_customer",
        "arguments": json.dumps(
            {
                "identifier": persona["cpf"],
                "birth_date": persona["birth_date"],
                "phone_last4": persona["phone"][-4:],
            }
        ),
    }
    bills = {
        "type": "function_call",
        "call_id": "bills",
        "name": "list_bills",
        "arguments": "{}",
    }
    openai_mock.route("responses").mock(
        side_effect=[
            httpx.Response(200, json=provider_response(output=[auth])),
            httpx.Response(200, json=provider_response(output=[bills])),
            httpx.Response(200, json=provider_response()),
        ]
    )
    response = await api_live_mocked.send(uuid4(), "minhas faturas")
    assert response.json()["status"] == "completed"
    assert [t["status"] for t in response.json()["trace"]["tool_calls"]] == ["ok", "ok"]
    assert (
        billing_mock["verify"].call_count == billing_mock["list_bills"].call_count == 1
    )
    last_input = openai_mock.captured_all("responses")[-1]["input"]
    output = next(
        i
        for i in last_input
        if i.get("call_id") == "bills" and i["type"] == "function_call_output"
    )
    assert json.loads(output["output"])["status"] == "ok"
    assert len(json.loads(output["output"])["untrusted_data"]) == len(persona["bills"])
    assert [r.status for r in await llm_calls(db, api_live_mocked.conversation_id)] == [
        "settled"
    ] * 3


@pytest.mark.parametrize(
    "malformed", ["missing_output", "invalid_status", "invalid_call"]
)
async def test_malformed_response_with_reported_usage_settles_and_blocks(
    model, request_body, ctx, openai_mock, db, malformed
):
    body = provider_response()
    if malformed == "missing_output":
        del body["output"]
    elif malformed == "invalid_status":
        body["status"] = "broken"
    else:
        body["output"] = [
            {"type": "function_call", "call_id": "c", "name": "list_bills"}
        ]
    openai_mock.route("responses").respond(200, json=body)
    with pytest.raises(ModelUnavailable, match="upstream_unavailable"):
        await generate(model, request_body, ctx)
    call = await only_llm_call(db, ctx.claim)
    assert call.status == "settled" and call.estimated_usd == Decimal("0.000203")
    assert await live_blocked_reason(db, ctx.claim) == "provider_outcome_unknown"
    assert openai_mock.route("responses").call_count == 1


async def test_zero_usage_does_not_release_unknown_spend(
    model, request_body, ctx, openai_mock, db
):
    body = provider_response()
    body["usage"] = {
        "input_tokens": 0,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 0,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 0,
    }
    openai_mock.route("responses").respond(200, json=body)
    with pytest.raises(ModelUnavailable):
        await generate(model, request_body, ctx)
    call = await only_llm_call(db, ctx.claim)
    assert call.status == "uncertain" and call.estimated_usd is None
    assert await live_blocked_reason(db, ctx.claim) == "provider_outcome_unknown"


@pytest.mark.parametrize(
    "content",
    [
        [{"text": "Certo!", "annotations": []}],
        [{"type": "unknown", "text": "Certo!", "annotations": []}],
        [{"type": "output_text", "annotations": []}],
        [{"type": "output_text", "text": "Certo!"}],
        [
            {
                "type": "output_text",
                "text": "Certo!",
                "annotations": [{"type": "url_citation"}],
            }
        ],
        [{"type": "refusal"}],
    ],
    ids=[
        "missing_type",
        "unknown_type",
        "missing_text",
        "missing_annotations",
        "invalid_annotation",
        "missing_refusal",
    ],
)
async def test_malformed_message_content_returns_upstream_and_blocks(
    api_live_mocked, openai_mock, db, content
):
    body = provider_response()
    body["output"][0]["content"] = content
    openai_mock.route("responses").respond(200, json=body)
    api = api_live_mocked
    response = await api.send(uuid4(), "olá")
    assert response.json()["reply"] == MESSAGES["upstream_unavailable"]
    assert response.json()["status"] == "failed"
    call = await only_llm_call(db, api.conversation_id)
    assert call.status == "settled" and call.estimated_usd == Decimal("0.000203")
    assert (
        await live_blocked_reason(db, api.conversation_id) == "provider_outcome_unknown"
    )
    assert openai_mock.route("responses").call_count == 1


@pytest.mark.parametrize(
    "content",
    [
        [{"type": "refusal", "refusal": "Não posso atender a esse pedido."}],
        [
            {
                "type": "output_text",
                "text": "Certo!",
                "annotations": [
                    {
                        "type": "url_citation",
                        "start_index": 0,
                        "end_index": 5,
                        "title": "Synthetic source",
                        "url": "https://example.test/source",
                    }
                ],
            }
        ],
    ],
    ids=["refusal", "annotated_text"],
)
async def test_valid_message_content_and_reasoning_remain_intact(
    model, request_body, ctx, openai_mock, db, content
):
    reasoning = {
        "id": "rs_test",
        "type": "reasoning",
        "summary": [],
        "encrypted_content": "opaque",
    }
    body = provider_response()
    body["output"][0]["content"] = content
    body["output"].insert(0, reasoning)
    openai_mock.route("responses").respond(200, json=body)
    response = await generate(model, request_body, ctx)
    assert response.status == "completed"
    assert response.text == ("Certo!" if content[0]["type"] == "output_text" else None)
    assert response.output_items == tuple(body["output"])
    assert (await only_llm_call(db, ctx.claim)).status == "settled"
    assert await live_blocked_reason(db, ctx.claim) is None
    assert openai_mock.route("responses").call_count == 1


async def test_trace_exposes_only_known_model_usage_metadata(
    model, request_body, ctx, openai_mock, db
):
    await generate(model, request_body, ctx)
    count, generation = ctx.trace
    assert count["kind"] == "token_count"
    assert count["input_tokens"] == 100
    assert (
        count["estimated_cost_usd"] is None
    )  # counting is verified unbilled, no ledger row
    assert generation["kind"] == "generation"
    assert generation["service_tier"] == "default"
    assert generation["input_tokens"] == 100
    assert generation["output_tokens"] == 10
    assert generation["estimated_cost_usd"] == "0.000203"
    assert generation["outcome_class"] == "settled"
    assert (
        not {"input", "output", "arguments", "payload", "instructions"}
        & generation.keys()
    )


async def test_trace_unknown_usage_remains_null(model, request_body, ctx, openai_mock):
    openai_mock.route("responses").mock(return_value=error_response(500))
    with pytest.raises(ModelUnavailable):
        await generate(model, request_body, ctx)
    generation = ctx.trace[-1]
    assert generation["input_tokens"] is None
    assert generation["output_tokens"] is None
    assert generation["estimated_cost_usd"] is None
    assert generation["outcome_class"] == "unknown"


async def test_api_trace_preserves_model_metadata_on_transcript_reload(api_live_mocked):
    sent = await api_live_mocked.send(uuid4(), "olá")
    calls = sent.json()["trace"]["model_calls"]
    assert calls[0]["kind"] == "token_count" and calls[0]["input_tokens"] == 100
    assert calls[-1]["service_tier"] == "default"
    assert calls[-1]["estimated_cost_usd"] == "0.000203"
    assert calls[-1]["outcome_class"] == "settled"
    transcript = await api_live_mocked.transcript()
    assert transcript.json()["turns"][0]["trace"]["model_calls"] == calls


async def test_failed_count_is_visible_as_unknown_usage_metadata(
    api_live_mocked, openai_mock, db
):
    openai_mock.route("input_tokens").mock(return_value=error_response(500))
    sent = await api_live_mocked.send(uuid4(), "olá")
    calls = sent.json()["trace"]["model_calls"]
    assert len(calls) == 1
    assert calls[0]["kind"] == "token_count"
    assert calls[0]["outcome_class"] == "count_failed"
    assert calls[0]["input_tokens"] is None
    assert calls[0]["estimated_cost_usd"] is None
    assert await llm_calls(db, api_live_mocked.conversation_id) == []
