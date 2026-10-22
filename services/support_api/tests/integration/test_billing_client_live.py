from datetime import date

import httpx
import pytest
from support_api.deadline import Deadline
from support_api.integrations.billing.client import BillingClient, BillingError


async def test_live_round_trip_against_mock_billing(billing_server, personas):
    a = personas[0]
    client = BillingClient(billing_server.base_url, billing_server.api_key)
    try:
        v = await client.verify(
            a["cpf"],
            date.fromisoformat(a["birth_date"]),
            a["phone"][-4:],
            deadline=Deadline(45),
        )
        assert v.attempts == 1 and v.value.verified
        assert v.value.customer_ref == a["customer_ref"]
        assert v.value.first_name == a["first_name"]

        bad = await client.verify(
            a["cpf"], date.fromisoformat(a["birth_date"]), "0000", deadline=Deadline(45)
        )
        assert bad.value.verified is False and bad.value.customer_ref is None

        ref = v.value.customer_ref
        acct = await client.get_account(ref, deadline=Deadline(45))
        assert acct.value.customer_ref == ref
        assert acct.value.masked_cpf.endswith(a["cpf"][-2:])
        assert [i.installation_id for i in acct.value.installations] == sorted(
            i["installation_id"] for i in a["installations"]
        )

        bills = await client.list_bills(
            ref, status=None, limit=12, deadline=Deadline(45)
        )
        assert len(bills.value) == min(12, len(a["bills"]))
        open_bills = await client.list_bills(
            ref, status="open", limit=12, deadline=Deadline(45)
        )
        assert open_bills.value and all(b.status == "open" for b in open_bills.value)

        detail = await client.get_bill(
            ref, bills.value[0].bill_id, deadline=Deadline(45)
        )
        assert (
            detail.value.bill_id == bills.value[0].bill_id and detail.value.line_items
        )

        with pytest.raises(BillingError) as e:
            await client.get_bill(ref, "bill_does_not_exist", deadline=Deadline(45))
        assert e.value.code == "not_found" and e.value.attempts == 1
    finally:
        await client.aclose()


async def test_live_wrong_api_key_is_unavailable_without_retry(
    billing_server, personas
):
    client = BillingClient(billing_server.base_url, "wrong-key")
    try:
        with pytest.raises(BillingError) as e:
            await client.get_account(personas[0]["customer_ref"], deadline=Deadline(45))
        assert e.value.code == "upstream_unavailable" and e.value.attempts == 1
    finally:
        await client.aclose()


async def test_live_injected_500_is_retried_then_unavailable(billing_server, personas):
    e_ref = personas[4]["customer_ref"]
    admin = {"X-Fault-Admin-Key": billing_server.fault_admin_key}
    rules = [{"route_name": "account", "customer_ref": e_ref, "effect": "http_500"}]
    async with httpx.AsyncClient(base_url=billing_server.base_url) as admin_http:
        assert (
            await admin_http.put("/_dev/faults", json={"rules": rules}, headers=admin)
        ).status_code == 204
        client = BillingClient(billing_server.base_url, billing_server.api_key)
        try:
            with pytest.raises(BillingError) as e:
                await client.get_account(e_ref, deadline=Deadline(45))
            assert e.value.code == "upstream_unavailable" and e.value.attempts == 3
        finally:
            await client.aclose()
            await admin_http.delete("/_dev/faults", headers=admin)
