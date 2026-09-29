"""Pure unit tests: money, security, calendar.

These run anywhere — no database, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import InvalidOperation

import pytest

from app.core.money import (
    RIAL_PER_TOMAN,
    bytes_to_gb,
    en_digits,
    fa_digits,
    format_amount,
    format_bytes,
    format_gb,
    gb_to_bytes,
    parse_user_amount,
    to_rial,
    to_toman,
)

# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------


def test_toman_rial_round_trip() -> None:
    assert RIAL_PER_TOMAN == 10
    assert to_toman(250_000) == 25_000
    assert to_rial(25_000) == 250_000
    assert to_toman(to_rial(123_456)) == 123_456


def test_toman_rounds_half_up() -> None:
    assert to_toman(5) == 1  # 0.5 Toman rounds up
    assert to_toman(4) == 0
    assert to_toman(15) == 2


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("250000", 2_500_000),  # typed in Toman (the default display unit)
        ("250,000", 2_500_000),
        ("۲۵۰٬۰۰۰", 2_500_000),  # Persian digits + separator
        ("250000 تومان", 2_500_000),
        ("2500000 ریال", 2_500_000),  # explicit Rial wins over the default
        ("۲۵۰۰۰۰۰ ر", 2_500_000),
    ],
)
def test_parse_user_amount(raw: str, expected: int) -> None:
    assert parse_user_amount(raw) == expected


def test_parse_user_amount_rejects_garbage() -> None:
    with pytest.raises(InvalidOperation):
        parse_user_amount("abc")


def test_format_amount_uses_display_unit() -> None:
    text = format_amount(2_500_000)
    assert "تومان" in text
    assert "۲۵۰,۰۰۰" in text  # Persian digits + grouping


def test_digit_translation() -> None:
    assert fa_digits("1402") == "۱۴۰۲"
    assert en_digits("۱۴۰۲") == "1402"


@pytest.mark.parametrize(
    ("gb", "fragment"),
    [(1, "گیگابایت"), (30, "۳۰"), (None, "نامحدود"), (0, "نامحدود")],
)
def test_format_gb(gb, fragment: str) -> None:
    assert fragment in format_gb(gb)


def test_byte_helpers() -> None:
    assert gb_to_bytes(1) == 1024**3
    assert gb_to_bytes(0) is None
    assert gb_to_bytes(None) is None
    assert bytes_to_gb(1024**3) == 1.0
    assert "گیگابایت" in format_bytes(2 * 1024**3)


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------


def test_password_hash_and_verify() -> None:
    from app.core.security import hash_password, verify_password

    stored = hash_password("S3cret-pass")
    assert stored.startswith("scrypt$")
    assert verify_password("S3cret-pass", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("", stored)
    assert not verify_password("S3cret-pass", None)
    assert not verify_password("S3cret-pass", "garbage")


def test_password_hash_is_salted() -> None:
    from app.core.security import hash_password

    assert hash_password("same") != hash_password("same")


@pytest.mark.parametrize(
    ("password", "ok"),
    [("short", False), ("12345678", False), ("abcdefgh", False), ("abcd1234", True)],
)
def test_password_strength(password: str, ok: bool) -> None:
    from app.core.security import password_strength_error

    assert (password_strength_error(password) is None) is ok


def test_secret_encryption_round_trip() -> None:
    from app.core.security import decrypt_secret, encrypt_secret

    original = "wg_secret_token_1234567890"
    cipher = encrypt_secret(original, purpose="panel-token")
    assert cipher and cipher != original
    assert decrypt_secret(cipher, purpose="panel-token") == original
    # A different purpose must not decrypt it.
    assert decrypt_secret(cipher, purpose="config") is None
    assert decrypt_secret(None) is None
    assert decrypt_secret("not-a-token") is None


def test_session_signing_round_trip() -> None:
    from app.core.security import load_session, sign_session

    token = sign_session({"sid": "abc", "uid": 7, "role": "owner"})
    data = load_session(token, max_age=60)
    assert data == {"sid": "abc", "uid": 7, "role": "owner"}
    assert load_session(token + "tampered", max_age=60) is None


def test_csrf_token_is_session_bound() -> None:
    from app.core.security import csrf_token_for

    assert csrf_token_for("a") == csrf_token_for("a")
    assert csrf_token_for("a") != csrf_token_for("b")


def test_mask_secret() -> None:
    from app.core.security import mask_secret

    assert mask_secret("wg_abcdefghijklmnop").startswith("wg_a")
    assert "…" in mask_secret("wg_abcdefghijklmnop")


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


def test_jalali_conversion() -> None:
    from app.core.jalali import jalali_date

    # 2026-03-21 is Nowruz 1405.
    assert jalali_date(datetime(2026, 3, 21, tzinfo=UTC)).startswith("۱۴۰۵")


def test_jalali_none_is_dash() -> None:
    from app.core.jalali import jalali_date, jalali_datetime

    assert jalali_date(None) == "—"
    assert jalali_datetime(None) == "—"


def test_humanize_delta() -> None:
    from app.core.jalali import humanize_delta, now_utc

    assert humanize_delta(None) == "نامحدود"
    assert "روز" in humanize_delta(now_utc() + timedelta(days=3))
    assert "ساعت" in humanize_delta(now_utc() + timedelta(hours=5))
    assert "پیش" in humanize_delta(now_utc() - timedelta(hours=2))


def test_to_utc_and_local_are_inverse() -> None:
    from app.core.jalali import to_local, to_utc

    naive = datetime(2026, 9, 29, 12, 0, 0)
    aware = to_utc(naive)
    assert aware is not None and aware.tzinfo is not None
    back = to_local(aware)
    assert back is not None
    assert back.astimezone(UTC) == aware
