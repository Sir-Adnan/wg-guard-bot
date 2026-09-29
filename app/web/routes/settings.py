"""تنظیمات فروشگاه — صفحه‌ای که از رجیستری :mod:`app.services.settings_store` ساخته می‌شود.

هر گروه یک فرم جدا دارد تا یک مقدار خراب، ویرایش‌های گروه‌های دیگر را از بین
نبرد.  «پیشرفته» فقط برای مالک فروشگاه است (هم در نمایش، هم در ذخیره).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings as env
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.money import fa_digits
from app.db.models import Staff, StaffRole
from app.services.settings_store import (
    GROUP_LABELS,
    SettingSpec,
    app_settings,
    spec_groups,
)
from app.web.deps import form_bool, form_dict, form_float, form_int, form_money, form_str
from app.web.security import get_db_session, require_manager, verify_csrf
from app.web.templating import redirect, render

log = get_logger(__name__)
router = APIRouter(tags=["settings"])

BASE = f"{env.panel_prefix}/settings"

#: گروهی که فقط مالک فروشگاه می‌بیند و ذخیره می‌کند.
ADVANCED_GROUP = "advanced"

GROUP_ICONS: dict[str, str] = {
    "shop": "box",
    "payment": "card",
    "test": "gift",
    "membership": "users",
    "notify": "bell",
    "appearance": "palette",
    "advanced": "sliders",
}

ENV_LABELS: dict[str, str] = {
    "production": "عملیاتی (production)",
    "development": "توسعه (development)",
    "test": "آزمایشی (test)",
}

BOT_MODE_LABELS: dict[str, str] = {
    "polling": "Polling — دریافت خودکار از تلگرام",
    "webhook": "Webhook — دریافت از طریق وب‌هوک",
}

#: دستور داخلی ربات برای ورود به پنل مدیریت تلگرامی.
BOT_ADMIN_COMMAND = "/admin"


# ---------------------------------------------------------------------------
# کمک‌کننده‌ها
# ---------------------------------------------------------------------------
def _guard_group(group: str, staff: Staff) -> None:
    """گروه «پیشرفته» مخصوص مالک است."""
    if group == ADVANCED_GROUP and staff.role != StaffRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="گروه «پیشرفته» فقط برای مالک فروشگاه است.",
        )


def _parse(spec: SettingSpec, form: dict[str, Any]) -> Any:
    """مقدار فرم را با نوع اعلام‌شده در spec به مقدار قابل ذخیره تبدیل می‌کند."""
    if spec.type == "bool":
        return form_bool(form, spec.key)
    if spec.type == "money":
        # اپراتور به تومان وارد می‌کند؛ ذخیره‌سازی همیشه ریال است.
        return form_money(form, spec.key, int(spec.default or 0))
    if spec.type == "int":
        return form_int(form, spec.key, int(spec.default or 0))
    if spec.type == "float":
        return form_float(form, spec.key, float(spec.default or 0))
    return form_str(form, spec.key, str(spec.default or ""))


def _group_label(group: str) -> str:
    return GROUP_LABELS.get(group, group)


def _deployment() -> dict[str, Any]:
    """واقعیت‌های استقرار؛ توکن ربات هرگز نمایش داده نمی‌شود."""
    return {
        "env": env.env,
        "env_label": ENV_LABELS.get(env.env, env.env),
        "bot_mode": env.bot_mode,
        "bot_mode_label": BOT_MODE_LABELS.get(env.bot_mode, env.bot_mode),
        "timezone": env.timezone,
        "panel_base_url": env.panel_base_url,
        "panel_prefix": env.panel_prefix,
        "currency_display": env.currency_display,
        "bot_token_set": bool(env.bot_token),
        "bot_admin_command": BOT_ADMIN_COMMAND,
    }


# ---------------------------------------------------------------------------
# نمایش
# ---------------------------------------------------------------------------
@router.get("/settings")
async def settings_page(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    values = await app_settings.load(session, force=True)
    is_owner = staff.role == StaffRole.OWNER

    sections: list[dict[str, Any]] = []
    for key, specs in spec_groups().items():
        if key == ADVANCED_GROUP and not is_owner:
            continue
        sections.append(
            {
                "key": key,
                "label": _group_label(key),
                "icon": GROUP_ICONS.get(key, "settings"),
                "specs": specs,
                "count": len(specs),
                "owner_only": key == ADVANCED_GROUP,
            }
        )

    return render(
        request,
        "settings.html",
        {
            "page_title": "تنظیمات فروشگاه",
            "page_subtitle": f"{fa_digits(len(sections))} گروه تنظیمات · {fa_digits(sum(s['count'] for s in sections))} گزینه",
            "sections": sections,
            "values": values,
            "deployment": _deployment(),
        },
    )


# ---------------------------------------------------------------------------
# ذخیره / بازنشانی
# ---------------------------------------------------------------------------
@router.post("/settings")
async def save_settings(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    group = form_str(form, "group")
    grouped = spec_groups()
    if group not in grouped:
        return redirect(BASE, message="گروه تنظیمات نامعتبر است.", level="danger")
    _guard_group(group, staff)

    payload = {spec.key: _parse(spec, form) for spec in grouped[group]}
    try:
        changed = await app_settings.set_many(session, payload)
        # ربات باید بلافاصله مقدار تازه را ببیند (کش درون‌فرآیندی).
        await app_settings.load(session, force=True)
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    log.info("%s تنظیمات گروه %s را ذخیره کرد", staff.login or staff.name, group)
    return redirect(
        f"{BASE}#group-{group}",
        message=f"تنظیمات «{_group_label(group)}» ذخیره شد ({fa_digits(len(changed))} گزینه).",
    )


@router.post("/settings/reset")
async def reset_group(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    group = form_str(form, "group")
    grouped = spec_groups()
    if group not in grouped:
        return redirect(BASE, message="گروه تنظیمات نامعتبر است.", level="danger")
    _guard_group(group, staff)

    try:
        for spec in grouped[group]:
            await app_settings.reset(session, spec.key)
        await app_settings.load(session, force=True)
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    log.info("%s تنظیمات گروه %s را بازنشانی کرد", staff.login or staff.name, group)
    return redirect(
        f"{BASE}#group-{group}",
        message=f"تنظیمات «{_group_label(group)}» به مقدار پیش‌فرض بازگشت.",
    )


__all__ = ["router"]
