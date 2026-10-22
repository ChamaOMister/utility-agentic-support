"""Provider outcome classification (spec §5.6 outcome table, row for row).

Pure: no DB, no network. Charge certainty (the ``ledger`` action) and retry eligibility are decided
here, separately; the caller applies them.

Rules worth knowing:

* The spec makes no zero-charge claim for any HTTP error response, so every provider HTTP error
  keeps its reservation ``uncertain``. Only a failure *before the request left the client*
  releases it.
* "Before send" is recognised conservatively, from the exception chain only: a connect error or
  connect timeout, or a pool timeout (no connection was ever acquired, so nothing was written).
  A bare ``openai.APIConnectionError`` with no such cause, a read/write timeout, an interrupted
  read, ``asyncio.TimeoutError`` and anything unrecognised are ``UNKNOWN``.
* The pinned ``openai`` SDK wraps ``httpx2`` errors (``APIConnectionError`` / ``APITimeoutError``
  with ``__cause__`` set), while code and tests that inject a legacy ``httpx.AsyncClient`` raise
  ``httpx`` errors. Both libraries' classes are matched; each import is defensive.
* The exception chain is walked over ``__cause__`` and ``__context__`` with a cycle guard, and the
  *outermost* recognised error decides. A ``ReadTimeout`` raised after an earlier, handled
  ``ConnectError`` therefore stays ``UNKNOWN``: by then a request may have been sent.
* A ``Retry-After`` HTTP-date is treated as non-numeric (delay 1.0 s); only delta-seconds are read.
"""

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

import httpx

try:  # the pinned openai SDK uses httpx2 internally
    import httpx2
except ImportError:  # pragma: no cover - depends on the installed openai version
    httpx2 = None  # type: ignore[assignment]


class OutcomeClass(StrEnum):
    SETTLED = "settled"
    UNPRICED_TIER = "unpriced_tier"
    CONNECT_FAILED = "connect_failed"
    RATE_LIMITED = "rate_limited"
    QUOTA = "quota"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Decision:
    outcome: OutcomeClass
    ledger: Literal["settle", "release", "uncertain"]
    block_reason: str | None
    retryable: bool


_TRANSIENT_CODES = frozenset({"rate_limit_exceeded", "slow_down"})
_QUOTA_TYPES = frozenset({"insufficient_quota"})
_QUOTA_CODES = frozenset(
    {
        # Not in the spec's code list, but OpenAI also reports it as the code; accepting it
        # keeps a "rate_limit_error" + "insufficient_quota" body on the blocking side.
        "insufficient_quota",
        "credit_balance_exhausted",
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
    }
)

_SETTLED = Decision(OutcomeClass.SETTLED, "settle", None, False)
_UNPRICED = Decision(OutcomeClass.UNPRICED_TIER, "uncertain", "unpriced_tier", False)
_CONNECT_FAILED = Decision(OutcomeClass.CONNECT_FAILED, "release", None, True)
_RATE_LIMITED = Decision(OutcomeClass.RATE_LIMITED, "uncertain", None, True)
_QUOTA = Decision(OutcomeClass.QUOTA, "uncertain", "provider_quota", False)
_REJECTED = Decision(OutcomeClass.REJECTED, "uncertain", "provider_rejected", False)
_UNKNOWN = Decision(
    OutcomeClass.UNKNOWN, "uncertain", "provider_outcome_unknown", False
)


def _libs() -> tuple:
    return tuple(m for m in (httpx, httpx2) if m is not None)


# Nothing was written to the wire: connection refused / DNS / connect timeout, or no pooled
# connection acquired.
_NOT_SENT: tuple[type[BaseException], ...] = tuple(
    cls
    for lib in _libs()
    for cls in (lib.ConnectError, lib.ConnectTimeout, lib.PoolTimeout)
)
# Any other transport failure or timeout: the request may have reached the provider.
_MAYBE_SENT: tuple[type[BaseException], ...] = (
    *(lib.TransportError for lib in _libs()),
    TimeoutError,  # also asyncio.TimeoutError on Python 3.11+
)


def classify_response(*, has_usage: bool, tier_priced: bool) -> Decision:
    """A provider response arrived. Settle only with reported usage on a priced tier."""
    if not has_usage:
        return _UNKNOWN
    return _SETTLED if tier_priced else _UNPRICED


def _chain_verdict(exc: BaseException) -> Literal["not_sent", "maybe_sent", "none"]:
    """The outermost recognised error in the cause/context graph decides."""
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, _NOT_SENT):
            return "not_sent"
        if isinstance(node, _MAYBE_SENT):
            return "maybe_sent"
        # Pop order: __cause__ first, then __context__.
        if node.__context__ is not None:
            stack.append(node.__context__)
        if node.__cause__ is not None:
            stack.append(node.__cause__)
    return "none"


def classify_error(
    *,
    exc: BaseException | None,
    status: int | None,
    error_type: str | None,
    error_code: str | None,
) -> Decision:
    """Classify a failed provider call. An HTTP ``status`` wins over ``exc``."""
    if status is not None:
        if status == 429:
            # Quota signals beat transient ones: when both appear, blocking is the safe side.
            if error_type in _QUOTA_TYPES or error_code in _QUOTA_CODES:
                return _QUOTA
            if error_type == "rate_limit_error" or error_code in _TRANSIENT_CODES:
                return _RATE_LIMITED
            return _QUOTA  # any unclassified 429
        if 400 <= status < 500:
            return _REJECTED
        return _UNKNOWN  # 5xx and anything else
    if exc is not None and _chain_verdict(exc) == "not_sent":
        return _CONNECT_FAILED
    return _UNKNOWN


_NUMERIC = re.compile(r"\d+(?:\.\d+)?")
_DEFAULT_DELAY_S = 1.0


def rate_limit_delay(
    retry_after: str | None, remaining_s: float, min_attempt_s: float = 5.0
) -> float | None:
    """Seconds to wait before the single 429 retry, or ``None`` if it does not fit the deadline.

    Only a non-negative, finite delta-seconds ``Retry-After`` is honoured; absent, HTTP-date,
    negative or otherwise non-numeric values use 1.0 s. The retry needs ``delay + min_attempt_s``
    to fit in ``remaining_s``.
    """
    delay = _DEFAULT_DELAY_S
    if retry_after is not None:
        text = retry_after.strip()
        if _NUMERIC.fullmatch(text):
            parsed = float(text)
            if math.isfinite(parsed):
                delay = parsed
    if delay + min_attempt_s <= remaining_s:
        return delay
    return None
