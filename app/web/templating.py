"""Jinja2 environment with project helpers, plus the flash-message helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app import __version__
from app.core.config import settings
from app.core.jalali import humanize_delta, jalali_date, jalali_datetime, jalali_long, jalali_short_day
from app.core.locales import default_text
from app.core.money import (
    fa_digits,
    format_amount,
    format_bytes,
    format_gb,
    format_rial,
    to_toman,
    unit_label,
)
from app.web.security import FLASH_COOKIE, read_session

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


class LayoutCounters:
    """Sidebar badge counts (imported lazily to avoid a circular import)."""

    __slots__ = ("orders", "receipts", "tickets")

    def __init__(self, receipts: int = 0, tickets: int = 0, orders: int = 0) -> None:
        self.receipts = receipts
        self.tickets = tickets
        self.orders = orders


# ---------------------------------------------------------------------------
# Filters / globals
# ---------------------------------------------------------------------------
def _money(rial: int | None) -> str:
    return format_amount(int(rial or 0))


def _rial(rial: int | None) -> str:
    return format_rial(int(rial or 0))


def _toman(rial: int | None) -> int:
    return to_toman(int(rial or 0))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _pct(value: float | None, digits: int = 0) -> str:
    if value is None:
        return "—"
    return fa_digits(f"{value:.{digits}f}") + "٪"


def _relative(dt) -> str:
    return humanize_delta(dt)


templates.env.filters.update(
    {
        "money": _money,
        "rial": _rial,
        "toman": _toman,
        "jdate": jalali_date,
        "jdatetime": jalali_datetime,
        "jlong": jalali_long,
        "jshort": jalali_short_day,
        "bytes": format_bytes,
        "gb": format_gb,
        "fa": fa_digits,
        "json": _json,
        "pct": _pct,
        "relative": _relative,
        "none_dash": lambda v: v if v not in (None, "") else "—",
    }
)

templates.env.globals.update(
    {
        "app_name": settings.app_name,
        "panel_prefix": settings.panel_prefix,
        "currency_label": unit_label(),
        "ui_text": default_text,
        "currency_display": settings.currency_display,
        "environment": settings.env,
    }
)


# ---------------------------------------------------------------------------
# Flash messages (cookie-based so they survive a redirect)
# ---------------------------------------------------------------------------
def flash(response: RedirectResponse, message: str, level: str = "success") -> RedirectResponse:
    """Attach a one-shot flash message to a redirect.

    The payload is JSON-encoded with ``ensure_ascii=True`` on purpose: cookie
    values travel as latin-1, so raw Persian characters would raise
    ``UnicodeEncodeError`` inside Starlette.  ``\\uXXXX`` escapes survive the
    round trip untouched (:func:`pop_flash` uses ``json.loads``).
    """
    payload = json.dumps({"message": message, "level": level})
    response.set_cookie(
        FLASH_COOKIE,
        payload,
        max_age=30,
        httponly=True,
        samesite="lax",
        secure=settings.panel_behind_proxy,
    )
    return response


def redirect(url: str, *, message: str | None = None, level: str = "success") -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    if message:
        flash(response, message, level)
    return response


def pop_flash(request: Request) -> dict[str, str] | None:
    raw = request.cookies.get(FLASH_COOKIE)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return {"message": str(data.get("message", "")), "level": str(data.get("level", "success"))}


def clear_flash(response) -> None:
    response.delete_cookie(FLASH_COOKIE)


# ---------------------------------------------------------------------------
# Render helper
# ---------------------------------------------------------------------------
def render(
    request: Request,
    template: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
    clear_flash_cookie: bool = False,
):
    """Render a template with the shared context every page needs."""
    info = read_session(request)
    base: dict[str, Any] = {
        "request": request,
        "staff": getattr(request.state, "staff", None),
        "session_info": info,
        "csrf_token": info.csrf_token if info else "",
        "flash": pop_flash(request),
        "current_path": request.url.path,
        "counters": getattr(request.state, "counters", LayoutCounters()),
        "panels_offline": getattr(request.state, "panels_offline", 0),
        "version": __version__,
    }
    base.update(context or {})
    response = templates.TemplateResponse(request, template, base, status_code=status_code)
    if clear_flash_cookie:
        clear_flash(response)
    return response


__all__ = [
    "STATIC_DIR",
    "TEMPLATES_DIR",
    "clear_flash",
    "flash",
    "pop_flash",
    "redirect",
    "render",
    "templates",
]
