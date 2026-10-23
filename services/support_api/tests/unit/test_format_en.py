from datetime import date
from decimal import Decimal

import pytest
from support_api.agent.format_en import format_brl, format_date, format_month


def test_format_brl():
    assert format_brl(Decimal("1234.5")) == "R$ 1,234.50"


@pytest.mark.parametrize(
    "amount,expected",
    [
        (Decimal(0), "R$ 0.00"),
        (Decimal("0.005"), "R$ 0.01"),  # ROUND_HALF_UP, not banker's rounding
        (Decimal("2.675"), "R$ 2.68"),
        (Decimal(999), "R$ 999.00"),
        (Decimal("1234567.891"), "R$ 1,234,567.89"),
        (Decimal(-1), "-R$ 1.00"),
        (Decimal("-1234.567"), "-R$ 1,234.57"),
        (Decimal("-0.001"), "R$ 0.00"),  # rounds to zero: no sign
    ],
)
def test_format_brl_cases(amount, expected):
    assert format_brl(amount) == expected


def test_format_date_spells_the_month_so_day_and_month_cannot_be_swapped():
    assert format_date(date(2026, 11, 5)) == "Nov 5, 2026"
    assert format_date(date(1980, 2, 1)) == "Feb 1, 1980"
    assert format_date(date(1994, 12, 25)) == "Dec 25, 1994"


def test_format_month():
    assert format_month(2026, 10) == "October 2026"
    assert format_month(2026, 1) == "January 2026"
