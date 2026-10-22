"""PT-BR formatting for money and dates."""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

_CENT = Decimal("0.01")


def format_brl(amount: Decimal) -> str:
    """``Decimal("1234.5")`` -> ``"R$ 1.234,50"``; negative amounts -> ``"-R$ 1,00"``."""
    q = amount.quantize(_CENT, rounding=ROUND_HALF_UP)
    body = f"{abs(q):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"-R$ {body}" if q < 0 else f"R$ {body}"


def format_date(d: date) -> str:
    """``date(2026, 11, 5)`` -> ``"05/11/2026"``."""
    return d.strftime("%d/%m/%Y")
