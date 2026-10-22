"""Capture the actual JSON log pipeline during complete synthetic offline journeys."""

import json
from uuid import uuid4

from support_api.catalog import MESSAGES
from support_api.logging import ALLOWED_KEYS

from tests.conftest import persona_creds_text


def persona_values(personas):
    values = set()
    for p in personas:
        cpf = p["cpf"]
        values.update(
            (
                p["first_name"],
                p["full_name"],
                cpf,
                p["account_number"],
                p["birth_date"],
                p["phone"],
            )
        )
        values.add(f"{cpf[:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:]}")
        y, m, d = p["birth_date"].split("-")
        values.add(f"{d}/{m}/{y}")
        values.add(p["address"]["street"])
    return values


async def test_logs_have_correlation_ids_and_no_persona_values(
    live_billing_api_factory, set_fault, personas, caplog_json
):
    secrets = persona_values(personas)
    responses = []
    for persona in personas:
        api = await live_billing_api_factory()
        secrets.add(api.http.cookies.get("uas_session"))
        responses.append(await api.send(uuid4(), "minhas faturas"))
        verified = await api.send(uuid4(), persona_creds_text(persona))
        responses.append(verified)
        assert verified.status_code == 200
        bills = await api.send(uuid4(), "minhas faturas")
        responses.append(bills)
        assert bills.status_code == 200 and "R$" in bills.json()["reply"]
        if persona is personas[-1]:
            await set_fault("list_bills", persona["customer_ref"], "unavailable")
            failed = await api.send(uuid4(), "minhas faturas")
            responses.append(failed)
            assert failed.json()["reply"] == MESSAGES["upstream_unavailable"]
    records = [json.loads(line) for line in caplog_json.getvalue().splitlines()]
    support_records = [
        rec for rec in records if rec["logger"].startswith("support_api")
    ]
    assert support_records
    assert all(rec.keys() <= ALLOWED_KEYS for rec in records)
    assert all(rec.get("correlation_id") for rec in support_records)
    cids = {rec["correlation_id"] for rec in support_records}
    assert all(r.headers["X-Correlation-ID"] in cids for r in responses)
    assert {
        "support_api.request",
        "support_api.billing",
        "support_api.tools",
        "support_api.turns",
    } <= {rec["logger"] for rec in support_records}
    assert not any(value in caplog_json.getvalue() for value in secrets)
