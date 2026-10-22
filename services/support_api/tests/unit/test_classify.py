"""Provider outcome classification (spec §5.6 table, row for row). Pure: no DB, no network."""

import httpx
import httpx2
import openai
import pytest
from support_api.usage.classify import (
    Decision,
    OutcomeClass,
    classify_error,
    classify_response,
    rate_limit_delay,
)

UNCERTAIN = "uncertain"


def _as_tuple(d: Decision) -> tuple[str, str | None, bool]:
    return (d.ledger, d.block_reason, d.retryable)


def _chain(
    outer: BaseException, inner: BaseException, *, how: str = "cause"
) -> BaseException:
    setattr(outer, "__cause__" if how == "cause" else "__context__", inner)
    return outer


@pytest.mark.parametrize(
    "status,etype,ecode,expected",
    [
        (429, "rate_limit_error", None, (UNCERTAIN, None, True)),
        (429, None, "slow_down", (UNCERTAIN, None, True)),
        (429, None, "rate_limit_exceeded", (UNCERTAIN, None, True)),
        (429, "insufficient_quota", None, (UNCERTAIN, "provider_quota", False)),
        (429, None, "credit_balance_exhausted", (UNCERTAIN, "provider_quota", False)),
        (
            429,
            None,
            "organization_spend_limit_exceeded",
            (UNCERTAIN, "provider_quota", False),
        ),
        (
            429,
            None,
            "project_spend_limit_exceeded",
            (UNCERTAIN, "provider_quota", False),
        ),
        (
            429,
            None,
            "organization_usage_limit_exceeded",
            (UNCERTAIN, "provider_quota", False),
        ),
        (
            429,
            None,
            "something_new",
            (UNCERTAIN, "provider_quota", False),
        ),  # unclassified 429
        (429, None, None, (UNCERTAIN, "provider_quota", False)),  # unclassified 429
        (429, "other_type", "other_code", (UNCERTAIN, "provider_quota", False)),
        (400, None, None, (UNCERTAIN, "provider_rejected", False)),
        (401, None, None, (UNCERTAIN, "provider_rejected", False)),
        (403, None, None, (UNCERTAIN, "provider_rejected", False)),
        (404, None, None, (UNCERTAIN, "provider_rejected", False)),  # other 4xx
        (422, None, None, (UNCERTAIN, "provider_rejected", False)),  # other 4xx
        (500, None, None, (UNCERTAIN, "provider_outcome_unknown", False)),
        (503, None, None, (UNCERTAIN, "provider_outcome_unknown", False)),
    ],
)
def test_http_error_rows(status, etype, ecode, expected):
    decision = classify_error(
        exc=None, status=status, error_type=etype, error_code=ecode
    )
    assert _as_tuple(decision) == expected


@pytest.mark.parametrize(
    "status,etype,ecode,outcome",
    [
        (429, "rate_limit_error", None, OutcomeClass.RATE_LIMITED),
        (429, None, "slow_down", OutcomeClass.RATE_LIMITED),
        (429, "insufficient_quota", None, OutcomeClass.QUOTA),
        (429, None, "something_new", OutcomeClass.QUOTA),
        (400, None, None, OutcomeClass.REJECTED),
        (503, None, None, OutcomeClass.UNKNOWN),
    ],
)
def test_http_error_outcome_classes(status, etype, ecode, outcome):
    decision = classify_error(
        exc=None, status=status, error_type=etype, error_code=ecode
    )
    assert decision.outcome is outcome


def test_quota_signal_wins_over_transient_signal():
    # A body that carries both signals must block, never retry (the safe direction).
    d = classify_error(
        exc=None,
        status=429,
        error_type="rate_limit_error",
        error_code="insufficient_quota",
    )
    assert d.outcome is OutcomeClass.QUOTA and d.retryable is False
    d = classify_error(
        exc=None,
        status=429,
        error_type="insufficient_quota",
        error_code="rate_limit_exceeded",
    )
    assert d.outcome is OutcomeClass.QUOTA and d.retryable is False


def test_error_type_and_code_only_matter_for_429():
    d = classify_error(
        exc=None, status=500, error_type="rate_limit_error", error_code="slow_down"
    )
    assert d.outcome is OutcomeClass.UNKNOWN
    d = classify_error(
        exc=None, status=400, error_type="insufficient_quota", error_code=None
    )
    assert d.outcome is OutcomeClass.REJECTED


