import asyncio
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from support_api import logging as uas_logging
from support_api.deadline import Deadline
from support_api.integrations.billing.client import BillingClient, BillingError

BASE = "http://billing.test"
ACCOUNT_URL = f"{BASE}/v1/customers/cus_a/account"
BILLS_URL = f"{BASE}/v1/customers/cus_a/bills"
BILL_URL = f"{BASE}/v1/customers/cus_a/bills/b1"
VERIFY_URL = f"{BASE}/v1/identity/verify"

ACCOUNT_JSON = {
    "customer_ref": "cus_a",
    "masked_cpf": "***.***.***-12",
    "installations": [{"installation_id": "i1", "label": "Casa"}],
    "address": {
        "street": "Rua X",
        "neighborhood": "Centro",
        "city": "Recife",
        "state": "PE",
    },
    "tariff_class": "residencial",
    "account_status": "active",
}
BILL_JSON = {
    "bill_id": "b1",
    "installation_id": "i1",
    "reference_month": "2026-09",
    "due_date": "2026-10-10",
    "amount_brl": "123.45",
    "status": "open",
    "consumption_kwh": 200,
    "tariff_flag": "verde",
}
BILL_DETAIL_JSON = {
    **BILL_JSON,
    "issue_date": "2026-09-25",
    "line_items": [{"description": "Energia", "amount_brl": "123.45"}],
}


class FakeSleep:
    def __init__(self, clock=None) -> None:
        self.calls: list[float] = []
        self._clock = clock

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self._clock is not None:
            self._clock.advance(seconds)


@pytest.fixture
def fake_sleep() -> FakeSleep:
    return FakeSleep()


def make_client(fake_sleep, **kw) -> BillingClient:
    kw.setdefault("sleep", fake_sleep)
    kw.setdefault("rng", lambda: 0.0)
    return BillingClient(BASE, "svc-key", **kw)


@pytest.fixture
def client(fake_sleep):
    return make_client(fake_sleep)


class ClaimLostStub(Exception):
    pass


async def test_get_retries_twice_then_unavailable(respx_mock, fake_sleep):
    client = make_client(fake_sleep, rng=lambda: 0.5)
    route = respx_mock.get(ACCOUNT_URL).mock(side_effect=[httpx.Response(500)] * 3)
    with pytest.raises(BillingError) as e:
        await client.get_account("cus_a", deadline=Deadline(45))
    assert e.value.code == "upstream_unavailable"
    assert route.call_count == 3 and e.value.attempts == 3
    assert [round(s, 1) for s in fake_sleep.calls] in (
        [0.2, 0.6], [0.2, 0.7], [0.3, 0.6], [0.3, 0.7],
    )  # fmt: skip
    assert fake_sleep.calls == pytest.approx(
        [0.2 * 1.125, 0.6 * 1.125]
    )  # base * (1 + 0.25 * rng)


async def test_retry_then_success_reports_attempts(respx_mock, client):
    route = respx_mock.get(ACCOUNT_URL).mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json=ACCOUNT_JSON)]
    )
    result = await client.get_account("cus_a", deadline=Deadline(45))
    assert route.call_count == 2 and result.attempts == 2
    assert result.value.customer_ref == "cus_a"
    assert result.value.installations[0].label == "Casa"


async def test_last_failure_decides_code(respx_mock, client):
    respx_mock.get(ACCOUNT_URL).mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(503),
            httpx.ReadTimeout("slow"),
        ]
    )
    with pytest.raises(BillingError) as e:
        await client.get_account("cus_a", deadline=Deadline(45))
    assert e.value.code == "upstream_timeout" and e.value.attempts == 3


async def test_verify_post_never_retried(respx_mock, client, fake_sleep):
    route = respx_mock.post(VERIFY_URL).mock(return_value=httpx.Response(503))
    from datetime import date

    with pytest.raises(BillingError) as e:
        await client.verify(
            "12345678909", date(1980, 1, 2), "1234", deadline=Deadline(45)
        )
    assert e.value.code == "upstream_unavailable" and e.value.attempts == 1
    assert route.call_count == 1 and fake_sleep.calls == []


