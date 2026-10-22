#!/usr/bin/env python3
"""Generate the seeded synthetic personas fixture (data/synthetic/personas.json).

Usage: gen_personas.py --seed 20261022 --out data/synthetic/personas.json

Everything is synthetic. Each CPF is a generated CPF (valid check digits) used
solely as a synthetic fixture. Output is byte-stable for a given seed: all dates
derive from fixed constants (never the wall clock) and key order is fixed.
"""

import argparse
import json
import random
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from mock_billing import cpf

DEFAULT_SEED = 20261022
CENT = Decimal("0.01")

# Fictional "today" for the fixture: newest reference month is 2026-09,
# issued 2026-10-05, due 2026-10-28 (still open on 2026-10-22).
NEWEST_MONTH = (2026, 9)

KWH_RATE = Decimal("0.89")
FLAG_SURCHARGE_PER_KWH = {
    "verde": Decimal(0),
    "amarela": Decimal("0.01885"),
    "vermelha": Decimal("0.04463"),
}
FLAG_LABEL = {
    "verde": "Bandeira verde (sem acréscimo)",
    "amarela": "Bandeira amarela",
    "vermelha": "Bandeira vermelha",
}
STREET_LIGHTING = Decimal("12.40")

PERSONA_SPECS = [
    {
        "persona": "A",
        "first_name": "Ana",
        "full_name": "Ana Exemplo da Silva",
        "situation": "One open bill and one overdue bill",
        "address": (
            "Rua das Acácias Fictícias, 100",
            "Jardim Exemplo",
            "Campinas",
            "SP",
        ),
        "tariff_class": "residencial",
        "installations": ["Casa"],
        "months": 8,
        "base_kwh": (170, 230),
    },
    {
        "persona": "B",
        "first_name": "Bruno",
        "full_name": "Bruno Teste Oliveira",
        "situation": "All bills paid",
        "address": ("Avenida Modelo, 2050", "Centro Simulado", "Belo Horizonte", "MG"),
        "tariff_class": "residencial",
        "installations": ["Casa"],
        "months": 10,
        "base_kwh": (140, 200),
    },
    {
        "persona": "C",
        "first_name": "Carla",
        "full_name": "Carla Fictícia Souza",
        "situation": "Two installations (home and small shop)",
        "address": ("Travessa do Exemplo, 33", "Vila Sintética", "Curitiba", "PR"),
        "tariff_class": "residencial",
        "installations": ["Casa", "Loja pequena"],
        "months": 6,
        "base_kwh": (150, 260),
    },
    {
        "persona": "D",
        "first_name": "Diego",
        "full_name": "Diego Amostra Pereira",
        "situation": "Recent consumption spike (seeds Milestone 2 bill-dispute scenario)",
        "address": ("Rua Hipotética, 721", "Bairro Fixture", "Recife", "PE"),
        "tariff_class": "residencial",
        "installations": ["Casa"],
        "months": 9,
        "base_kwh": (180, 220),
    },
    {
        "persona": "E",
        "first_name": "Elisa",
        "full_name": "Elisa Demonstração Lima",
        "situation": "Used for outage demos via fault rules",
        "address": ("Alameda dos Testes, 12", "Parque Mock", "Porto Alegre", "RS"),
        "tariff_class": "residencial",
        "installations": ["Casa"],
        "months": 7,
        "base_kwh": (160, 240),
    },
]


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def hex_id(rng: random.Random, prefix: str, nbytes: int = 6) -> str:
    return f"{prefix}_{rng.getrandbits(nbytes * 8):0{nbytes * 2}x}"


def month_back(n: int) -> tuple[int, int]:
    year, month = NEWEST_MONTH
    index = year * 12 + (month - 1) - n
    return index // 12, index % 12 + 1


def next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def make_cpf(rng: random.Random, used: set[str]) -> str:
    while True:
        base = "".join(str(rng.randrange(10)) for _ in range(9))
        candidate = base + cpf.check_digits(base)
        if len(set(candidate)) > 1 and candidate not in used:
            used.add(candidate)
            return candidate


def make_account_number(rng: random.Random, used: set[str]) -> str:
    while True:
        candidate = str(rng.randrange(1, 10)) + "".join(
            str(rng.randrange(10)) for _ in range(9)
        )
        if candidate not in used:
            used.add(candidate)
            return candidate


