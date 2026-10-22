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
  with ``__cause__`` set; real chain: ``APIConnectionError`` -> ``httpx2.ConnectError`` ->
  ``httpcore2.ConnectError`` -> ``ConnectionRefusedError``), while code and tests that inject a
  legacy ``httpx.AsyncClient`` raise ``httpx`` errors. The not-sent types of ``httpx``, ``httpx2``,
  ``httpcore`` and ``httpcore2`` are all matched; each optional import is defensive.
* The chain is followed ONLY through ``openai.APIConnectionError`` (incl. ``APITimeoutError``),
  stepping to ``__cause__`` or, absent that, ``__context__``, with a cycle guard. The first
  non-wrapper decides: a not-sent type -> ``CONNECT_FAILED``; anything else -> ``UNKNOWN``. An
  unrecognised exception (``ValueError``, ``ssl.SSLError``, ``ConnectionResetError``,
  ``JSONDecodeError``, ``APIResponseValidationError``...) is never looked through: a
  ``ConnectError`` in its ``__context__`` does not release, because a later request may have been
  sent. Likewise a ``ReadTimeout`` or ``asyncio.TimeoutError`` over an old ``ConnectError``.
* A ``Retry-After`` HTTP-date is treated as non-numeric (delay 1.0 s); only delta-seconds are read.
"""

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

import httpcore
import httpx
import openai

try:  # the pinned openai SDK uses httpx2 (and httpcore2 beneath it) internally
    import httpx2
except ImportError:  # pragma: no cover - depends on the installed openai version
    httpx2 = None  # type: ignore[assignment]
try:
    import httpcore2
except ImportError:  # pragma: no cover - depends on the installed openai version
    httpcore2 = None  # type: ignore[assignment]


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


# Nothing was written to the wire: connection refused / DNS / connect timeout, or no pooled
# connection acquired. Every other exception (read/write timeout, protocol error, anything
# unrecognised) is "may have been sent" and classifies UNKNOWN.
_NOT_SENT: tuple[type[BaseException], ...] = tuple(
    cls
    for lib in (httpx, httpx2, httpcore, httpcore2)
    if lib is not None
    for cls in (lib.ConnectError, lib.ConnectTimeout, lib.PoolTimeout)
)


def classify_response(*, has_usage: bool, tier_priced: bool) -> Decision:
    """A provider response arrived. Settle only with reported usage on a priced tier."""
    if not has_usage:
        return _UNKNOWN
    return _SETTLED if tier_priced else _UNPRICED


def _connect_failed(exc: BaseException) -> bool:
    """True only when ``exc`` is, or is wrapped solely by SDK wrappers around, a not-sent error.

    Start at the raised exception. While it is an ``openai.APIConnectionError`` (which includes
    ``APITimeoutError``) step to its ``__cause__``, or to ``__context__`` when there is no cause.
    The first non-wrapper decides: a known not-sent type is True; anything else (a read/write
    timeout, a protocol error, ``ValueError``, ``ssl.SSLError``, ``JSONDecodeError``...) is False.
    The chain is never followed through an unrecognised exception.
    """
    seen: set[int] = set()
    node: BaseException | None = exc
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        if isinstance(node, _NOT_SENT):
            return True
        if not isinstance(node, openai.APIConnectionError):
            return False
        node = node.__cause__ if node.__cause__ is not None else node.__context__
    return False


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
    if exc is not None and _connect_failed(exc):
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
