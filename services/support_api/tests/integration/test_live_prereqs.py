"""Live-gate prerequisites: billed token counting, the single project cap, money CHECKs."""

import asyncio
from decimal import Decimal
from uuid import uuid4

import httpx
import openai
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from support_api.agent.models.base import ModelContext, ModelRequest, ModelUnavailable
from support_api.agent.models.openai_model import (
    OpenAIResponsesModel,
    count_reservation_usd,
)
from support_api.deadline import Deadline
from support_api.usage.budget import recover_reservations, sync_project_cap

from tests.conftest import (
    conv_budget,
    live_blocked_reason,
    llm_calls,
    only_llm_call,
    project_budget,
)


def make_model(db, pricing, *, count_billed=True, effort="low", cap=Decimal("0.10")):
    client = openai.AsyncOpenAI(
        api_key="sk-test-not-real", max_retries=0, http_client=httpx.AsyncClient()
    )
    return OpenAIResponsesModel(
        client,
        db=db,
        model="gpt-6-luna",
        tier="default",
        rate=pricing.rate("gpt-6-luna", "default"),
        max_output_tokens=800,
        pricing=pricing,
        conversation_cap_usd=cap,
        count_billed=count_billed,
        reasoning_effort=effort,
    )


@pytest.fixture
async def billed_model(db, verified_live_config, openai_mock):
    model = make_model(db, verified_live_config[2])
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
        "Reply in English.",
        ({"type": "message", "role": "user", "content": "olá"},),
        (),
    )


# --- billed token counting ----------------------------------------------------
async def test_billed_count_reserves_then_settles_at_input_rate(
    billed_model, request_body, ctx, openai_mock, db
):
    p_before = (await project_budget(db)).spent_usd
    await billed_model.count_input_tokens(request_body, ctx)
    await billed_model.generate(request_body, ctx)
    count = await only_llm_call(db, ctx.claim, kind="token_count")
    expected_hold = count_reservation_usd(
        billed_model.build_count_body(request_body), billed_model._rate
    )
    assert count.status == "settled" and count.reserved_usd == expected_hold
    assert count.estimated_usd == Decimal("0.000125")  # 100 tokens * 1.25 / 1M
    assert (count.input_tokens, count.output_tokens) == (100, 0)
    assert count.outcome_class == "settled" and count.service_tier == "default"
    generation = await only_llm_call(db, ctx.claim)
    assert generation.status == "settled"
    spent = (await project_budget(db)).spent_usd - p_before
    assert spent == count.estimated_usd + generation.estimated_usd
    conv = await conv_budget(db, ctx.claim)
    assert conv.reserved_usd == 0 and conv.uncertain_usd == 0
    assert ctx.trace[0]["kind"] == "token_count"
    assert ctx.trace[0]["estimated_cost_usd"] == "0.000125"


async def test_unbilled_count_writes_no_ledger_row(
    db, verified_live_config, openai_mock, request_body, ctx
):
    model = make_model(db, verified_live_config[2], count_billed=False)
    try:
        await model.count_input_tokens(request_body, ctx)
    finally:
        await model.aclose()
    assert await llm_calls(db, ctx.claim, kind="token_count") == []
    assert ctx.trace[0]["estimated_cost_usd"] is None


@pytest.mark.parametrize(
    "error", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout]
)
async def test_billed_count_never_sent_is_released(
    billed_model, request_body, ctx, openai_mock, db, error
):
    openai_mock.route("input_tokens").mock(side_effect=error("before send"))
    with pytest.raises(ModelUnavailable, match="upstream_unavailable"):
        await billed_model.count_input_tokens(request_body, ctx)
    row = await only_llm_call(db, ctx.claim, kind="token_count")
    assert row.status == "released" and row.outcome_class == "connect_failed"
    conv = await conv_budget(db, ctx.claim)
    assert conv.reserved_usd == 0 and conv.uncertain_usd == 0
    assert openai_mock.route("responses").call_count == 0


@pytest.mark.parametrize("failure", ["500", "timeout", "malformed"])
async def test_billed_count_possibly_sent_stays_uncertain_without_blocking(
    billed_model, request_body, ctx, openai_mock, db, failure
):
    route = openai_mock.route("input_tokens")
    if failure == "500":
        route.respond(500, json={"error": {"type": "server_error"}})
    elif failure == "timeout":
        route.mock(side_effect=httpx.ReadTimeout("after send"))
    else:
        route.respond(200, json={"object": "response.input_tokens"})
    with pytest.raises(ModelUnavailable, match="upstream_unavailable"):
        await billed_model.count_input_tokens(request_body, ctx)
    row = await only_llm_call(db, ctx.claim, kind="token_count")
    assert row.status == "uncertain"
    conv = await conv_budget(db, ctx.claim)
    assert conv.uncertain_usd == row.reserved_usd and conv.reserved_usd == 0
    # A failed count ends only this turn; the conversation can still go live next turn.
    assert await live_blocked_reason(db, ctx.claim) is None
    assert openai_mock.route("responses").call_count == 0


