"""The startup gate runs before constructing any provider client (AC 19)."""

from unittest.mock import patch
from uuid import uuid4

import pytest
from support_api.app import create_app
from support_api.catalog import MESSAGES
from support_api.usage.pricing import LiveGateClosed

from tests.conftest import persona_creds_text


async def test_disabled_live_mode_sends_zero_requests(api, personas, openai_mock):
    assert (await api.send(uuid4(), "minhas faturas")).json()["status"] == "completed"
    assert (await api.send(uuid4(), persona_creds_text(personas[0]))).json()[
        "status"
    ] == "completed"
    result = (await api.send(uuid4(), "minhas faturas")).json()
    assert result["status"] == "completed"
    assert result["trace"]["tool_calls"][-1]["name"] == "list_bills"
    assert openai_mock.call_count == 0


@pytest.mark.parametrize(
    "override",
    [
        "live_disabled",
        "missing_key",
        "pricing_unverified",
        "token_count_billed",
        "token_count_unknown",
        "reasoning_unverified",
    ],
)
def test_gate_closed_fails_startup_without_client(
    override, live_settings, verified_live_config, openai_mock
):
    pricing, models, _, _ = verified_live_config
    if override == "live_disabled":
        live_settings = live_settings.model_copy(update={"live_llm_enabled": False})
    elif override == "missing_key":
        live_settings = live_settings.model_copy(update={"openai_api_key": None})
    elif override == "pricing_unverified":
        pricing["models"]["gpt-6-luna"]["default"]["verified"] = False
    elif override.startswith("token_count"):
        pricing["token_count"]["billed"] = True if override.endswith("billed") else None
    else:
        models["gpt-6-luna"]["verified"] = False
    with (
        patch(
            "openai.AsyncOpenAI.__init__",
            side_effect=AssertionError("client constructed before gate"),
        ) as spy,
        pytest.raises(LiveGateClosed),
    ):
        create_app(live_settings)
    assert spy.call_count == 0 and openai_mock.call_count == 0


async def test_explicit_conversation_cap_blocks_before_generation(
    live_app_factory, openai_mock
):
    from decimal import Decimal

    api = await live_app_factory(conversation_budget_usd=Decimal("0.000001"))
    response = await api.send(uuid4(), "olá")
    assert response.json()["reply"] == MESSAGES["live_blocked"]
    assert openai_mock.route("input_tokens").call_count == 1
    assert openai_mock.route("responses").call_count == 0


async def test_runtime_config_directory_used_in_installed_app(
    settings, openai_mock, tmp_path, monkeypatch
):
    import shutil

    from pydantic import SecretStr

    from tests.conftest import SERVICE_DIR

    # Docker installs the package into site-packages but copies verified/operator config to /app/config.
    config = tmp_path / "config"
    config.mkdir()
    for name in ("pricing", "models"):
        shutil.copy(
            SERVICE_DIR / "tests" / "fixtures" / f"{name}_verified.json",
            config / f"{name}.json",
        )
    monkeypatch.chdir(tmp_path)
    app = create_app(
        settings.model_copy(
            update={
                "llm_provider": "openai",
                "live_llm_enabled": True,
                "openai_api_key": SecretStr("sk-test-not-real"),
                "system_prompt_path": SERVICE_DIR / "prompts" / "system_ptbr.md",
            }
        )
    )
    try:
        assert app.state.model.live and openai_mock.call_count == 0
    finally:
        await app.state.model.aclose()
        await app.state.billing.aclose()
        await app.state.db.dispose()
