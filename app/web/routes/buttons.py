"""دکمه‌ها و ایموجی — برچسب، رنگ و ایموجی پرمیوم هر عنصر ربات، و چیدمان منوی اصلی.

هر عنصر ربات یک کلید پایدار دارد (``menu.buy``، ``emoji.fire``) و پیش‌فرضش در
:mod:`app.services.appearance` تعریف شده است.  این صفحه فقط بازنویسی‌های اپراتور
را در جدول ``button_styles`` می‌نویسد: کادر خالی یعنی «همان پیش‌فرض ربات»، پس
هر ردیف را می‌توان بی‌خطر به حالت اول برگرداند.

فرم فقط فیلدهایی را می‌فرستد که واقعاً در صفحه رندر شده‌اند (``<field>__<key>``)
و :meth:`AppearanceStore.bulk_set` آن‌ها را یک‌جا ذخیره می‌کند؛ بنابراین صفحه با
صدها کلید هم فقط یک رفت‌وبرگشت به دیتابیس دارد.

The same page also owns the **main-menu layout editor** (``POST /buttons/layout``
and ``POST /buttons/layout/reset``), which drives
:mod:`app.services.menu_layout`.  It follows the dual-shape contract of
:mod:`app.web.routes.reorder`: ``panel.js`` posts the whole arrangement as JSON
and reads ``{"ok", "changed", "message"}``, while a browser without JavaScript
posts the same page as repeated ``keys`` fields and gets the usual 303 + flash.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError, ValidationError
from app.core.logging import get_logger
from app.core.money import fa_digits
from app.db.models import Staff
from app.services import menu_layout
from app.services.appearance import (
    CATALOG,
    GROUP_LABELS,
    STYLE_VALUES,
    appearance,
    catalog_groups,
)
from app.web.deps import form_dict
from app.web.security import get_db_session, require_manager, verify_csrf
from app.web.templating import redirect, render

log = get_logger(__name__)
router = APIRouter(tags=["buttons"])

BASE = f"{settings.panel_prefix}/buttons"

#: Marker ``panel.js`` sends with every background call (the same one
#: :mod:`app.web.routes.reorder` reads).
FETCH_HEADER = "x-requested-with"

#: Said whenever the request itself is no longer trustworthy — never the raw CSRF
#: reason, which names internals.
STALE_SESSION_MESSAGE = "نشست شما منقضی شده است؛ صفحه را دوباره باز کنید و چیدمان را از نو ذخیره کنید."

#: The ``layout`` field arrived but is not the JSON the editor sends (a truncated
#: body, a hand-written POST).  Says what to do, never what was parsed.
BAD_LAYOUT_MESSAGE = "چیدمان فرستاده\u200cشده خوانده نشد؛ صفحه را دوباره باز کنید و دوباره ذخیره کنید."

#: پیشوند فیلدهای فرم → کلیدهای :meth:`AppearanceStore.set_visual`.
#: ``label__menu.buy`` یعنی برچسب عنصر ``menu.buy``.
FIELD_MAP: dict[str, str] = {
    "label": "label",
    "style": "style",
    "emoji_id": "icon_custom_emoji_id",
    "fallback": "emoji_fallback",
}

#: عناصر منوی اصلی که در پیش‌نمایش گوشی پایین صفحه نشان داده می‌شوند.
PREVIEW_KEYS: tuple[str, ...] = ("menu.buy", "menu.my_services", "menu.wallet", "menu.support")

STYLE_TITLES: dict[str, str] = {
    "primary": "آبی",
    "success": "سبز",
    "danger": "قرمز",
    "link": "لینک",
}

#: گزینه‌های کشویی رنگ؛ مقدار خالی یعنی «پیش‌فرض ربات».
STYLE_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "", "label": "پیش‌فرض"},
    *({"value": value, "label": STYLE_TITLES.get(value, value)} for value in STYLE_VALUES),
)


# ---------------------------------------------------------------------------
# کمک‌کننده‌ها
# ---------------------------------------------------------------------------
def _ordered_groups(grouped: dict[str, list[Any]]) -> list[str]:
    """گروه‌های شناخته‌شده اول (به ترتیب :data:`GROUP_LABELS`)، بقیه ته فهرست."""
    known = [key for key in GROUP_LABELS if key in grouped]
    extra = sorted(key for key in grouped if key not in GROUP_LABELS)
    return known + extra


async def _collect(
    grouped: dict[str, list[Any]],
    session: AsyncSession,
    kind: str,
    overrides: dict[str, dict[str, str | None]],
) -> list[dict[str, Any]]:
    """ردیف‌های یک نوع عنصر (دکمه یا ایموجی) با مقادیر نهاییِ پس از بازنویسی."""
    rows: list[dict[str, Any]] = []
    for group in _ordered_groups(grouped):
        for spec in grouped[group]:
            if spec.kind != kind:
                continue
            # resolve زنجیره‌ی کامل را می‌پوشاند: بازنویسی اپراتور، پیش‌فرض کاتالوگ و
            # حتی ایموجی پرمیومی که دکمه از ایموجیِ پیوندشده قرض می‌گیرد.
            resolved = await appearance.resolve(spec.key, session)
            emoji = resolved.emoji_fallback or ""
            rows.append(
                {
                    "key": spec.key,
                    "label": spec.label,
                    "group": group,
                    "group_label": GROUP_LABELS.get(group, group),
                    "value": resolved.label,
                    "style": resolved.style or "",
                    "icon_custom_emoji_id": resolved.icon_custom_emoji_id or "",
                    "emoji_fallback": emoji,
                    "emoji": emoji,
                    "overridden": spec.key in overrides,
                }
            )
    return rows


def _sections(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """ردیف‌ها را به کارت‌های گروه‌بندی‌شده (به ترتیب ورودی) تبدیل می‌کند."""
    sections: list[dict[str, Any]] = []
    for row in rows:
        if not sections or sections[-1]["key"] != row["group"]:
            sections.append({"key": row["group"], "label": row["group_label"], "rows": []})
        sections[-1]["rows"].append(row)
    return sections


# ---------------------------------------------------------------------------
# The main-menu layout editor
# ---------------------------------------------------------------------------
async def _chip(key: str, session: AsyncSession) -> dict[str, Any]:
    """One menu button as the editor draws it: the label the bot would show."""
    resolved = await appearance.resolve(key, session)
    return {"key": key, "label": resolved.label, "emoji": resolved.emoji_fallback or ""}


async def _builder(session: AsyncSession) -> dict[str, Any]:
    """Everything the «چیدمان منوی اصلی» card needs to render server-side.

    The editor is rendered in full without JavaScript, so the page is readable
    (and testable) before the script takes over: rows of chips, the palette of
    buttons that are *not* in the menu, and the caps the service enforces.
    """
    rows = await menu_layout.rows(session)
    hidden = await menu_layout.hidden_keys(session)
    available = set(await menu_layout.available_keys(session))

    menu_rows: list[dict[str, Any]] = []
    for row in rows:
        chips = [await _chip(key, session) for key in row.keys]
        for chip in chips:
            chip["palette"] = False
            chip["note"] = ""
        menu_rows.append({"index": row.index, "chips": chips})

    palette: list[dict[str, Any]] = []
    for key in menu_layout.ADDABLE_KEYS:
        if key not in hidden and key not in available:
            continue
        chip = await _chip(key, session)
        chip["palette"] = True
        # A palette button is either one the owner removed or one that never was
        # in the menu; only the first deserves the "removed" mark.
        chip["note"] = "حذف\u200cشده" if key in hidden else ""
        palette.append(chip)

    return {
        "menu_rows": menu_rows,
        "menu_palette": palette,
        "menu_button_count": sum(len(row["chips"]) for row in menu_rows) + len(palette),
        "menu_layout_url": f"{BASE}/layout",
        "menu_reset_url": f"{BASE}/layout/reset",
        "menu_max_rows": menu_layout.MAX_ROWS,
        "menu_max_buttons": menu_layout.MAX_ROW_BUTTONS,
    }


def _wants_json(request: Request) -> bool:
    """True for the background caller (``panel.js`` / an API client)."""
    if request.headers.get(FETCH_HEADER, "").strip().lower() == "fetch":
        return True
    return "application/json" in request.headers.get("accept", "").lower()


def _json(*, ok: bool, changed: int, message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"ok": ok, "changed": changed, "message": message}, status_code=status_code)


def _refuse(exc: AppError, *, wants_json: bool):
    """One calm sentence back to the caller, in whichever shape it asked for."""
    if wants_json:
        return _json(ok=False, changed=0, message=exc.message, status_code=400)
    return redirect(BASE, message=exc.message, level="danger")


def _rows_from_json(raw: str) -> list[list[str]]:
    """The background payload: a JSON array of rows, each an array of keys.

    Anything else — a string, a nested object, a row that is not an array — is
    refused instead of guessed at: a malformed body must never build a menu.
    """
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValidationError(BAD_LAYOUT_MESSAGE) from exc
    if not isinstance(data, list):
        raise ValidationError(BAD_LAYOUT_MESSAGE)
    rows: list[list[str]] = []
    for row in data:
        if not isinstance(row, list):
            raise ValidationError(BAD_LAYOUT_MESSAGE)
        rows.append([str(key) for key in row])
    return rows


def _rows_from_form(form: Any) -> list[list[str]]:
    """The no-JavaScript payload: repeated ``keys`` fields, in document order.

    The editor renders one hidden ``keys`` input per chip plus one ``row_break``
    marker at the end of every row, so the whole arrangement survives a plain
    form post.  ``multi_items()`` is what makes this readable at all: it keeps
    the fields in the order the browser sent them, while ``form_dict`` collapses
    the repeats and would lose both the row split and the order inside a row.
    """
    rows: list[list[str]] = [[]]
    for name, value in form.multi_items():
        if name == "row_break":
            rows.append([])
        elif name == "keys":
            key = str(value or "").strip()
            if key:
                rows[-1].append(key)
    return rows


def _requested_rows(form: Any) -> list[list[str]]:
    """The whole menu from whichever shape the caller sent."""
    raw = form.get("layout")
    if raw is not None and str(raw).strip():
        return _rows_from_json(str(raw))
    return _rows_from_form(form)


# ---------------------------------------------------------------------------
# صفحه‌ی ویرایش
# ---------------------------------------------------------------------------
@router.get("/buttons")
async def list_buttons(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    grouped = catalog_groups()
    overrides = await appearance.load(session)

    buttons = await _collect(grouped, session, "button", overrides)
    emojis = await _collect(grouped, session, "emoji", overrides)
    menu_preview = [row for row in buttons if row["key"] in PREVIEW_KEYS]
    builder = await _builder(session)

    return render(
        request,
        "buttons.html",
        {
            "page_title": "دکمه‌ها و ایموجی",
            "page_subtitle": (
                f"{fa_digits(len(buttons))} دکمه · {fa_digits(len(emojis))} ایموجی · "
                f"{fa_digits(len(overrides))} سفارشی‌شده"
            ),
            "buttons": buttons,
            "emojis": emojis,
            "button_groups": _sections(buttons),
            "emoji_groups": _sections(emojis),
            "group_labels": GROUP_LABELS,
            "style_values": STYLE_VALUES,
            "style_options": STYLE_OPTIONS,
            "menu_preview": menu_preview,
            "custom_count": len(overrides),
            "base_url": BASE,
            **builder,
        },
    )


# ---------------------------------------------------------------------------
# ذخیره‌ی گروهی
# ---------------------------------------------------------------------------
@router.post("/buttons")
async def save_buttons(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    entries: dict[str, dict[str, str | None]] = {}
    for field, raw in form.items():
        prefix, separator, key = field.partition("__")
        if not separator or prefix not in FIELD_MAP or key not in CATALOG:
            continue
        value = "" if raw is None else str(raw).strip()
        entries.setdefault(key, {})[FIELD_MAP[prefix]] = value

    if not entries:
        return redirect(BASE, message="ردیفی برای ذخیره پیدا نشد.", level="warning")

    saved = await appearance.bulk_set(session, entries)
    await session.commit()

    log.info("%s ظاهر %d عنصر ربات را به‌روزرسانی کرد", staff.login or staff.name, saved)
    return redirect(BASE, message=f"{fa_digits(saved)} ردیف ذخیره شد.")


@router.post("/buttons/reset")
async def reset_buttons(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    removed = await appearance.reset(session)
    await session.commit()

    log.info("%s تنظیمات ظاهر ربات را بازنشانی کرد (%d ردیف)", staff.login or staff.name, removed)
    if not removed:
        return redirect(BASE, message="تنظیمی برای بازنشانی وجود نداشت.", level="info")
    return redirect(BASE, message=f"{fa_digits(removed)} ردیف به پیش‌فرض بازگشت.")


# ---------------------------------------------------------------------------
# چیدمان منوی اصلی
# ---------------------------------------------------------------------------
@router.post("/buttons/layout")
async def save_layout(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    """Save the whole main menu (the payload shapes are in the module docstring).

    The service is the only judge of what a valid menu is: it refuses an unknown
    key, refuses an empty menu, drops a duplicate and records a *hidden* row for
    every shipped button the payload leaves out.  This handler only reads the
    request, translates it and turns a refusal into one operator sentence.
    """
    form = await request.form()
    wants_json = _wants_json(request)

    try:
        verify_csrf(request, form.get("csrf_token"))
    except HTTPException:
        log.warning("Menu layout refused: CSRF validation failed for %s", request.url.path)
        if wants_json:
            return _json(ok=False, changed=0, message=STALE_SESSION_MESSAGE, status_code=403)
        return redirect(BASE, message=STALE_SESSION_MESSAGE, level="danger")

    # The ✕ inside a chip is a real submit button, so the no-JavaScript path can
    # take one button out of the menu and save the rest in a single click.
    remove_key = str(form.get("remove_key") or "").strip()

    try:
        rows = _requested_rows(form)
        was_present = any(remove_key in row for row in rows)
        if remove_key and was_present:
            rows = [[key for key in row if key != remove_key] for row in rows]
        changed = await menu_layout.save(session, rows)
        await session.commit()
    except AppError as exc:
        await session.rollback()
        return _refuse(exc, wants_json=wants_json)

    log.info("%s main menu layout saved (%d rows)", staff.login or staff.name, changed)

    if remove_key and was_present:
        label = (await appearance.resolve(remove_key, session)).label
        message = f"دکمه «{label}» از منوی اصلی برداشته شد؛ از «دکمه\u200cهای بیرون از منو» می\u200cتوانید برگردانیدش."
    else:
        message = "چیدمان منوی اصلی ذخیره شد."

    if wants_json:
        return _json(ok=True, changed=changed, message=message, status_code=200)
    return redirect(BASE, message=message)


@router.post("/buttons/layout/reset")
async def reset_layout(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    """Back to the shipped main menu — a delete, since the overrides are sparse."""
    form = await request.form()
    wants_json = _wants_json(request)

    try:
        verify_csrf(request, form.get("csrf_token"))
    except HTTPException:
        log.warning("Menu layout reset refused: CSRF validation failed for %s", request.url.path)
        if wants_json:
            return _json(ok=False, changed=0, message=STALE_SESSION_MESSAGE, status_code=403)
        return redirect(BASE, message=STALE_SESSION_MESSAGE, level="danger")

    try:
        await menu_layout.reset(session)
        changed = len(await menu_layout.rows(session))
        await session.commit()
    except AppError as exc:
        await session.rollback()
        return _refuse(exc, wants_json=wants_json)

    log.info("%s main menu layout reset to default", staff.login or staff.name)
    message = "چیدمان منوی اصلی به حالت پیش\u200cفرض برگشت."

    if wants_json:
        return _json(ok=True, changed=changed, message=message, status_code=200)
    return redirect(BASE, message=message)


__all__ = ["router"]
