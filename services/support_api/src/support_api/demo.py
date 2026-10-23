"""GET /api/demo/customers: labeled synthetic demo data, read from the persona file (never `billing`)."""

import json
from pathlib import Path

from fastapi import APIRouter, Request

router = APIRouter()


def load_demo_customers(path: Path) -> list[dict]:
    personas = json.loads(path.read_text(encoding="utf-8"))
    return [
        {
            "persona": p["persona"],
            "name": p["full_name"],
            "cpf": p["cpf"],
            "account_number": p["account_number"],
            "birth_date": p["birth_date"],
            "phone_last4": p["phone"][-4:],
            "synthetic": True,
        }
        for p in personas
    ]


@router.get("/api/demo/customers")
async def demo_customers(request: Request) -> list[dict]:
    return request.app.state.demo_customers
