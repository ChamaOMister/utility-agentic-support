"""Identifier normalization and the lockout key (spec §5.2)."""

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Literal

_NON_DIGITS = re.compile(r"[^0-9]")
_KIND_BY_LENGTH: dict[int, Literal["cpf", "account"]] = {11: "cpf", 10: "account"}


@dataclass(frozen=True)
class NormalizedIdentifier:
    kind: Literal["cpf", "account"]
    digits: str


def normalize(raw: str) -> NormalizedIdentifier | None:
    """Strip everything but digits; 11 digits is a CPF, 10 an account number, else ``None``."""
    digits = _NON_DIGITS.sub("", raw)
    kind = _KIND_BY_LENGTH.get(len(digits))
    return NormalizedIdentifier(kind, digits) if kind else None


def lockout_key(n: NormalizedIdentifier, key: bytes) -> str:
    """Hex ``HMAC-SHA256(key, "<kind>:<digits>")``: the only form of the identifier ever stored."""
    return hmac.new(key, f"{n.kind}:{n.digits}".encode(), hashlib.sha256).hexdigest()
