"""English formatting for money (Brazilian reais) and dates."""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

_CENT = Decimal("0.01")
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def format_brl(amount: Decimal) -> str:
    """``Decimal("1234.5")`` -> ``"R$ 1,234.50"``; negative amounts -> ``"-R$ 1.00"``."""
    q = amount.quantize(_CENT, rounding=ROUND_HALF_UP)
    body = f"{abs(q):,.2f}"  # abs() also drops the sign of a rounded -0.00
    return f"-R$ {body}" if q < 0 else f"R$ {body}"


def format_date(d: date) -> str:
    """``date(2026, 11, 5)`` -> ``"Nov 5, 2026"``: the spelled month cannot be read day-first."""
    return f"{MONTHS[d.month - 1][:3]} {d.day}, {d.year}"


def format_month(year: int, month: int) -> str:
    """``(2026, 10)`` -> ``"October 2026"``."""
    return f"{MONTHS[month - 1]} {year}"
