import logging
import re
from decimal import Decimal

import pytest


def _verify_body(identifier, birth, last4):
    return {"identifier": identifier, "birth_date": birth, "phone_last4": last4}


# --- authentication -------------------------------------------------------


async def test_v1_requires_api_key(client, A):
    path = f"/v1/customers/{A.ref}/account"
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers={"X-API-Key": "wrong"})).status_code == 401
    assert (await client.get(path, headers={"X-API-Key": ""})).status_code == 401


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/identity/verify"),
        ("GET", "/v1/customers/cus_x/bills"),
        ("GET", "/v1/customers/cus_x/bills/bill_x"),
    ],
)
async def test_every_v1_route_rejects_missing_key(client, method, path):
    response = await client.request(method, path, json={} if method == "POST" else None)
    assert response.status_code == 401


async def test_health_needs_no_auth(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- identity -------------------------------------------------------------


async def test_verify_unknown_and_wrong_answers_identical(client, auth, A):
    unknown = await client.post(
        "/v1/identity/verify", headers=auth, json=_verify_body("00000000000", A.birth, A.last4)
    )
    wrong_birth = await client.post(
        "/v1/identity/verify", headers=auth, json=_verify_body(A.cpf, "1900-01-01", A.last4)
    )
    wrong_phone = await client.post(
        "/v1/identity/verify", headers=auth, json=_verify_body(A.cpf, A.birth, "0000")
    )
    odd_length = await client.post(
        "/v1/identity/verify", headers=auth, json=_verify_body("12345", A.birth, A.last4)
    )
    for r in (unknown, wrong_birth, wrong_phone, odd_length):
        assert r.status_code == 200
        assert r.json() == {"verified": False}
        assert r.content == unknown.content


async def test_verify_success_by_cpf_and_account(client, auth, A):
    for identifier in (
        A.cpf,
        A.account,
        f"{A.cpf[:3]}.{A.cpf[3:6]}.{A.cpf[6:9]}-{A.cpf[9:]}",  # punctuation is stripped
        f" {A.account[:5]} {A.account[5:]} ",
    ):
        response = await client.post(
            "/v1/identity/verify", headers=auth, json=_verify_body(identifier, A.birth, A.last4)
        )
        assert response.status_code == 200
        assert response.json() == {"verified": True, "customer_ref": A.ref, "first_name": A.first}


async def test_verify_does_not_cross_customers(client, auth, A, B):
    response = await client.post(
        "/v1/identity/verify", headers=auth, json=_verify_body(A.cpf, B.birth, B.last4)
    )
    assert response.json() == {"verified": False}


async def test_verify_rejects_malformed_body(client, auth, A):
    bad_date = await client.post(
        "/v1/identity/verify", headers=auth, json=_verify_body(A.cpf, "not-a-date", A.last4)
    )
    bad_last4 = await client.post(
        "/v1/identity/verify", headers=auth, json=_verify_body(A.cpf, A.birth, "12")
    )
    assert bad_date.status_code == bad_last4.status_code == 422


# --- account --------------------------------------------------------------


async def test_account_masks_cpf(client, auth, A):
    response = await client.get(f"/v1/customers/{A.ref}/account", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert re.fullmatch(r"\*\*\*\.\*\*\*\.\*\*\*-\d\d", body["masked_cpf"])
    assert body["masked_cpf"].endswith(A.cpf[-2:])
    assert A.cpf not in response.text and A.cpf[:9] not in response.text


async def test_account_shape(client, auth, A, C):
    body = (await client.get(f"/v1/customers/{A.ref}/account", headers=auth)).json()
    assert body["customer_ref"] == A.ref
    assert body["tariff_class"] == A.data["tariff_class"]
    assert body["account_status"] == A.data["account_status"]
    assert body["address"] == A.data["address"]
    assert body["installations"] == A.data["installations"]
    assert set(body) == {
        "customer_ref", "masked_cpf", "installations", "address", "tariff_class", "account_status",
    }
    # Persona C has two installations.
    c_body = (await client.get(f"/v1/customers/{C.ref}/account", headers=auth)).json()
    assert c_body["installations"] == C.data["installations"] and len(c_body["installations"]) == 2


async def test_unknown_customer_is_404(client, auth):
    for path in ("account", "bills"):
        r = await client.get(f"/v1/customers/cus_nope/{path}", headers=auth)
        assert r.status_code == 404


# --- bills ----------------------------------------------------------------


async def test_foreign_bill_is_404_like_nonexistent(client, auth, A, B):
    foreign = await client.get(f"/v1/customers/{A.ref}/bills/{B.bills[0]}", headers=auth)
    missing = await client.get(f"/v1/customers/{A.ref}/bills/bill_does_not_exist", headers=auth)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert foreign.content == missing.content  # byte-identical
    assert B.bills[0] not in foreign.text


async def test_list_bills_filter_order_limit(client, auth, A):
    base = f"/v1/customers/{A.ref}/bills"
    default = (await client.get(base, headers=auth)).json()
    assert len(default) == min(12, len(A.bills))
    months = [b["reference_month"] for b in default]
    assert months == sorted(months, reverse=True)
    assert [b["bill_id"] for b in default] == A.bills[: len(default)]

    overdue = (await client.get(base, headers=auth, params={"status": "overdue"})).json()
    expected = [b for b in A.data["bills"] if b["status"] == "overdue"]
    assert [b["bill_id"] for b in overdue] == [b["bill_id"] for b in expected]
    assert len(overdue) >= 1 and all(b["status"] == "overdue" for b in overdue)

    opened = (await client.get(base, headers=auth, params={"status": "open"})).json()
    assert all(b["status"] == "open" for b in opened) and len(opened) >= 1

    limited = (await client.get(base, headers=auth, params={"limit": 2})).json()
    assert [b["bill_id"] for b in limited] == A.bills[:2]

    assert (await client.get(base, headers=auth, params={"limit": 13})).status_code == 422
    assert (await client.get(base, headers=auth, params={"limit": 0})).status_code == 422
    assert (await client.get(base, headers=auth, params={"status": "bogus"})).status_code == 422


async def test_list_bills_empty_result_for_all_paid_customer(client, auth, B):
    for status in ("open", "overdue"):
        r = await client.get(f"/v1/customers/{B.ref}/bills", headers=auth, params={"status": status})
        assert r.status_code == 200 and r.json() == []
    paid = (await client.get(f"/v1/customers/{B.ref}/bills", headers=auth, params={"status": "paid"})).json()
    assert len(paid) == min(12, len(B.bills))


async def test_bill_summary_shape_and_money_as_string(client, auth, A):
    first = A.data["bills"][0]
    body = (await client.get(f"/v1/customers/{A.ref}/bills", headers=auth)).json()[0]
    assert set(body) == {
        "bill_id", "installation_id", "reference_month", "due_date", "amount_brl", "status",
        "consumption_kwh", "tariff_flag",
    }
    assert body["bill_id"] == first["bill_id"]
    assert isinstance(body["amount_brl"], str) and body["amount_brl"] == first["amount_brl"]
    assert body["due_date"] == first["due_date"]
    assert body["consumption_kwh"] == first["consumption_kwh"]
    assert body["tariff_flag"] == first["tariff_flag"]


async def test_bill_detail_has_line_items_summing_to_amount(client, auth, A):
    first = A.data["bills"][0]
    r = await client.get(f"/v1/customers/{A.ref}/bills/{first['bill_id']}", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["issue_date"] == first["issue_date"]
    assert body["line_items"] == first["line_items"]
    assert sum(Decimal(li["amount_brl"]) for li in body["line_items"]) == Decimal(body["amount_brl"])


# --- seed + logging -------------------------------------------------------


async def test_seed_is_idempotent(migrated_db, client, auth, A):
    from mock_billing.seed import seed_from_file

    await seed_from_file(migrated_db.billing_database_url, migrated_db.personas_path)
    body = (await client.get(f"/v1/customers/{A.ref}/bills/{A.bills[0]}", headers=auth)).json()
    assert body["line_items"] == A.data["bills"][0]["line_items"]
    assert len((await client.get(f"/v1/customers/{A.ref}/bills", headers=auth)).json()) == min(12, len(A.bills))


async def test_correlation_id_is_logged_without_payloads(client, auth, A, caplog):
    caplog.set_level(logging.INFO, logger="mock_billing")
    await client.post(
        "/v1/identity/verify",
        headers={**auth, "X-Correlation-ID": "corr-123"},
        json=_verify_body(A.cpf, A.birth, A.last4),
    )
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "corr-123" in text
    assert A.cpf not in text and A.birth not in text and A.last4 not in text