def test_status_wins_over_exception():
    exc = openai.APIConnectionError(request=httpx.Request("POST", "http://x"))
    exc.__cause__ = httpx.ConnectError("boom")
    d = classify_error(exc=exc, status=503, error_type=None, error_code=None)
    assert d.outcome is OutcomeClass.UNKNOWN


def test_connect_error_releases_and_retries():
    exc = RuntimeError("wrapped")
    exc.__cause__ = httpx.ConnectError("refused")
    d = classify_error(exc=exc, status=None, error_type=None, error_code=None)
    assert d.outcome is OutcomeClass.CONNECT_FAILED
    assert _as_tuple(d) == ("release", None, True)


@pytest.mark.parametrize(
    "inner",
    [
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout("slow"),
        httpx2.ConnectError("refused"),
        httpx2.ConnectTimeout("slow"),
    ],
)
@pytest.mark.parametrize("how", ["cause", "context"])
def test_connect_errors_found_via_cause_or_context(inner, how):
    exc = _chain(RuntimeError("outer"), inner, how=how)
    d = classify_error(exc=exc, status=None, error_type=None, error_code=None)
    assert d.outcome is OutcomeClass.CONNECT_FAILED


def test_bare_connect_error_itself_is_connect_failed():
    d = classify_error(
        exc=httpx.ConnectError("x"), status=None, error_type=None, error_code=None
    )
    assert d.outcome is OutcomeClass.CONNECT_FAILED


@pytest.mark.parametrize("inner_factory", [httpx.ConnectError, httpx2.ConnectError])
def test_openai_connection_error_with_connect_cause_is_connect_failed(inner_factory):
    exc = openai.APIConnectionError(request=httpx.Request("POST", "http://x"))
    exc.__cause__ = inner_factory("refused")
    d = classify_error(exc=exc, status=None, error_type=None, error_code=None)
    assert d.outcome is OutcomeClass.CONNECT_FAILED
    assert _as_tuple(d) == ("release", None, True)


def test_openai_timeout_caused_by_connect_timeout_is_connect_failed():
    exc = openai.APITimeoutError(request=httpx.Request("POST", "http://x"))
    exc.__cause__ = httpx2.ConnectTimeout("slow connect")
    d = classify_error(exc=exc, status=None, error_type=None, error_code=None)
    assert d.outcome is OutcomeClass.CONNECT_FAILED


def test_bare_openai_connection_error_is_unknown():
    exc = openai.APIConnectionError(request=httpx.Request("POST", "http://x"))
    d = classify_error(exc=exc, status=None, error_type=None, error_code=None)
    assert d.outcome is OutcomeClass.UNKNOWN
    assert _as_tuple(d) == (UNCERTAIN, "provider_outcome_unknown", False)


def test_openai_connection_error_with_unrelated_cause_is_unknown():
    exc = openai.APIConnectionError(request=httpx.Request("POST", "http://x"))
    exc.__cause__ = httpx.RemoteProtocolError("peer closed")
    d = classify_error(exc=exc, status=None, error_type=None, error_code=None)
    assert d.outcome is OutcomeClass.UNKNOWN


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("slow"),
        httpx.WriteTimeout("slow"),
        httpx2.ReadTimeout("slow"),
        httpx2.WriteTimeout("slow"),
        httpx.ReadError("reset"),
        httpx.RemoteProtocolError("closed"),
        openai.APITimeoutError(request=httpx.Request("POST", "http://x")),
        TimeoutError(),  # also asyncio.TimeoutError (an alias on 3.11+)
        ValueError("malformed"),
    ],
)
def test_read_timeout_is_unknown(exc):
    d = classify_error(exc=exc, status=None, error_type=None, error_code=None)
    assert d.outcome is OutcomeClass.UNKNOWN
    assert _as_tuple(d) == (UNCERTAIN, "provider_outcome_unknown", False)


def test_read_timeout_after_earlier_connect_error_stays_unknown():
    # A retry loop can leave an earlier ConnectError in __context__ of a later ReadTimeout.
    # The request may have been sent by then, so the outermost recognised error decides.
    exc = _chain(httpx.ReadTimeout("slow"), httpx.ConnectError("old"), how="context")
    d = classify_error(exc=exc, status=None, error_type=None, error_code=None)
    assert d.outcome is OutcomeClass.UNKNOWN


def test_asyncio_timeout_over_connect_error_context_is_unknown():
    exc = _chain(TimeoutError(), httpx.ConnectError("old"), how="context")
    d = classify_error(exc=exc, status=None, error_type=None, error_code=None)
    assert d.outcome is OutcomeClass.UNKNOWN


