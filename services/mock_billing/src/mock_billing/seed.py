"""Idempotent seed of the synthetic personas (upsert by customer_ref)."""

import asyncio
import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from .db import make_engine
from .models import bill_line_items, bills, customers, installations


def _upsert(table, rows: list[dict], key: str):
    stmt = insert(table).values(rows)
    updates = {c.name: stmt.excluded[c.name] for c in table.columns if c.name != key}
    return stmt.on_conflict_do_update(index_elements=[key], set_=updates)


async def seed(engine: AsyncEngine, personas: list[dict]) -> None:
    async with engine.begin() as conn:
        for p in personas:
            address = p["address"]
            await conn.execute(
                _upsert(
                    customers,
                    [
                        {
                            "customer_ref": p["customer_ref"],
                            "first_name": p["first_name"],
                            "full_name": p["full_name"],
                            "cpf": p["cpf"],
                            "account_number": p["account_number"],
                            "birth_date": date.fromisoformat(p["birth_date"]),
                            "phone": p["phone"],
                            "street": address["street"],
                            "neighborhood": address["neighborhood"],
                            "city": address["city"],
                            "state": address["state"],
                            "tariff_class": p["tariff_class"],
                            "account_status": p["account_status"],
                        }
                    ],
                    "customer_ref",
                )
            )
            await conn.execute(
                _upsert(
                    installations,
                    [
                        {
                            "installation_id": i["installation_id"],
                            "customer_ref": p["customer_ref"],
                            "label": i["label"],
                        }
                        for i in p["installations"]
                    ],
                    "installation_id",
                )
            )
            bill_ids = [b["bill_id"] for b in p["bills"]]
            await conn.execute(
                _upsert(
                    bills,
                    [
                        {
                            "bill_id": b["bill_id"],
                            "customer_ref": p["customer_ref"],
                            "installation_id": b["installation_id"],
                            "reference_month": b["reference_month"],
                            "issue_date": date.fromisoformat(b["issue_date"]),
                            "due_date": date.fromisoformat(b["due_date"]),
                            "amount_brl": Decimal(b["amount_brl"]),
                            "status": b["status"],
                            "consumption_kwh": b["consumption_kwh"],
                            "tariff_flag": b["tariff_flag"],
                        }
                        for b in p["bills"]
                    ],
                    "bill_id",
                )
            )
            await conn.execute(
                bill_line_items.delete().where(bill_line_items.c.bill_id.in_(bill_ids))
            )
            await conn.execute(
                bill_line_items.insert(),
                [
                    {
                        "bill_id": b["bill_id"],
                        "position": pos,
                        "description": li["description"],
                        "amount_brl": Decimal(li["amount_brl"]),
                    }
                    for b in p["bills"]
                    for pos, li in enumerate(b["line_items"])
                ],
            )


async def seed_from_file(database_url: str, personas_path: Path) -> None:
    personas = json.loads(Path(personas_path).read_text(encoding="utf-8"))
    engine = make_engine(database_url)
    try:
        await seed(engine, personas)
    finally:
        await engine.dispose()


def main() -> None:
    url = os.environ["BILLING_DATABASE_URL"]
    path = Path(os.environ.get("PERSONAS_PATH", "data/synthetic/personas.json"))
    asyncio.run(seed_from_file(url, path))
    print(f"seeded personas from {path}")


if __name__ == "__main__":
    main()
