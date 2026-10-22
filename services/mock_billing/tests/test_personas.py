import statistics
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

from mock_billing import cpf

REPO_ROOT = Path(__file__).resolve().parents[3]
PERSONAS_PATH = REPO_ROOT / "data" / "synthetic" / "personas.json"
SEED = "20261022"


def test_five_personas_with_required_situations(personas):
    assert [p["persona"] for p in personas] == ["A", "B", "C", "D", "E"]
    a, b, c, d, _ = personas
    assert sorted(x["status"] for x in a["bills"][:2]) == ["open", "overdue"]
    assert all(x["status"] == "paid" for x in b["bills"])
    assert len(c["installations"]) == 2
    assert d["bills"][0]["consumption_kwh"] >= 1.8 * statistics.mean(
        x["consumption_kwh"] for x in d["bills"][1:]
    )


def test_cpfs_have_valid_check_digits_and_unique_identifiers(personas):
    assert all(cpf.is_valid(p["cpf"]) for p in personas)
    assert len({p["cpf"] for p in personas}) == 5 and all(
        len(p["account_number"]) == 10 for p in personas
    )


def test_bills_shape(personas):
    for p in personas:
        assert 6 <= len(p["bills"]) <= 12
        for bill in p["bills"]:
            assert Decimal(bill["amount_brl"]) == sum(
                Decimal(li["amount_brl"]) for li in bill["line_items"]
            )


def test_bills_are_newest_first(personas):
    for p in personas:
        months = [b["reference_month"] for b in p["bills"]]
        assert months == sorted(months, reverse=True)


def test_phone_last_four_digits_are_distinct(personas):
    assert all(p["phone"].startswith("+55") for p in personas)
    assert len({p["phone"][-4:] for p in personas}) == 5


def test_cpf_check_digits_known_values():
    assert cpf.check_digits("123456789") == "09"
    assert cpf.is_valid("12345678909")
    assert not cpf.is_valid("12345678900")
    assert not cpf.is_valid("11111111111")
    assert not cpf.is_valid("1234567890")
    assert not cpf.is_valid("1234567890a")


def test_generator_is_deterministic(tmp_path):
    out = tmp_path / "personas.json"
    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "gen_personas.py"),
            "--seed",
            SEED,
            "--out",
            str(out),
        ],
        check=True,
        cwd=tmp_path,
    )
    assert out.read_bytes() == PERSONAS_PATH.read_bytes()
