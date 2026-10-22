import logging
import time

ACCOUNT = "/v1/customers/{ref}/account"


async def test_faults_require_admin_key(client_enabled, settings):
    url = "/_dev/faults"
    assert (await client_enabled.put(url, json={"rules": []})).status_code == 401
    wrong = {"X-Fault-Admin-Key": settings.billing_api_key}
    assert (await client_enabled.put(url, json={"rules": []}, headers=wrong)).status_code == 401
    assert (await client_enabled.delete(url, headers=wrong)).status_code == 401
    ok = {"X-Fault-Admin-Key": settings.fault_admin_key}
    assert (await client_enabled.put(url, json={"rules": []}, headers=ok)).status_code == 204
    assert (await client_enabled.delete(url, headers=ok)).status_code == 204


async def test_faults_not_mounted_when_disabled(client_disabled, settings):
    r = await client_disabled.put(
        "/_dev/faults", json={"rules": []}, headers={"X-Fault-Admin-Key": settings.fault_admin_key}
    )
    assert r.status_code == 404


async def test_empty_admin_key_never_authenticates(migrated_db):
    import httpx
    from mock_billing.app import create_app

    cfg = migrated_db.model_copy(update={"fault_injection_enabled": True, "fault_admin_key": ""})
    app = create_app(cfg)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://b.test") as c:
        r = await c.put("/_dev/faults", json={"rules": []}, headers={"X-Fault-Admin-Key": ""})
        assert r.status_code == 401
    await app.state.engine.dispose()


async def test_rule_scoped_to_customer(client_enabled, admin, auth, A, E):
    put = lambda rules: client_enabled.put("/_dev/faults", json={"rules": rules}, headers=admin)
    get = lambda ref: client_enabled.get(ACCOUNT.format(ref=ref), headers=auth)

    assert (await put([{"route_name": "account", "customer_ref": E.ref, "effect": "http_500"}])).status_code == 204
    assert (await get(E.ref)).status_code == 500
    assert (await get(A.ref)).status_code == 200

    await put([{"route_name": "account", "customer_ref": E.ref, "effect": "unavailable"}])
    assert (await get(E.ref)).status_code == 503
    assert (await get(A.ref)).status_code == 200

    await put([{"route_name": "account", "customer_ref": E.ref, "effect": "delay_ms:200"}])
    started = time.monotonic()
    assert (await get(E.ref)).status_code == 200
    assert time.monotonic() - started >= 0.2

    await put([{"route_name": "account", "customer_ref": E.ref, "effect": "http_500"}])
    assert (await client_enabled.delete("/_dev/faults", headers=admin)).status_code == 204
    assert (await get(E.ref)).status_code == 200


async def test_rule_without_customer_matches_whole_route_only(client_enabled, admin, auth, A):
    rules = [{"route_name": "list_bills", "effect": "http_500"}]
    await client_enabled.put("/_dev/faults", json={"rules": rules}, headers=admin)
    assert (await client_enabled.get(f"/v1/customers/{A.ref}/bills", headers=auth)).status_code == 500
    assert (await client_enabled.get(ACCOUNT.format(ref=A.ref), headers=auth)).status_code == 200
    detail = f"/v1/customers/{A.ref}/bills/{A.bills[0]}"
    assert (await client_enabled.get(detail, headers=auth)).status_code == 200


async def test_bill_detail_and_verify_routes(client_enabled, admin, auth, A):
    rules = [
        {"route_name": "bill_detail", "effect": "unavailable"},
        {"route_name": "verify", "effect": "http_500"},
    ]
    await client_enabled.put("/_dev/faults", json={"rules": rules}, headers=admin)
    detail = f"/v1/customers/{A.ref}/bills/{A.bills[0]}"
    assert (await client_enabled.get(detail, headers=auth)).status_code == 503
    body = {"identifier": A.cpf, "birth_date": A.birth, "phone_last4": A.last4}
    assert (await client_enabled.post("/v1/identity/verify", json=body, headers=auth)).status_code == 500


async def test_verify_never_matches_customer_scoped_rule(client_enabled, admin, auth, A):
    # verify has no customer_ref in its path, so a customer-scoped rule cannot match it.
    rules = [{"route_name": "verify", "customer_ref": A.ref, "effect": "http_500"}]
    await client_enabled.put("/_dev/faults", json={"rules": rules}, headers=admin)
    body = {"identifier": A.cpf, "birth_date": A.birth, "phone_last4": A.last4}
    assert (await client_enabled.post("/v1/identity/verify", json=body, headers=auth)).status_code == 200


async def test_put_replaces_all_rules(client_enabled, admin, auth, A):
    url = "/_dev/faults"
    await client_enabled.put(url, json={"rules": [{"route_name": "account", "effect": "http_500"}]}, headers=admin)
    await client_enabled.put(url, json={"rules": [{"route_name": "list_bills", "effect": "http_500"}]}, headers=admin)
    assert (await client_enabled.get(ACCOUNT.format(ref=A.ref), headers=auth)).status_code == 200


async def test_timeout_effect_sleeps_30_seconds(app_enabled, client_enabled, admin, auth, A):
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    app_enabled.state.sleep = fake_sleep
    rules = [{"route_name": "account", "effect": "timeout"}]
    await client_enabled.put("/_dev/faults", json={"rules": rules}, headers=admin)
    assert (await client_enabled.get(ACCOUNT.format(ref=A.ref), headers=auth)).status_code == 200
    assert slept == [30]


async def test_fault_requires_billing_key_first(client_enabled, admin, A):
    rules = [{"route_name": "account", "effect": "http_500"}]
    await client_enabled.put("/_dev/faults", json={"rules": rules}, headers=admin)
    assert (await client_enabled.get(ACCOUNT.format(ref=A.ref))).status_code == 401


async def test_invalid_rules_rejected(client_enabled, admin):
    bad = [
        {"route_name": "nope", "effect": "http_500"},
        {"route_name": "account", "effect": "explode"},
        {"route_name": "account", "effect": "delay_ms:-5"},
        {"route_name": "account", "effect": "delay_ms:abc"},
        {"route_name": "account", "effect": "delay_ms:"},
        {"route_name": "account", "effect": "delay_ms:600000"},
    ]
    for rule in bad:
        r = await client_enabled.put("/_dev/faults", json={"rules": [rule]}, headers=admin)
        assert r.status_code == 422, rule


async def test_fault_responses_are_logged(client_enabled, admin, auth, A, caplog):
    rules = [{"route_name": "account", "effect": "http_500"}]
    await client_enabled.put("/_dev/faults", json={"rules": rules}, headers=admin)
    with caplog.at_level(logging.INFO, logger="mock_billing"):
        await client_enabled.get(ACCOUNT.format(ref=A.ref), headers=auth)
    lines = [r.getMessage() for r in caplog.records if "request handled" in r.getMessage()]
    assert any("status=500" in line and "/v1/customers/{ref}/account" in line for line in lines)
    assert not any(A.ref in line for line in lines)
