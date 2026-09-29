"""Calendar helpers — Gregorian storage, Jalali (Shamsi) presentation."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import jdatetime

from app.core.config import settings

PERSIAN_MONTHS = (
    "فروردین",
    "اردیبهشت",
    "خرداد",
    "تیر",
    "مرداد",
    "شهریور",
    "مهر",
    "آبان",
    "آذر",
    "دی",
    "بهمن",
    "اسفند",
)
PERSIAN_WEEKDAYS = ("شنبه", "یک‌شنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنج‌شنبه", "جمعه")

#: Single source of truth for digit conversion lives in :mod:`app.core.money`.
from app.core.money import fa_digits as _fa  # noqa: E402  (import after constants on purpose)


def tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.timezone)
    except Exception:  # pragma: no cover - bad tz name
        return ZoneInfo("Asia/Tehran")


def now_utc() -> datetime:
    """Timezone-aware UTC now.  All DB timestamps are stored in UTC."""
    return datetime.now(UTC)


def now_local() -> datetime:
    return now_utc().astimezone(tz())


def to_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz())


def to_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def jalali_date(dt: datetime | date | None) -> str:
    """``۱۴۰۳/۰۷/۰۸``"""
    if dt is None:
        return "—"
    if isinstance(dt, datetime):
        dt = to_local(dt) or dt
        d = dt.date()
    else:
        d = dt
    j = jdatetime.date.fromgregorian(date=d)
    return _fa(f"{j.year:04d}/{j.month:02d}/{j.day:02d}")


def jalali_datetime(dt: datetime | None) -> str:
    """``۱۴۰۳/۰۷/۰۸ - ۱۴:۳۰``"""
    if dt is None:
        return "—"
    local = to_local(dt) or dt
    j = jdatetime.datetime.fromgregorian(datetime=local)
    return _fa(f"{j.year:04d}/{j.month:02d}/{j.day:02d} - {j.hour:02d}:{j.minute:02d}")


def jalali_long(dt: datetime | None) -> str:
    """``۸ مهر ۱۴۰۳ ساعت ۱۴:۳۰``"""
    if dt is None:
        return "—"
    local = to_local(dt) or dt
    j = jdatetime.datetime.fromgregorian(datetime=local)
    return _fa(f"{j.day} {PERSIAN_MONTHS[j.month - 1]} {j.year} ساعت {j.hour:02d}:{j.minute:02d}")


def jalali_short_day(dt: datetime | date | None) -> str:
    """``۸ مهر``"""
    if dt is None:
        return "—"
    if isinstance(dt, datetime):
        dt = to_local(dt) or dt
        d = dt.date()
    else:
        d = dt
    j = jdatetime.date.fromgregorian(date=d)
    return _fa(f"{j.day} {PERSIAN_MONTHS[j.month - 1]}")


def humanize_delta(target: datetime | None, *, from_dt: datetime | None = None) -> str:
    """``۳ روز و ۴ ساعت`` — positive when ``target`` is in the future."""
    if target is None:
        return "نامحدود"
    base = from_dt or now_utc()
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    delta = target - base
    total = int(delta.total_seconds())
    past = total < 0
    total = abs(total)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60

    if days:
        text = f"{days} روز" + (f" و {hours} ساعت" if hours else "")
    elif hours:
        text = f"{hours} ساعت" + (f" و {minutes} دقیقه" if minutes else "")
    elif minutes:
        text = f"{minutes} دقیقه"
    else:
        text = "چند لحظه"
    text = _fa(text)
    return f"{text} پیش" if past else text


def humanize_bytes_rate(num_bytes: float) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def period_bounds(days: int) -> tuple[datetime, datetime]:
    """Return ``(start, end)`` UTC bounds covering the last ``days`` days."""
    end = now_utc()
    start = end - timedelta(days=days)
    return start, end
