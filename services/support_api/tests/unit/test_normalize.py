import hashlib
import hmac

import pytest
from support_api.identity.normalize import NormalizedIdentifier, lockout_key, normalize

KEY = b"test-hash-key"


@pytest.mark.parametrize(
    "raw", ["123.456.789-09", "12345678909", " 123 456 789 09 ", "123-456.789/09"]
)
def test_cpf_formats_share_key(raw):
    assert lockout_key(normalize(raw), KEY) == lockout_key(
        normalize("12345678909"), KEY
    )


def test_cpf_is_classified_by_eleven_digits():
    assert normalize("123.456.789-09") == NormalizedIdentifier("cpf", "12345678909")


def test_account_number_is_ten_digits():
    assert normalize("00 1234-5678") == NormalizedIdentifier("account", "0012345678")


@pytest.mark.parametrize("raw", ["123", "1" * 12, "", "abc", "1" * 9])
def test_invalid_lengths_return_none(raw):
    assert normalize(raw) is None


def test_lockout_key_is_hex_hmac_sha256_of_kind_and_digits():
    n = NormalizedIdentifier("cpf", "12345678909")
    expected = hmac.new(KEY, b"cpf:12345678909", hashlib.sha256).hexdigest()
    assert lockout_key(n, KEY) == expected


def test_kind_is_part_of_the_key():
    # same digits can never collide across kinds, and a different key gives a different hash
    a = lockout_key(NormalizedIdentifier("cpf", "1234567890"), KEY)
    b = lockout_key(NormalizedIdentifier("account", "1234567890"), KEY)
    assert a != b
    assert lockout_key(NormalizedIdentifier("cpf", "12345678909"), b"other") != (
        lockout_key(NormalizedIdentifier("cpf", "12345678909"), KEY)
    )


def test_only_ascii_digits_count():
    assert normalize("١٢٣٤٥٦٧٨٩٠١") is None
