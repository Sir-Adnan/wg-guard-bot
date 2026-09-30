"""Money and traffic helpers.

Project rule (non-negotiable): **the stored unit is always Rial**.
Every ``*_rial`` column, every API payload and every internal calculation uses
Rial as an integer.  Toman is a *display* concern only, and ``CURRENCY_DISPLAY``
decides which unit the customer sees.

1 Toman == 10 Rial.

Traffic is the other unit that has two spellings.  A plan says "50 GB"; the node
stores bytes.  Whether that is 50 × 1024³ or 50 × 1000³ is a product decision —
WG-Guard's own panel prints decimal gigabytes, so the operator who types 50 in
the bot and then opens the node sees 53.7 GB under the binary basis.  The basis
lives here, defaults to the binary one this project has always used, and is moved
by :func:`set_gb_basis` from the stored setting (see ``shop.traffic_unit``).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.core.config import settings

RIAL_PER_TOMAN = 10

#: Bytes in one "GB".  ``1024**3`` is what a Linux tool calls a gigabyte; the
#: vendor panel prints decimal, hence the alternative.
GB_BINARY = 1024**3
GB_DECIMAL = 1000**3

_gb_bytes: int = GB_BINARY

#: Persian (Extended Arabic-Indic) digits, written with explicit escapes so no
#: editor or copy/paste can silently swap them for Arabic-Indic ones.
PERSIAN_DIGITS = "\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9"
#: Arabic-Indic digits (used by some Arabic keyboards/imports).
ARABIC_INDIC_DIGITS = "\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669"

_ASCII = "0123456789"
_TO_PERSIAN = str.maketrans(_ASCII, PERSIAN_DIGITS)
#: Both digit families -> ASCII, in one pass (chained translates would not work).
_TO_ASCII = str.maketrans(
    {
        **dict(zip(PERSIAN_DIGITS, _ASCII, strict=True)),
        **dict(zip(ARABIC_INDIC_DIGITS, _ASCII, strict=True)),
    }
)


def to_toman(rial: int) -> int:
    """Convert Rial to whole Toman (rounded half-up)."""
    return int((Decimal(int(rial)) / RIAL_PER_TOMAN).quantize(Decimal(1), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# Traffic unit basis
# ---------------------------------------------------------------------------
def _gb_basis() -> int:
    return _gb_bytes


def _mb_per_gb() -> int:
    """How many "MB" fit in one of this shop's GB (1024 or 1000)."""
    return 1024 if _gb_bytes == GB_BINARY else 1000


def set_gb_basis(*, decimal: bool) -> None:
    """Choose what an operator's "GB" means, in bytes.

    Called once per process from the stored setting, and again whenever the
    operator changes it — the value is read by the provisioning path, so a
    process that never calls this keeps the historical binary basis.
    """
    global _gb_bytes
    _gb_bytes = GB_DECIMAL if decimal else GB_BINARY


def gb_basis_is_decimal() -> bool:
    return _gb_bytes == GB_DECIMAL


def to_rial(toman: int | float | Decimal) -> int:
    """Convert Toman to Rial."""
    return int((Decimal(str(toman)) * RIAL_PER_TOMAN).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def user_unit_amount(rial: int) -> int:
    """Amount expressed in the unit the customer is configured to see."""
    return to_toman(rial) if settings.currency_display == "toman" else int(rial)


def unit_label() -> str:
    return "تومان" if settings.currency_display == "toman" else "ریال"


def parse_user_amount(amount: int | str, *, unit: str | None = None) -> int:
    """Parse a number typed by an admin/customer into Rial.

    Admins type prices in the display unit (Toman by default).  A trailing
    ``ریال``/``تومان`` word overrides the default unit so both are accepted.
    """
    if isinstance(amount, str):
        text = amount.strip()
        text = text.translate(_TO_ASCII)
        text = text.replace(",", "").replace("\u066c", "").replace("\u060c", "").replace(" ", "")
        forced: str | None = None
        for word, name in (("ریال", "rial"), ("تومان", "toman"), ("ت", "toman"), ("ر", "rial")):
            if text.endswith(word):
                forced = name
                text = text[: -len(word)]
                break
        unit = forced or unit or settings.currency_display
        value = Decimal(text or "0")
    else:
        value = Decimal(str(amount))
        unit = unit or settings.currency_display

    return to_rial(value) if unit == "toman" else int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def format_amount(rial: int, *, with_unit: bool = True, persian_digits: bool = True) -> str:
    """Format a Rial amount for display: ``۲۵۰,۰۰۰ تومان``."""
    amount = user_unit_amount(rial)
    text = f"{amount:,}"
    if persian_digits:
        text = text.translate(_TO_PERSIAN)
    if with_unit:
        return f"{text} {unit_label()}"
    return text


def format_rial(rial: int) -> str:
    """Always-Rial formatting, for invoices/admin exports."""
    text = f"{int(rial):,}".translate(_TO_PERSIAN)
    return f"{text} ریال"


def format_gb(gb: float | int | None) -> str:
    """Human readable traffic volume from a GB value (``None``/0 = unlimited)."""
    if gb is None:
        return "نامحدود"
    value = float(gb)
    if value <= 0:
        return "نامحدود"
    if value < 1:
        return f"{_num(value * _mb_per_gb(), 0)} مگابایت".translate(_TO_PERSIAN)
    if value == int(value):
        return f"{int(value)} گیگابایت".translate(_TO_PERSIAN)
    return f"{value:g} گیگابایت".translate(_TO_PERSIAN)


def format_bytes(num_bytes: int | None) -> str:
    """Format a byte count using Persian units (بایت/کیلوبایت/...)."""
    if num_bytes is None:
        return "نامحدود"
    # The ladder steps by KB/MB/GB — 1024 for a binary basis, 1000 for decimal.
    step = float(_mb_per_gb())
    size = float(num_bytes)
    for unit in ("بایت", "کیلوبایت", "مگابایت", "گیگابایت", "ترابایت"):
        if size < step or unit == "ترابایت":
            return f"{_num(size, 0 if unit == 'بایت' else 2)} {unit}".translate(_TO_PERSIAN)
        size /= step
    return f"{size:.2f} ترابایت".translate(_TO_PERSIAN)


def gb_to_bytes(gb: float | int | None) -> int | None:
    """Convert a GB figure to bytes (``None``/0 stays unlimited)."""
    if gb is None:
        return None
    value = float(gb)
    if value <= 0:
        return None
    return int(Decimal(str(value)) * _gb_basis())


def bytes_to_gb(num_bytes: int | None) -> float | None:
    if num_bytes is None:
        return None
    return round(num_bytes / _gb_basis(), 3)


def days_to_seconds(days: int | None) -> int | None:
    if not days or days <= 0:
        return None
    return int(days) * 86400


def _num(value: float, digits: int) -> str:
    if digits <= 0:
        return f"{value:.0f}"
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def fa_digits(text: str | int) -> str:
    """Translate ASCII digits in any string to Persian digits."""
    return str(text).translate(_TO_PERSIAN)


def en_digits(text: str) -> str:
    """Translate Persian/Arabic-Indic digits back to ASCII (user input parsing)."""
    return str(text).translate(_TO_ASCII)