@pytest.mark.parametrize("exc_type", [httpx.PoolTimeout, httpx2.PoolTimeout])
def test_pool_timeout_means_not_sent_so_releases(exc_type):
    # A pool timeout means no connection was ever acquired: nothing left the client.
    d = classify_error(
        exc=exc_type("pool"), status=None, error_type=None, error_code=None
    )
    assert d.outcome is OutcomeClass.CONNECT_FAILED
    assert _as_tuple(d) == ("release", None, True)


def test_no_status_no_exception_is_unknown():
    d = classify_error(exc=None, status=None, error_type=None, error_code=None)
    assert d.outcome is OutcomeClass.UNKNOWN
    assert _as_tuple(d) == (UNCERTAIN, "provider_outcome_unknown", False)


def test_cyclic_exception_chain_terminates():
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__cause__ = b
    b.__context__ = a
    d = classify_error(exc=a, status=None, error_type=None, error_code=None)
    assert d.outcome is OutcomeClass.UNKNOWN


def test_cyclic_chain_still_finds_connect_error():
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__cause__ = b
    b.__context__ = a
    b.__cause__ = httpx.ConnectError("x")
    d = classify_error(exc=a, status=None, error_type=None, error_code=None)
    assert d.outcome is OutcomeClass.CONNECT_FAILED


def test_response_with_usage_and_priced_tier_settles():
    d = classify_response(has_usage=True, tier_priced=True)
    assert d.outcome is OutcomeClass.SETTLED
    assert _as_tuple(d) == ("settle", None, False)


def test_unpriced_tier_blocks():
    d = classify_response(has_usage=True, tier_priced=False)
    assert d.outcome is OutcomeClass.UNPRICED_TIER
    assert _as_tuple(d) == (UNCERTAIN, "unpriced_tier", False)


@pytest.mark.parametrize("tier_priced", [True, False])
def test_response_without_usage_is_unknown(tier_priced):
    d = classify_response(has_usage=False, tier_priced=tier_priced)
    assert d.outcome is OutcomeClass.UNKNOWN
    assert _as_tuple(d) == (UNCERTAIN, "provider_outcome_unknown", False)


def test_decision_is_frozen():
    d = classify_response(has_usage=True, tier_priced=True)
    with pytest.raises(AttributeError):
        d.ledger = "release"  # type: ignore[misc]


def test_outcome_class_members_and_values():
    assert {m.name: m.value for m in OutcomeClass} == {
        "SETTLED": "settled",
        "UNPRICED_TIER": "unpriced_tier",
        "CONNECT_FAILED": "connect_failed",
        "RATE_LIMITED": "rate_limited",
        "QUOTA": "quota",
        "REJECTED": "rejected",
        "UNKNOWN": "unknown",
    }


def test_rate_limit_delay_respects_deadline():
    assert rate_limit_delay("2", 10) == 2 and rate_limit_delay("6", 10) is None
    assert rate_limit_delay(None, 6) == 1.0


def test_rate_limit_delay_boundary_is_inclusive():
    assert rate_limit_delay("5", 10) == 5.0  # 5 + 5 <= 10
    assert rate_limit_delay("5.01", 10) is None
    assert rate_limit_delay(None, 5.9) is None  # 1 + 5 > 5.9
    assert rate_limit_delay(None, 6.0) == 1.0


def test_rate_limit_delay_custom_min_attempt():
    assert rate_limit_delay("1", 3, min_attempt_s=2.0) == 1.0
    assert rate_limit_delay("2", 3, min_attempt_s=2.0) is None


@pytest.mark.parametrize(
    "value",
    [
        "abc",
        "",
        "  ",
        "-1",
        "-0.5",
        "nan",
        "inf",
        "-inf",
        "1e3",
        "1_0",
        "Wed, 21 Oct 2026 07:28:00 GMT",
    ],
)
def test_rate_limit_delay_non_numeric_or_negative_defaults_to_one(value):
    assert rate_limit_delay(value, 100) == 1.0


def test_rate_limit_delay_float_and_whitespace():
    assert rate_limit_delay("0", 10) == 0.0
    assert rate_limit_delay("2.5", 10) == 2.5
    assert rate_limit_delay(" 3 ", 10) == 3.0


def test_rate_limit_delay_no_time_left():
    assert rate_limit_delay(None, 0) is None
    assert rate_limit_delay("0", -1) is None
    assert rate_limit_delay("0", float("nan")) is None
