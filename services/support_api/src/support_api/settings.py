from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven settings. Defaults are the Global Constraints values."""

    model_config = SettingsConfigDict(
        extra="ignore", case_sensitive=False, populate_by_name=True
    )

    # Infrastructure
    support_database_url: str
    billing_base_url: str = "http://localhost:8001"
    billing_api_key: SecretStr
    identifier_hash_key: SecretStr
    allowed_origins: Annotated[list[str], NoDecode] = []
    personas_path: Path = Path("data/synthetic/personas.json")
    demo_data_endpoint_enabled: bool = True

    # Model provider. LIVE_LLM_ENABLED stays false unless a human flips it; the gate in §5.6 also
    # needs verified pricing/model config, which ships as placeholders.
    llm_provider: Literal["scripted", "openai"] = "scripted"
    live_llm_enabled: bool = False
    openai_api_key: SecretStr | None = None
    openai_chat_model: str = Field(
        default="gpt-6-luna",
        validation_alias=AliasChoices("OPENAI_CHAT_MODEL", "OPENAI_MODEL"),
    )
    openai_service_tier: str = "default"

    # Per-turn limits (§5.4)
    max_model_calls: int = 5
    max_tool_calls: int = 4
    max_turns: int = 30
    max_output_tokens: int = 800
    max_input_tokens: int = 8000
    history_max_turns: int = 6
    turn_deadline_s: float = 45
    turn_lease_s: float = 60
    llm_attempt_s: float = 20
    token_count_s: float = 10
    billing_attempt_s: float = 3
    billing_tool_total_s: float = 8

    # Identity (§5.2)
    identity_max_failures: int = 3
    identity_window_s: int = 900
    identity_lock_s: int = 900
    verification_slot_ttl_s: int = 30
    verification_ttl_s: int = 1800

    # Budgets (money is Decimal; stored as NUMERIC(12,6))
    project_budget_usd: Decimal = Decimal(20)
    conversation_budget_usd: Decimal = Decimal("0.10")

    @field_validator("openai_api_key", mode="before")
    @classmethod
    def _empty_key_is_none(cls, v):
        return None if v in ("", None) else v

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, v):
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @model_validator(mode="after")
    def _deadline_below_lease(self):
        # The lease must outlive the turn deadline; the gap is the margin the pre-send gate relies on.
        if self.turn_deadline_s >= self.turn_lease_s:
            raise ValueError("TURN_DEADLINE_S must be below TURN_LEASE_S")
        return self
