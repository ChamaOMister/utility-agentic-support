"""Identifier admission and lockout through distinct cookie jars and the real billing API."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import text
from support_api.catalog import MESSAGES

from tests.conftest import conv_auth, identifier_row, persona_creds_text, persona_hash


def wrong_credentials(persona, identifier):
    return (
        persona_creds_text(persona)
        .replace(persona_creds_text(persona).split(",")[0], f"CPF {identifier}")
        .replace(f"ending {persona['phone'][-4:]}", "ending 0000")
    )


async def attempts_for(db, apis):
    async with db.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT status FROM support.verification_attempts WHERE conversation_id = ANY(:cs)"
            ),
            {"cs": [api.conversation_id for api in apis]},
        )
        return list(rows.scalars())


async def test_lockout_across_sessions_and_formats(
    live_billing_api_factory, db, personas
):
    a = personas[0]
    cpf = a["cpf"]
    formatted = f"{cpf[:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:]}"
    first = await live_billing_api_factory()
    for _ in range(3):
        result = await first.send(uuid4(), wrong_credentials(a, formatted))
        assert result.status_code == 200
        assert result.json()["reply"] == MESSAGES["auth_failed"]
    assert len(first.billing_routes.verify_requests) == 3
    assert (await conv_auth(db, first.conversation_id)).locked_until is not None
    for identifier in (cpf, " ".join((cpf[:3], cpf[3:6], cpf[6:9], cpf[9:]))):
        second = await live_billing_api_factory()
        assert second.http.cookies.get("uas_session") != first.http.cookies.get(
            "uas_session"
        )
        result = await second.send(uuid4(), wrong_credentials(a, identifier))
        assert result.status_code == 200
        assert result.json()["reply"] == MESSAGES["auth_failed"]
        assert result.json()["trace"]["tool_calls"][-1]["error_code"] == "locked"
        assert not second.billing_routes.verify_requests
    assert (await identifier_row(db, persona_hash(a))).failed_count == 3


async def test_concurrent_lockout_across_ten_sessions(
    live_billing_api_factory, set_fault, db, personas
):
    a = personas[0]
    await set_fault("verify", None, "delay_ms:500")
    apis = [await live_billing_api_factory() for _ in range(10)]
    assert len({api.http.cookies.get("uas_session") for api in apis}) == 10
    results = await asyncio.wait_for(
        asyncio.gather(
            *(api.send(uuid4(), wrong_credentials(a, a["cpf"])) for api in apis)
        ),
        10,
    )
    assert all(
        r.status_code == 200 and r.json()["reply"] == MESSAGES["auth_failed"]
        for r in results
    )
    assert sum(len(api.billing_routes.verify_requests) for api in apis) == 3
    assert await attempts_for(db, apis) == ["failed"] * 3
    row = await identifier_row(db, persona_hash(a))
    assert row.failed_count == 3 and row.locked_until is not None


@pytest.mark.parametrize("effect", ["timeout", "http_500", "unavailable"])
async def test_verification_upstream_error_frees_slot_without_counting(
    effect, live_billing_api_factory, set_fault, db, personas
):
    api = await live_billing_api_factory()
    a = personas[0]
    await set_fault("verify", None, effect)
    response = await api.send(uuid4(), wrong_credentials(a, a["cpf"]))
    assert response.status_code == 200
    assert response.json()["reply"] == MESSAGES["upstream_unavailable"]
    assert len(api.billing_routes.verify_requests) == 1  # verify POST is never retried
    assert await attempts_for(db, [api]) == ["upstream_error"]
    row = await identifier_row(db, persona_hash(a))
    auth = await conv_auth(db, api.conversation_id)
    assert row.failed_count == auth.failed_attempts == 0
    assert row.locked_until is auth.locked_until is None
    await set_fault()
    result = await api.send(uuid4(), persona_creds_text(a))
    assert result.status_code == 200
    assert (await conv_auth(db, api.conversation_id)).assurance_level == "kba"


async def test_upstream_503_during_concurrent_verification_counts_nothing(
    live_billing_api_factory, set_fault, db, personas
):
    a = personas[0]
    await set_fault("verify", None, "unavailable")
    # Hold each real 503 response for 500 ms, ensuring admission overlaps just as for wrong answers.
    apis = [
        await live_billing_api_factory(verify_response_delay=0.5) for _ in range(10)
    ]
    results = await asyncio.wait_for(
        asyncio.gather(
            *(api.send(uuid4(), wrong_credentials(a, a["cpf"])) for api in apis)
        ),
        10,
    )
    assert all(r.status_code == 200 for r in results)
    assert all(
        r.json()["reply"] in (MESSAGES["auth_failed"], MESSAGES["upstream_unavailable"])
        for r in results
    )
    sent = sum(len(api.billing_routes.verify_requests) for api in apis)
    assert sent == 3
    assert await attempts_for(db, apis) == ["upstream_error"] * sent
    row = await identifier_row(db, persona_hash(a))
    assert row.failed_count == 0 and row.locked_until is None
    # Capacity rejection can precede creation of conversation_auth; missing means no failures.
    async with db.begin() as conn:
        counters = (
            await conn.execute(
                text(
                    "SELECT COALESCE(a.failed_attempts, 0), a.locked_until "
                    "FROM support.conversations c LEFT JOIN support.conversation_auth a "
                    "ON a.conversation_id = c.id WHERE c.id = ANY(:cs)"
                ),
                {"cs": [api.conversation_id for api in apis]},
            )
        ).all()
    assert len(counters) == 10
    assert all(failures == 0 and locked is None for failures, locked in counters)
    await set_fault()
    recovered = await apis[0].send(uuid4(), persona_creds_text(a))
    assert recovered.status_code == 200
    assert (await conv_auth(db, apis[0].conversation_id)).assurance_level == "kba"
