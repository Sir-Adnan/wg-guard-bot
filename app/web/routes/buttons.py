"""دکمه‌ها و ایموجی — برچسب، رنگ و ایموجی پرمیوم هر عنصر ربات.

هر عنصر ربات یک کلید پایدار دارد (``menu.buy``، ``emoji.fire``) و پیش‌فرضش در
:mod:`app.services.appearance` تعریف شده است.  این صفحه فقط بازنویسی‌های اپراتور
را در جدول ``button_styles`` می‌نویسد: کادر خالی یعنی «همان پیش‌فرض ربات»، پس
هر ردیف را می‌توان بی‌خطر به حالت اول برگرداند.

فرم فقط فیلدهایی را می‌فرستد که واقعاً در صفحه رندر شده‌اند (``<field>__<key>``)
و :meth:`AppearanceStore.bulk_set` آن‌ها را یک‌جا ذخیره می‌کند؛ بنابراین صفحه با
صدها کلید هم فقط یک رفت‌وبرگشت به دیتابیس دارد.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.core.money import fa_digits
from app.db.models import Staff
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


__all__ = ["router"]
