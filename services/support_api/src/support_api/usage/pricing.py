"""Pricing, per-model config and the live-mode gate (spec §5.6 items 1-4, §8).

Pure module: reads two JSON files, never touches the network and never builds an OpenAI client.

Everything here fails closed:
- A value counts only if it is exactly the verified shape (``verified is True``, numeric rates,
  non-empty ``source_url``, ISO ``verified_on``). Anything else reads as unverified.
- A structurally malformed file (missing, bad JSON, wrong container types) raises ``ConfigError``
  on load, so startup cannot proceed with a config nobody can interpret.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, get_args

from support_api.settings import Settings

ReasoningMode = Literal["none", "current_turn", "all_turns"]
_REASONING_MODES = frozenset(get_args(ReasoningMode))


class ConfigError(ValueError):
    """A pricing/models config file is unreadable or structurally malformed."""


class LiveGateClosed(Exception):
    """Live mode was requested but the §5.6 gate is closed. ``reasons`` lists every failed check."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__(reasons)
        self.reasons = list(reasons)

    def __str__(self) -> str:
        return "live mode gate is closed: " + ", ".join(self.reasons)


@dataclass(frozen=True)
class Rate:
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cached_input_per_mtok: Decimal | None


def _read_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (
        OSError,
        ValueError,
    ) as exc:  # ValueError covers JSONDecodeError and bad encodings
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config {path} must be a JSON object")
    return data


def _has_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_iso_date(value: Any) -> bool:
    if not _has_text(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _has_provenance(entry: dict[str, Any]) -> bool:
    return _has_text(entry.get("source_url")) and _is_iso_date(entry.get("verified_on"))


def _decimal(value: Any, *, minimum_exclusive: bool) -> Decimal | None:
    """JSON number -> Decimal via its text form (never float arithmetic); None if not a sane rate."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    result = Decimal(str(value))
    if result < 0 or (minimum_exclusive and result == 0):
        return None
    return result


class Pricing:
    def __init__(
        self, models: dict[str, dict[str, dict[str, Any]]], token_count: dict[str, Any]
    ):
        self._models = models
        self._token_count = token_count

    @classmethod
    def load(cls, path: Path) -> Pricing:
        data = _read_object(path)
        models = data.get("models")
        token_count = data.get("token_count")
        if not isinstance(models, dict) or not isinstance(token_count, dict):
            raise ConfigError(
                f"pricing {path}: 'models' and 'token_count' must be objects"
            )
        for model, tiers in models.items():
            if not isinstance(tiers, dict) or not all(
                isinstance(e, dict) for e in tiers.values()
            ):
                raise ConfigError(
                    f"pricing {path}: entry for {model!r} must map tier -> object"
                )
        return cls(models, token_count)

    def rate(self, model: str, tier: str) -> Rate | None:
        """The verified rate for (model, tier), else None."""
        entry = self._models.get(model, {}).get(tier)
        if (
            entry is None
            or entry.get("verified") is not True
            or not _has_provenance(entry)
        ):
            return None
        input_rate = _decimal(entry.get("input_per_mtok"), minimum_exclusive=True)
        output_rate = _decimal(entry.get("output_per_mtok"), minimum_exclusive=True)
        if input_rate is None or output_rate is None:
            return None
        cached_raw = entry.get("cached_input_per_mtok")
        cached = None
        if cached_raw is not None:
            cached = _decimal(cached_raw, minimum_exclusive=False)
            if cached is None:
                return None
        return Rate(input_rate, output_rate, cached)

    def token_count_unbilled(self) -> bool:
        """True only when token counting is recorded as unbilled, with provenance."""
        tc = self._token_count
        return tc.get("billed") is False and _has_provenance(tc)

    def token_count_billed(self) -> bool | None:
        """Recorded billing of token counting: True/False with provenance, else None (unknown).

        ``True`` means counts are reserved and settled at the model's input rate, the safe reading
        when the provider does not document the endpoint as free.
        """
        tc = self._token_count
        billed = tc.get("billed")
        if isinstance(billed, bool) and _has_provenance(tc):
            return billed
        return None


class ModelsConfig:
    def __init__(self, models: dict[str, dict[str, Any]]):
        self._models = models

    @classmethod
    def load(cls, path: Path) -> ModelsConfig:
        data = _read_object(path)
        for model, entry in data.items():
            if not isinstance(entry, dict):
                raise ConfigError(
                    f"models {path}: entry for {model!r} must be an object"
                )
        return cls(data)

    def reasoning_items(self, model: str) -> ReasoningMode | None:
        """The verified cross-turn reasoning mode for ``model``, else None."""
        entry = self._models.get(model)
        if (
            entry is None
            or entry.get("verified") is not True
            or not _has_provenance(entry)
        ):
            return None
        mode = entry.get("reasoning_items")
        if isinstance(mode, str) and mode in _REASONING_MODES:
            return mode  # type: ignore[return-value]
        return None


def live_gate_failures(
    settings: Settings, pricing: Pricing, models: ModelsConfig
) -> list[str]:
    """Reasons the live gate is closed, in a stable order. Empty list means open.

    Mirrors §5.6 items 1-4. Every check is evaluated (no short-circuit) so callers see all reasons.
    Token counting may be recorded as billed: the model then reserves and settles each count.
    """
    failures: list[str] = []
    if not settings.live_llm_enabled:
        failures.append("live_disabled")
    key = settings.openai_api_key
    if key is None or not key.get_secret_value().strip():
        failures.append("missing_api_key")
    if pricing.rate(settings.openai_chat_model, settings.openai_service_tier) is None:
        failures.append("pricing_unverified")
    if pricing.token_count_billed() is None:
        failures.append("token_count_billing_unverified")
    if models.reasoning_items(settings.openai_chat_model) is None:
        failures.append("model_reasoning_unverified")
    return failures