async def test_verify_success_and_request_body(respx_mock, client):
    from datetime import date

    route = respx_mock.post(VERIFY_URL).mock(
        return_value=httpx.Response(
            200, json={"verified": True, "customer_ref": "cus_a", "first_name": "Ana"}
        )
    )
    r = await client.verify(
        "12345678909", date(1980, 1, 2), "1234", deadline=Deadline(45)
    )
    assert r.attempts == 1
    assert (
        r.value.verified
        and r.value.customer_ref == "cus_a"
        and r.value.first_name == "Ana"
    )
    import json

    assert json.loads(route.calls[0].request.content) == {
        "identifier": "12345678909",
        "birth_date": "1980-01-02",
        "phone_last4": "1234",
    }
    respx_mock.post(VERIFY_URL).mock(
        return_value=httpx.Response(200, json={"verified": False})
    )
    r = await client.verify(
        "12345678909", date(1980, 1, 2), "9999", deadline=Deadline(45)
    )
    assert r.value.verified is False and r.value.customer_ref is None


async def test_timeout_capped_by_turn_deadline(respx_mock, client):
    seen: list[dict] = []

    def respond(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json=ACCOUNT_JSON)

    respx_mock.get(ACCOUNT_URL).mock(side_effect=respond)
    await client.get_account("cus_a", deadline=Deadline(1))
    assert seen and all(0 < v <= 1 for v in seen[0].values())


async def test_per_attempt_timeout_is_3s_with_ample_deadline(respx_mock, client):
    seen: list[dict] = []

    def respond(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json=ACCOUNT_JSON)

    respx_mock.get(ACCOUNT_URL).mock(side_effect=respond)
    await client.get_account("cus_a", deadline=Deadline(45))
    assert set(seen[0].values()) == {3}


async def test_tool_total_deadline_8s(respx_mock, fake_clock):
    sleep = FakeSleep(fake_clock)
    client = make_client(sleep, clock=fake_clock)
    timeouts: list[float] = []

    def timed_out(request):
        t = request.extensions["timeout"]["read"]
        timeouts.append(t)
        fake_clock.advance(t)  # the attempt consumes its whole allowance
        raise httpx.ReadTimeout("slow")

    route = respx_mock.get(ACCOUNT_URL).mock(side_effect=timed_out)
    started = fake_clock.now
    with pytest.raises(BillingError) as e:
        await client.get_account("cus_a", deadline=Deadline(45, clock=fake_clock))
    assert e.value.code == "upstream_timeout" and route.call_count == 3
    # three 3 s attempts would be 9 s + sleeps; the tool budget caps the last attempt instead.
    assert timeouts[:2] == [3, 3] and timeouts[2] == pytest.approx(
        8 - 3 - 0.2 - 3 - 0.6
    )
    assert fake_clock.now - started <= 8 + 1e-9


async def test_backoff_skipped_when_it_cannot_fit_the_remaining_deadline(
    respx_mock, fake_clock
):
    """Pin: a retry sleeps only if sleep + minimum useful attempt (0.5 s) fits in what remains."""
    sleep = FakeSleep(fake_clock)
    client = make_client(sleep, clock=fake_clock)

    def timed_out(request):
        fake_clock.advance(request.extensions["timeout"]["read"])
        raise httpx.ReadTimeout("slow")

    route = respx_mock.get(ACCOUNT_URL).mock(side_effect=timed_out)
    turn = Deadline(7, clock=fake_clock)  # 3 + 0.2 + 3 leaves 0.8 s; 0.6 + 0.5 > 0.8
    with pytest.raises(BillingError) as e:
        await client.get_account("cus_a", deadline=turn)
    assert route.call_count == 2 and e.value.attempts == 2
    assert e.value.code == "upstream_timeout"
    assert sleep.calls == [0.2]  # the 0.6 s backoff was not slept


