"""CPF check-digit utilities.

Every CPF in this project is a generated CPF (valid check digits) used solely
as a synthetic fixture. None identifies a real person.
"""

CPF_LENGTH = 11


def _digit(digits: list[int]) -> int:
    weight = len(digits) + 1
    total = sum(d * (weight - i) for i, d in enumerate(digits))
    remainder = (total * 10) % 11
    return 0 if remainder == 10 else remainder


def check_digits(base9: str) -> str:
    """Return the two check digits for a 9-digit base."""
    if len(base9) != 9 or not base9.isascii() or not base9.isdigit():
        raise ValueError("base must be exactly 9 ASCII digits")
    digits = [int(c) for c in base9]
    first = _digit(digits)
    second = _digit([*digits, first])
    return f"{first}{second}"


def is_valid(digits11: str) -> bool:
    """True for 11 ASCII digits, not all equal, with correct check digits."""
    if len(digits11) != CPF_LENGTH or not digits11.isascii() or not digits11.isdigit():
        return False
    if len(set(digits11)) == 1:
        return False
    return check_digits(digits11[:9]) == digits11[9:]
