from decimal import Decimal

from support_api.settings import Settings

REQUIRED = {
    "support_database_url": "postgresql+psycopg://u:p@localhost/x",
    "billing_api_key": "k",
    "identifier_hash_key": "h",
}


def test_defaults_match_global_constraints(monkeypatch):
    for name in (
        "LLM_PROVIDER",
        "LIVE_LLM_ENABLED",
        "OPENAI_API_KEY",
        "ALLOWED_ORIGINS",
    ):
        monkeypatch.delenv(name, raising=False)
    s = Settings(**REQUIRED)
    assert (s.max_model_calls, s.max_tool_calls, s.max_turns) == (5, 4, 30)
    assert s.max_output_tokens == 800
    assert (s.turn_deadline_s, s.turn_lease_s) == (45, 60)
    assert (
        s.llm_attempt_s,
        s.token_count_s,
        s.billing_attempt_s,
        s.billing_tool_total_s,
    ) == (20, 10, 3, 8)
    assert s.history_max_turns == 6 and s.max_input_tokens == 8000
    assert (s.identity_max_failures, s.identity_window_s, s.identity_lock_s) == (
        3,
        900,
        900,
    )
    assert s.verification_slot_ttl_s == 30 and s.verification_ttl_s == 1800
    assert s.project_budget_usd == Decimal(20) and s.conversation_budget_usd == Decimal(
        "0.10"
    )
    assert s.openai_service_tier == "default" and s.openai_chat_model == "gpt-6-luna"
    assert s.llm_provider == "scripted" and s.live_llm_enabled is False
    assert s.openai_api_key is None
    assert s.demo_data_endpoint_enabled is True


def test_empty_api_key_and_comma_separated_origins(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://a.example, https://b.example")
    s = Settings(**REQUIRED)
    assert s.openai_api_key is None
    assert s.allowed_origins == ["https://a.example", "https://b.example"]