def make_phone(rng: random.Random, used_last4: set[str]) -> str:
    while True:
        ddd = rng.choice(["11", "19", "21", "31", "41", "51", "81"])
        subscriber = "9" + "".join(str(rng.randrange(10)) for _ in range(8))
        if subscriber[-4:] not in used_last4:
            used_last4.add(subscriber[-4:])
            return f"+55{ddd}{subscriber}"


def make_bill(
    rng: random.Random,
    installation_id: str,
    offset: int,
    kwh: int,
    status: str,
) -> dict:
    year, month = month_back(offset)
    issue_year, issue_month = next_month(year, month)
    flag = rng.choices(["verde", "amarela", "vermelha"], weights=[6, 3, 1])[0]
    line_items = [
        {
            "description": "Consumo de energia elétrica",
            "amount_brl": money(KWH_RATE * kwh),
        },
        {
            "description": FLAG_LABEL[flag],
            "amount_brl": money(FLAG_SURCHARGE_PER_KWH[flag] * kwh),
        },
        {
            "description": "Contribuição de iluminação pública",
            "amount_brl": STREET_LIGHTING,
        },
    ]
    total = sum((li["amount_brl"] for li in line_items), Decimal(0))
    return {
        "bill_id": hex_id(rng, "bill"),
        "installation_id": installation_id,
        "reference_month": f"{year:04d}-{month:02d}",
        "issue_date": date(issue_year, issue_month, 5).isoformat(),
        "due_date": date(issue_year, issue_month, 28).isoformat(),
        "amount_brl": str(money(total)),
        "status": status,
        "consumption_kwh": kwh,
        "tariff_flag": flag,
        "line_items": [
            {"description": li["description"], "amount_brl": str(li["amount_brl"])}
            for li in line_items
        ],
    }


def bill_status(persona: str, offset: int) -> str:
    """offset 0 is the newest reference month."""
    if persona == "A":
        return {0: "open", 1: "overdue"}.get(offset, "paid")
    if persona == "B":
        return "paid"
    return "open" if offset == 0 else "paid"


def make_persona(rng: random.Random, spec: dict, used: dict) -> dict:
    persona = spec["persona"]
    installations = [
        {"installation_id": hex_id(rng, "inst"), "label": label}
        for label in spec["installations"]
    ]
    low, high = spec["base_kwh"]
    bills = []
    for offset in range(spec["months"]):
        for index, inst in enumerate(installations):
            scale = 1 if index == 0 else Decimal("0.55")
            kwh = int(rng.randint(low, high) * scale)
            if persona == "D" and offset == 0:
                kwh = int(kwh * Decimal("2.6"))
            bills.append(
                make_bill(
                    rng,
                    inst["installation_id"],
                    offset,
                    kwh,
                    bill_status(persona, offset),
                )
            )
    street, neighborhood, city, state = spec["address"]
    return {
        "persona": persona,
        "customer_ref": hex_id(rng, "cus"),
        "first_name": spec["first_name"],
        "full_name": spec["full_name"],
        "cpf": make_cpf(rng, used["cpf"]),
        "account_number": make_account_number(rng, used["account"]),
        "birth_date": date(
            rng.randint(1965, 1998), rng.randint(1, 12), rng.randint(1, 28)
        ).isoformat(),
        "phone": make_phone(rng, used["phone4"]),
        "situation": spec["situation"],
        "address": {
            "street": street,
            "neighborhood": neighborhood,
            "city": city,
            "state": state,
        },
        "tariff_class": spec["tariff_class"],
        "account_status": "active",
        "installations": installations,
        "bills": bills,
    }


def generate(seed: int) -> list[dict]:
    rng = random.Random(seed)
    used = {"cpf": set(), "account": set(), "phone4": set()}
    return [make_persona(rng, spec, used) for spec in PERSONA_SPECS]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--out", type=Path, default=Path("data/synthetic/personas.json")
    )
    args = parser.parse_args()
    personas = generate(args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(personas, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    args.out.write_bytes(text.encode("utf-8"))


if __name__ == "__main__":
    main()
