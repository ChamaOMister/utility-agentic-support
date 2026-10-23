"""Offline HTTP turns with real fault-enabled billing, never a paid provider."""

import time
from uuid import uuid4

import pytest
from support_api.catalog import MESSAGES

from tests.conftest import persona_creds_text


@pytest.mark.parametrize(
    "effect,error_code",
    [
        ("timeout", "upstream_timeout"),
        ("http_500", "upstream_unavailable"),
        ("unavailable", "upstream_unavailable"),
    ],
)
async def test_billing_faults_recoverable(
    effect, error_code, live_billing_api_factory, set_fault, personas
):
    api = await live_billing_api_factory()
    persona = personas[4]
    verified = await api.send(uuid4(), persona_creds_text(persona))
    assert verified.status_code == 200
    assert verified.json()["trace"]["tool_calls"][0]["status"] == "ok"
    await set_fault("list_bills", persona["customer_ref"], effect)
    started = time.monotonic()
    response = await api.send(uuid4(), "my bills")
    elapsed = time.monotonic() - started
    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == MESSAGES["upstream_unavailable"]
    assert "R$" not in body["reply"]
    trace = body["trace"]["tool_calls"][-1]
    assert trace["name"] == "list_bills"
    assert trace["error_code"] == error_code
    assert 1 <= trace["attempts"] <= 3
    assert (
        elapsed <= 10
    )  # billing's 8 s total plus local HTTP/DB overhead; turn cap is 45 s

    # Recoverability means this verified conversation succeeds after clearing the outage.
    await set_fault()
    recovered = await api.send(uuid4(), "my bills")
    assert recovered.status_code == 200
    assert "R$" in recovered.json()["reply"]
    assert recovered.json()["trace"]["tool_calls"][-1]["error_code"] is None