async def test_billed_count_over_conversation_cap_sends_nothing(
    db, verified_live_config, openai_mock, request_body, ctx
):
    model = make_model(db, verified_live_config[2], cap=Decimal("0.000001"))
    try:
        with pytest.raises(ModelUnavailable, match="live_blocked"):
            await model.count_input_tokens(request_body, ctx)
    finally:
        await model.aclose()
    assert openai_mock.call_count == 0
    assert await llm_calls(db, ctx.claim, kind="token_count") == []


async def test_billed_count_expired_deadline_releases_unsent_hold(
    billed_model, request_body, ctx, openai_mock, db, fake_clock
):
    fake_clock.advance(46)
    with pytest.raises(ModelUnavailable, match="limit_reached"):
        await billed_model.count_input_tokens(request_body, ctx)
    assert openai_mock.call_count == 0
    row = await only_llm_call(db, ctx.claim, kind="token_count")
    assert row.status == "released" and row.outcome_class == "deadline_expired"


async def test_billed_zero_count_is_malformed_and_uncertain(
    billed_model, request_body, ctx, openai_mock, db
):
    openai_mock.count_returns([0])
    with pytest.raises(ModelUnavailable, match="upstream_unavailable"):
        await billed_model.count_input_tokens(request_body, ctx)
    row = await only_llm_call(db, ctx.claim, kind="token_count")
    assert row.status == "uncertain" and row.estimated_usd is None
    assert ctx.input_tokens is None and openai_mock.route("responses").call_count == 0


@pytest.mark.parametrize(
    ("status", "code", "error_type", "reason"),
    [
        (401, "invalid_api_key", "invalid_request_error", "provider_rejected"),
        (400, None, "invalid_request_error", "provider_rejected"),
        (429, "insufficient_quota", "insufficient_quota", "provider_quota"),
        (429, "project_spend_limit_exceeded", "insufficient_quota", "provider_quota"),
    ],
)
async def test_deterministic_count_failure_blocks_live_mode(
    billed_model, request_body, ctx, openai_mock, db, status, code, error_type, reason
):
    openai_mock.route("input_tokens").respond(
        status, json={"error": {"type": error_type, "code": code, "message": "x"}}
    )
    with pytest.raises(ModelUnavailable, match="upstream_unavailable"):
        await billed_model.count_input_tokens(request_body, ctx)
    assert (
        await only_llm_call(db, ctx.claim, kind="token_count")
    ).status == "uncertain"
    assert await live_blocked_reason(db, ctx.claim) == reason
    assert openai_mock.route("responses").call_count == 0


async def test_claim_lost_after_count_reserve_keeps_hold_for_recovery(
    billed_model, request_body, ctx, openai_mock, db, pause_at
):
    from support_api.turns.claim import ClaimLost

    from tests.conftest import take_over

    gate = pause_at("after_count_reserve")
    task = asyncio.create_task(billed_model.count_input_tokens(request_body, ctx))
    await asyncio.wait_for(gate.reached.wait(), 10)
    await take_over(db, ctx.claim)
    gate.release()
    with pytest.raises(ClaimLost):
        await asyncio.wait_for(task, 10)
    assert openai_mock.call_count == 0
    row = await only_llm_call(db, ctx.claim, kind="token_count")
    assert row.status == "reserved"  # §5.5 recovery moves it to uncertain and blocks
    async with db.begin() as conn:
        await conn.execute(
            text("SELECT 1 FROM support.conversations WHERE id = :c FOR UPDATE"),
            {"c": ctx.claim.conversation_id},
        )
        assert (
            await recover_reservations(
                conn, ctx.claim.conversation_id, ctx.claim.turn_id
            )
            == 1
        )
    assert (
        await only_llm_call(db, ctx.claim, kind="token_count")
    ).status == "uncertain"
    assert await live_blocked_reason(db, ctx.claim) is not None