async def test_backoff_slept_when_it_fits_and_skipped_just_below(
    respx_mock, fake_clock
):
    sleep = FakeSleep(fake_clock)
    client = make_client(sleep, clock=fake_clock)

    def timed_out(request):
        fake_clock.advance(request.extensions["timeout"]["read"])
        raise httpx.ReadTimeout("slow")

    respx_mock.get(ACCOUNT_URL).mock(side_effect=timed_out)
    turn = Deadline(
        7.1, clock=fake_clock
    )  # remaining 0.9 after two attempts; 0.6 + 0.5 = 1.1 > 0.9
    with pytest.raises(BillingError):
        await client.get_account("cus_a", deadline=turn)
    assert sleep.calls == [0.2]
    sleep.calls.clear()
    fake_clock.now = 1000.0
    turn = Deadline(
        7.2 + 0.2, clock=fake_clock
    )  # remaining 1.2 after two attempts; 1.1 fits
    respx_mock.reset()
    route = respx_mock.get(ACCOUNT_URL).mock(side_effect=timed_out)
    with pytest.raises(BillingError) as e:
        await client.get_account("cus_a", deadline=turn)
    assert route.call_count == 3 and e.value.attempts == 3 and sleep.calls == [0.2, 0.6]


async def test_expired_turn_deadline_sends_nothing(respx_mock, client, fake_clock):
    route = respx_mock.get(ACCOUNT_URL).mock(
        return_value=httpx.Response(200, json=ACCOUNT_JSON)
    )
    turn = Deadline(1, clock=fake_clock)
    fake_clock.advance(5)
    with pytest.raises(BillingError) as e:
        await client.get_account("cus_a", deadline=turn)
    assert e.value.code == "upstream_timeout" and e.value.attempts == 0
    assert route.call_count == 0


async def test_404_maps_to_not_found_without_retry(respx_mock, client, fake_sleep):
    route = respx_mock.get(BILL_URL).mock(
        return_value=httpx.Response(404, json={"error": "not_found"})
    )
    with pytest.raises(BillingError) as e:
        await client.get_bill("cus_a", "b1", deadline=Deadline(45))
    assert e.value.code == "not_found" and e.value.attempts == 1
    assert route.call_count == 1 and fake_sleep.calls == []


@pytest.mark.parametrize("status", [401, 403, 422])
async def test_other_4xx_is_unavailable_without_retry(
    respx_mock, client, fake_sleep, status
):
    route = respx_mock.get(ACCOUNT_URL).mock(return_value=httpx.Response(status))
    with pytest.raises(BillingError) as e:
        await client.get_account("cus_a", deadline=Deadline(45))
    assert e.value.code == "upstream_unavailable" and route.call_count == 1
    assert fake_sleep.calls == []


async def test_malformed_body_is_unavailable_without_retry(respx_mock, client):
    route = respx_mock.get(ACCOUNT_URL).mock(
        return_value=httpx.Response(200, json={"x": 1})
    )
    with pytest.raises(BillingError) as e:
        await client.get_account("cus_a", deadline=Deadline(45))
    assert e.value.code == "upstream_unavailable" and route.call_count == 1


async def test_guard_runs_before_every_attempt(respx_mock, fake_sleep):
    client = make_client(fake_sleep)
    route = respx_mock.get(ACCOUNT_URL).mock(side_effect=[httpx.Response(500)] * 3)
    guard = AsyncMock(side_effect=[None, ClaimLostStub()])  # second check fails
    with pytest.raises(ClaimLostStub):
        await client.get_account("cus_a", deadline=Deadline(45), guard=guard)
    assert route.call_count == 1 and guard.await_count == 2  # retry never sent


async def test_guard_runs_after_backoff_sleep(respx_mock):
    order: list[str] = []

    async def sleep(s):
        order.append("sleep")

    async def guard():
        order.append("guard")

    client = BillingClient(BASE, "k", sleep=sleep, rng=lambda: 0.0)
    respx_mock.get(ACCOUNT_URL).mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json=ACCOUNT_JSON)]
    )
    await client.get_account("cus_a", deadline=Deadline(45), guard=guard)
    assert order == ["guard", "sleep", "guard"]


