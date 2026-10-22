"""Live-mode gate (spec §5.6 items 1-4). Pure: no network, no OpenAI client, no real key."""

import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest
from support_api.settings import Settings
from support_api.usage.pricing import (
    ConfigError,
    LiveGateClosed,
    ModelsConfig,
    Pricing,
    Rate,
    live_gate_failures,
)

SERVICE_DIR = Path(__file__).resolve().parents[2]
PRICING = SERVICE_DIR / "config" / "pricing.json"
MODELS = SERVICE_DIR / "config" / "models.json"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FIXTURE_PRICING = FIXTURES / "pricing_verified.json"
FIXTURE_MODELS = FIXTURES / "models_verified.json"

BASE = {
    "support_database_url": "postgresql+psycopg://u:p@localhost/x",
    "billing_api_key": "k",
    "identifier_hash_key": "h",
}


def make_settings(monkeypatch, **overrides) -> Settings:
    for name in (
        "LIVE_LLM_ENABLED",
        "OPENAI_API_KEY",
        "OPENAI_CHAT_MODEL",
        "OPENAI_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None, **{**BASE, **overrides})


@pytest.fixture
def settings_live_with_key(monkeypatch):
    return make_settings(
        monkeypatch, live_llm_enabled=True, openai_api_key="sk-test-not-real"
    )


@pytest.fixture
def verified_fixture_files():
    return Pricing.load(FIXTURE_PRICING), ModelsConfig.load(FIXTURE_MODELS)


def _mutated(src: Path, tmp_path: Path, name: str, mutate) -> Path:
    data = json.loads(src.read_text())
    mutate(data)
    out = tmp_path / name
    out.write_text(json.dumps(data))
    return out


def _entry(data):
    return data["models"]["gpt-6-luna"]["default"]


# --- gate -------------------------------------------------------------------
def test_committed_config_keeps_gate_closed(settings_live_with_key):
    failures = live_gate_failures(
        settings_live_with_key, Pricing.load(PRICING), ModelsConfig.load(MODELS)
    )
    assert set(failures) == {
        "pricing_unverified",
        "token_count_not_verified_unbilled",
        "model_reasoning_unverified",
    }


def test_committed_config_files_are_unverified_placeholders():
    # Guards against anyone flipping the shipped placeholders.
    assert (
        json.loads(PRICING.read_text())["models"]["gpt-6-luna"]["default"]["verified"]
        is False
    )
    assert json.loads(MODELS.read_text())["gpt-6-luna"]["verified"] is False


def test_fully_verified_fixture_opens_gate(
    settings_live_with_key, verified_fixture_files
):
    pricing, models = verified_fixture_files
    assert live_gate_failures(settings_live_with_key, pricing, models) == []


def test_disabled_flag_alone_closes_gate(monkeypatch, verified_fixture_files):
    s = make_settings(
        monkeypatch, live_llm_enabled=False, openai_api_key="sk-test-not-real"
    )
    assert live_gate_failures(s, *verified_fixture_files) == ["live_disabled"]


@pytest.mark.parametrize("key", [None, ""])
def test_missing_key_alone_closes_gate(monkeypatch, verified_fixture_files, key):
    s = make_settings(monkeypatch, live_llm_enabled=True, openai_api_key=key)
    assert live_gate_failures(s, *verified_fixture_files) == ["missing_api_key"]


def test_empty_secret_object_counts_as_missing(monkeypatch, verified_fixture_files):
    s = make_settings(
        monkeypatch, live_llm_enabled=True, openai_api_key="sk-test-not-real"
    )
    object.__setattr__(s, "openai_api_key", type(s.openai_api_key)(""))
    assert live_gate_failures(s, *verified_fixture_files) == ["missing_api_key"]


def test_everything_off_reports_all_reasons_in_stable_order(monkeypatch):
    s = make_settings(monkeypatch)
    assert live_gate_failures(s, Pricing.load(PRICING), ModelsConfig.load(MODELS)) == [
        "live_disabled",
        "missing_api_key",
        "pricing_unverified",
        "token_count_not_verified_unbilled",
        "model_reasoning_unverified",
    ]


@pytest.mark.parametrize("billed", [True, None])
def test_billed_or_unknown_counting_closes_gate(
    billed, tmp_path, settings_live_with_key, verified_fixture_files
):
    p = _mutated(
        FIXTURE_PRICING,
        tmp_path,
        "p.json",
        lambda d: d["token_count"].update(billed=billed),
    )
    failures = live_gate_failures(
        settings_live_with_key, Pricing.load(p), verified_fixture_files[1]
    )
    assert failures == ["token_count_not_verified_unbilled"]


@pytest.mark.parametrize("billed", ["false", 0, "", [], {}])
def test_billed_must_be_literally_false(
    billed, tmp_path, settings_live_with_key, verified_fixture_files
):
    p = _mutated(
        FIXTURE_PRICING,
        tmp_path,
        "p.json",
        lambda d: d["token_count"].update(billed=billed),
    )
    assert not Pricing.load(p).token_count_unbilled()


@pytest.mark.parametrize("field", ["source_url", "verified_on"])
def test_unbilled_without_provenance_is_unverified(field, tmp_path):
    p = _mutated(
        FIXTURE_PRICING,
        tmp_path,
        "p.json",
        lambda d: d["token_count"].update({field: None}),
    )
    assert not Pricing.load(p).token_count_unbilled()


def test_tier_without_entry_closes_gate(monkeypatch, verified_fixture_files):
    s = make_settings(
        monkeypatch,
        live_llm_enabled=True,
        openai_api_key="sk-test-not-real",
        openai_service_tier="priority",
    )
    assert live_gate_failures(s, *verified_fixture_files) == ["pricing_unverified"]


def test_model_without_entry_closes_pricing_and_reasoning(
    monkeypatch, verified_fixture_files
):
    s = make_settings(
        monkeypatch,
        live_llm_enabled=True,
        openai_api_key="sk-test-not-real",
        openai_chat_model="some-other-model",
    )
    assert live_gate_failures(s, *verified_fixture_files) == [
        "pricing_unverified",
        "model_reasoning_unverified",
    ]


# --- Pricing.rate -----------------------------------------------------------
def test_rate_parses_decimals_without_float_error(verified_fixture_files):
    rate = verified_fixture_files[0].rate("gpt-6-luna", "default")
    assert rate == Rate(Decimal("1.25"), Decimal(10), Decimal("0.125"))
    assert all(
        isinstance(v, Decimal)
        for v in (rate.input_per_mtok, rate.output_per_mtok, rate.cached_input_per_mtok)
    )


def test_rate_uses_str_not_float_for_parsing(tmp_path):
    p = _mutated(
        FIXTURE_PRICING,
        tmp_path,
        "p.json",
        lambda d: _entry(d).update(input_per_mtok=0.1),
    )
    assert Pricing.load(p).rate("gpt-6-luna", "default").input_per_mtok == Decimal(
        "0.1"
    )


def test_cached_rate_may_be_null(tmp_path):
    p = _mutated(
        FIXTURE_PRICING,
        tmp_path,
        "p.json",
        lambda d: _entry(d).update(cached_input_per_mtok=None),
    )
    assert Pricing.load(p).rate("gpt-6-luna", "default").cached_input_per_mtok is None


def test_rate_none_for_unknown_model_or_tier(verified_fixture_files):
    pricing = verified_fixture_files[0]
    assert pricing.rate("nope", "default") is None
    assert pricing.rate("gpt-6-luna", "flex") is None


@pytest.mark.parametrize(
    "patch",
    [
        {"verified": False},
        {"verified": "true"},
        {"verified": 1},
        {"verified": None},
        {"input_per_mtok": None},
        {"output_per_mtok": None},
        {"source_url": None},
        {"source_url": ""},
        {"verified_on": None},
        {"verified_on": ""},
        {"input_per_mtok": "1.25"},
        {"output_per_mtok": True},
        {"input_per_mtok": -1},
        {"output_per_mtok": 0},
        {"cached_input_per_mtok": "x"},
        {"input_per_mtok": float("nan")},
    ],
)
def test_rate_none_unless_every_field_is_verified(patch, tmp_path):
    p = _mutated(FIXTURE_PRICING, tmp_path, "p.json", lambda d: _entry(d).update(patch))
    assert Pricing.load(p).rate("gpt-6-luna", "default") is None


def test_committed_pricing_has_no_rate():
    assert Pricing.load(PRICING).rate("gpt-6-luna", "default") is None


# --- ModelsConfig -----------------------------------------------------------
def test_reasoning_items_verified(verified_fixture_files):
    assert verified_fixture_files[1].reasoning_items("gpt-6-luna") == "current_turn"


@pytest.mark.parametrize("mode", ["none", "current_turn", "all_turns"])
def test_reasoning_modes_accepted(mode, tmp_path):
    p = _mutated(
        FIXTURE_MODELS,
        tmp_path,
        "m.json",
        lambda d: d["gpt-6-luna"].update(reasoning_items=mode),
    )
    assert ModelsConfig.load(p).reasoning_items("gpt-6-luna") == mode


@pytest.mark.parametrize(
    "patch",
    [
        {"verified": False},
        {"verified": "true"},
        {"reasoning_items": None},
        {"reasoning_items": "sometimes"},
        {"reasoning_items": 1},
        {"source_url": None},
        {"verified_on": ""},
    ],
)
def test_reasoning_items_none_unless_verified(patch, tmp_path):
    p = _mutated(
        FIXTURE_MODELS, tmp_path, "m.json", lambda d: d["gpt-6-luna"].update(patch)
    )
    assert ModelsConfig.load(p).reasoning_items("gpt-6-luna") is None


def test_reasoning_items_unknown_model(verified_fixture_files):
    assert verified_fixture_files[1].reasoning_items("nope") is None


# --- malformed config fails closed (loading raises ConfigError) -------------
@pytest.mark.parametrize("loader", [Pricing.load, ModelsConfig.load])
def test_missing_file_raises(loader, tmp_path):
    with pytest.raises(ConfigError):
        loader(tmp_path / "absent.json")


@pytest.mark.parametrize("loader", [Pricing.load, ModelsConfig.load])
@pytest.mark.parametrize("text", ["", "{not json", "[]", "null", '"x"'])
def test_bad_json_or_wrong_top_level_raises(loader, text, tmp_path):
    f = tmp_path / "bad.json"
    f.write_text(text)
    with pytest.raises(ConfigError):
        loader(f)


@pytest.mark.parametrize(
    "data",
    [
        {"models": [], "token_count": {}},
        {"models": {"m": []}, "token_count": {}},
        {"models": {"m": {"default": "x"}}, "token_count": {}},
        {"models": {}, "token_count": []},
        {"models": {}},
        {"token_count": {}},
    ],
)
def test_pricing_wrong_shape_raises(data, tmp_path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps(data))
    with pytest.raises(ConfigError):
        Pricing.load(f)


@pytest.mark.parametrize("data", [{"m": []}, {"m": "x"}, {"m": None}])
def test_models_wrong_shape_raises(data, tmp_path):
    f = tmp_path / "m.json"
    f.write_text(json.dumps(data))
    with pytest.raises(ConfigError):
        ModelsConfig.load(f)


def test_gate_closed_exception_carries_reasons():
    exc = LiveGateClosed(["live_disabled", "missing_api_key"])
    assert exc.reasons == ["live_disabled", "missing_api_key"]
    assert "live_disabled" in str(exc)
    assert copy.copy(exc).reasons == exc.reasons