async def test_billed_counting_through_the_app_keeps_ledger_sums(
    live_app_factory, verified_live_config, openai_mock, db
):
    verified_live_config[0]["token_count"]["billed"] = True  # the shipped setting
    api = await live_app_factory()
    assert api.model._count_billed is True
    p_before = (await project_budget(db)).spent_usd
    for message in ("olá", "obrigado"):
        body = (await api.send(uuid4(), message)).json()
        assert body["status"] == "completed", body
    counts = await llm_calls(db, api.conversation_id, kind="token_count")
    gens = await llm_calls(db, api.conversation_id)
    assert [r.status for r in counts] == ["settled", "settled"]
    assert all(r.estimated_usd > 0 for r in counts)
    assert [r.status for r in gens] == ["settled", "settled"]
    total = sum(r.estimated_usd for r in [*counts, *gens])
    conv = await conv_budget(db, api.conversation_id)
    assert (
        conv.spent_usd == total and conv.reserved_usd == 0 and conv.uncertain_usd == 0
    )
    assert (await project_budget(db)).spent_usd - p_before == total


def test_count_reservation_bounds_the_body_size(verified_live_config):
    rate = verified_live_config[2].rate("gpt-6-luna", "default")
    small = count_reservation_usd({"input": "olá"}, rate)
    large = count_reservation_usd({"input": "x" * 40_000}, rate)
    assert small > 0
    # One token per UTF-8 byte plus 1000 headroom: 40k chars bound at least 40k tokens.
    assert large >= Decimal(41_000) * Decimal("1.25") / Decimal(1_000_000)


# --- reasoning effort -----------------------------------------------------------
async def test_reasoning_effort_sent_on_count_and_generation(
    billed_model, request_body, ctx, openai_mock
):
    await billed_model.count_input_tokens(request_body, ctx)
    await billed_model.generate(request_body, ctx)
    assert openai_mock.captured_all("responses")[0]["reasoning"] == {"effort": "low"}
    # The count endpoint accepts reasoning, so the counted request matches the generated one.
    assert openai_mock.captured_all("input_tokens")[0]["reasoning"] == {"effort": "low"}


def test_no_reasoning_effort_omits_the_field(db, verified_live_config, request_body):
    model = make_model(db, verified_live_config[2], effort=None)
    assert "reasoning" not in model.build_generation_body(request_body)


def test_reasoning_effort_setting_defaults_to_low(settings):
    fields = type(settings).model_fields
    assert fields["openai_reasoning_effort"].default == "low"
    blank = settings.model_validate(
        {**settings.model_dump(), "openai_reasoning_effort": ""}
    )
    assert blank.openai_reasoning_effort == "low"


# --- PROJECT_BUDGET_USD is the single source of the project cap -------------------
@pytest.fixture
async def restore_project_cap(db):
    original = (await project_budget(db)).cap_usd
    yield
    await sync_project_cap(db, original)


async def test_sync_project_cap_writes_settings_value(db, restore_project_cap):
    await sync_project_cap(db, Decimal("12.5"))
    assert (await project_budget(db)).cap_usd == Decimal("12.500000")


@pytest.mark.parametrize("bad", [Decimal(-1), Decimal("NaN"), Decimal("Infinity")])
async def test_sync_project_cap_rejects_invalid(db, bad):
    with pytest.raises(ValueError):
        await sync_project_cap(db, bad)


async def test_app_startup_applies_project_budget(
    settings, db, restore_project_cap, billing_mock
):
    from support_api.app import create_app

    app = create_app(settings.model_copy(update={"project_budget_usd": Decimal(7)}))
    async with app.router.lifespan_context(app):
        assert (await project_budget(db)).cap_usd == Decimal("7.000000")


# --- money CHECK constraints -------------------------------------------------------
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE support.budget_accounts SET spent_usd = -0.000001 WHERE id = 'project'",
        "UPDATE support.budget_accounts SET reserved_usd = -1 WHERE id = 'project'",
        "UPDATE support.budget_accounts SET uncertain_usd = -1 WHERE id = 'project'",
        "UPDATE support.budget_accounts SET cap_usd = -1 WHERE id = 'project'",
        "UPDATE support.conversation_budgets SET reserved_usd = -1",
        "UPDATE support.llm_calls SET estimated_usd = -1",
        "UPDATE support.llm_calls SET input_tokens = -1",
    ],
)
async def test_negative_money_is_rejected_by_the_database(
    billed_model, request_body, ctx, openai_mock, db, statement
):
    # One settled count + generation gives every table a row to violate.
    await billed_model.count_input_tokens(request_body, ctx)
    await billed_model.generate(request_body, ctx)
    with pytest.raises(IntegrityError):
        async with db.begin() as conn:
            await conn.execute(text(statement))