async def test_guard_failure_on_first_attempt_sends_nothing(respx_mock, client):
    route = respx_mock.get(ACCOUNT_URL).mock(
        return_value=httpx.Response(200, json=ACCOUNT_JSON)
    )
    with pytest.raises(ClaimLostStub):
        await client.get_account(
            "cus_a", deadline=Deadline(45), guard=AsyncMock(side_effect=ClaimLostStub())
        )
    assert route.call_count == 0


async def test_attempt_total_time_bounded(respx_mock, fake_sleep):
    """A trickling response whose every piece arrives within the phase timeout is still cut at the
    per-attempt total (asyncio.timeout), not allowed to run for the sum of its pieces."""
    client = make_client(fake_sleep, attempt_timeout=0.1)
    started_count = completed = 0

    async def trickle(request):
        nonlocal started_count, completed
        started_count += 1
        for _ in range(10):  # 10 x 0.03 s = 0.3 s total; each piece is far below 0.1 s
            await asyncio.sleep(0.03)
        completed += 1
        return httpx.Response(200, json=ACCOUNT_JSON)

    respx_mock.get(ACCOUNT_URL).mock(side_effect=trickle)
    started = time.monotonic()
    with pytest.raises(BillingError) as e:
        await client.get_account("cus_a", deadline=Deadline(45))
    elapsed = time.monotonic() - started
    assert e.value.code == "upstream_timeout" and e.value.attempts == 3
    assert started_count == 3 and completed == 0
    assert elapsed < 3 * 0.3 * 0.9  # three cut-off attempts, not three full trickles


async def test_sends_api_key_and_correlation_id(respx_mock, client):
    route = respx_mock.get(ACCOUNT_URL).mock(
        return_value=httpx.Response(200, json=ACCOUNT_JSON)
    )
    token = uas_logging.correlation_id.set("corr-12345678")
    try:
        await client.get_account("cus_a", deadline=Deadline(45))
    finally:
        uas_logging.correlation_id.reset(token)
    headers = route.calls[0].request.headers
    assert headers["x-api-key"] == "svc-key"
    assert headers["x-correlation-id"] == "corr-12345678"


async def test_correlation_id_omitted_when_unset(respx_mock, client):
    route = respx_mock.get(ACCOUNT_URL).mock(
        return_value=httpx.Response(200, json=ACCOUNT_JSON)
    )
    await client.get_account("cus_a", deadline=Deadline(45))
    assert "x-correlation-id" not in route.calls[0].request.headers


async def test_list_bills_params_and_parsing(respx_mock, client):
    route = respx_mock.get(BILLS_URL).mock(
        return_value=httpx.Response(200, json=[BILL_JSON])
    )
    r = await client.list_bills("cus_a", status="open", limit=5, deadline=Deadline(45))
    assert dict(route.calls[0].request.url.params) == {"status": "open", "limit": "5"}
    assert str(r.value[0].amount_brl) == "123.45" and r.value[0].status == "open"
    await client.list_bills("cus_a", status=None, limit=12, deadline=Deadline(45))
    assert dict(route.calls[1].request.url.params) == {"limit": "12"}


async def test_get_bill_parsing(respx_mock, client):
    respx_mock.get(BILL_URL).mock(
        return_value=httpx.Response(200, json=BILL_DETAIL_JSON)
    )
    r = await client.get_bill("cus_a", "b1", deadline=Deadline(45))
    assert r.value.line_items[0].description == "Energia"
    assert r.value.issue_date.isoformat() == "2026-09-25"


async def test_failed_attempts_are_logged_without_payload(respx_mock, client, caplog):
    respx_mock.get(ACCOUNT_URL).mock(return_value=httpx.Response(401))
    with (
        caplog.at_level("INFO", logger="support_api.billing"),
        pytest.raises(BillingError),
    ):
        await client.get_account("cus_a", deadline=Deadline(45))
    rec = [r for r in caplog.records if r.name == "support_api.billing"][-1]
    assert rec.http_status == 401 and rec.error_code == "upstream_unavailable"
    assert rec.attempt == 1 and rec.tool_name == "get_account"
