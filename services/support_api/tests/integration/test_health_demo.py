from sqlalchemy import text
from support_api.catalog import MESSAGES, UI_MESSAGES


async def test_health_reports_scripted_mode(client):
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok", "mode": "scripted", "live_enabled": False,
        "ui": {**UI_MESSAGES, "offline_badge": MESSAGES["offline_badge"]},
    }


async def test_demo_customers_reads_persona_file(client, personas):
    resp = await client.get("/api/demo/customers")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 5
    for entry, persona in zip(body, personas, strict=True):
        assert entry["synthetic"] is True
        assert entry["cpf"] == persona["cpf"]
        assert entry["account_number"] == persona["account_number"]
        assert entry["birth_date"] == persona["birth_date"]
        assert entry["phone_last4"] == persona["phone"][-4:]
        assert entry["persona"] == persona["persona"]
        assert "phone" not in entry and "bills" not in entry


async def test_demo_customers_disabled(client_demo_off):
    resp = await client_demo_off.get("/api/demo/customers")
    assert resp.status_code == 404


async def test_migration_seeds_project_budget(support_db):
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(support_db.support_database_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT cap_usd, spent_usd, reserved_usd, uncertain_usd "
                        "FROM support.budget_accounts WHERE id = 'project'"
                    )
                )
            ).one()
    finally:
        await engine.dispose()
    assert (row.cap_usd, row.spent_usd, row.reserved_usd, row.uncertain_usd) == (
        20,
        0,
        0,
        0,
    )


async def test_schema_tables_exist(support_db):
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(support_db.support_database_url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'support'"
                )
            )
            names = {r[0] for r in rows}
    finally:
        await engine.dispose()
    assert names >= {
        "browser_sessions", "conversations", "conversation_auth", "identifier_lockouts",
        "verification_attempts", "turns", "conversation_items", "tool_calls", "budget_accounts",
        "conversation_budgets", "llm_calls",
    }  # fmt: skip
