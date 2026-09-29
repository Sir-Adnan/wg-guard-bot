"""متن‌های ربات — ویرایش همه‌ی جمله‌های ربات از پنل.

کاتالوگ پیش‌فرض در ``app/locales/fa.json`` است و بازنویسی‌های اپراتور در جدول
``bot_texts`` نگه داشته می‌شود؛ :class:`app.services.texts.TextStore` این دو را
روی هم می‌گذارد.  این صفحه فقط لایه‌ی نمایش است: هر کلید یک ``textarea``
می‌گیرد و فرمِ ذخیره تنها فیلدهایی را می‌فرستد که واقعاً تغییر کرده‌اند، پس
صفحه‌ای با صدها متن هم چیزی را بی‌دلیل بازنویسی نمی‌کند.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.money import fa_digits
from app.db.models import Staff
from app.services.texts import GROUP_LABELS, texts
from app.web.deps import form_dict, form_str
from app.web.security import get_db_session, require_manager, verify_csrf
from app.web.templating import redirect, render

log = get_logger(__name__)
router = APIRouter(tags=["texts"])

BASE = f"{settings.panel_prefix}/texts"

#: پیشوند نام فیلدهای فرم؛ ``value__menu.buy`` یعنی کلید ``menu.buy``.
FIELD_PREFIX = "value__"


# ---------------------------------------------------------------------------
# کمک‌کننده‌ها
# ---------------------------------------------------------------------------
def _ordered_group_keys(grouped: dict[str, list[Any]]) -> list[str]:
    """گروه‌های شناخته‌شده اول (به ترتیب :data:`GROUP_LABELS`)، بقیه ته فهرست."""
    known = [key for key in GROUP_LABELS if key in grouped]
    extra = sorted(key for key in grouped if key not in GROUP_LABELS)
    return known + extra


def _matches(key: str, value: str, needle: str) -> bool:
    """جست‌وجوی سمت سرور روی کلید و متن (برای textarea فیلتر سمت کلاینت کار نمی‌کند)."""
    return needle in key.casefold() or needle in (value or "").casefold()


def _url(*, group: str = "", q: str = "") -> str:
    params: list[str] = []
    if group:
        params.append("group=" + quote(group))
    if q:
        params.append("q=" + quote(q))
    return f"{BASE}?{'&'.join(params)}" if params else BASE


# ---------------------------------------------------------------------------
# صفحه‌ی ویرایش
# ---------------------------------------------------------------------------
@router.get("/texts")
async def list_texts(
    request: Request,
    group: str = Query("", description="فقط یک گروه نمایش داده شود"),
    q: str = Query("", description="جست‌وجو در کلید و متن"),
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    grouped = await texts.grouped(session)
    needle = (q or "").strip().casefold()
    active_group = (group or "").strip()

    tabs: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    total = 0
    custom_total = 0

    for key in _ordered_group_keys(grouped):
        rows = grouped[key]
        tabs.append({"key": key, "label": GROUP_LABELS.get(key, key), "count": len(rows)})
        if active_group and key != active_group:
            continue
        visible = [row for row in rows if not needle or _matches(row[0], row[1], needle)]
        if not visible:
            continue
        customs = sum(1 for row in visible if row[2])
        total += len(visible)
        custom_total += customs
        sections.append(
            {
                "key": key,
                "label": GROUP_LABELS.get(key, key),
                "rows": [{"key": row[0], "value": row[1], "is_custom": row[2]} for row in visible],
                "count": len(visible),
                "custom_count": customs,
            }
        )

    if needle and not sections:
        subtitle = f"چیزی برای «{q.strip()}» پیدا نشد"
    elif active_group:
        subtitle = f"{fa_digits(total)} متن در گروه «{GROUP_LABELS.get(active_group, active_group)}»"
    else:
        subtitle = f"{fa_digits(total)} متن · {fa_digits(custom_total)} سفارشی‌شده"

    return render(
        request,
        "texts.html",
        {
            "page_title": "متن‌های ربات",
            "page_subtitle": subtitle,
            "tabs": tabs,
            "sections": sections,
            "active_group": active_group,
            "q": q.strip(),
            "total": total,
            "custom_total": custom_total,
            "group_labels": GROUP_LABELS,
        },
    )


# ---------------------------------------------------------------------------
# ذخیره‌ی گروهی
# ---------------------------------------------------------------------------
@router.post("/texts")
async def save_texts(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    current = await texts.effective(session)
    changes: dict[str, str] = {}
    for field, raw in form.items():
        if not field.startswith(FIELD_PREFIX):
            continue
        key = field[len(FIELD_PREFIX) :]
        if key not in current:
            continue
        value = "" if raw is None else str(raw)
        if value != current[key]:
            changes[key] = value

    group = form_str(form, "group")
    q = form_str(form, "q")
    target = _url(group=group, q=q)

    if not changes:
        return redirect(target, message="تغییری برای ذخیره وجود نداشت.", level="info")

    try:
        saved = await texts.set_many(session, changes)
        # کش درون‌فرآیندی باید فوراً تازه شود تا ربات بدون ری‌استارت متن جدید را بفرستد.
        await texts.load(session, force=True)
        await session.commit()
    except AppError as exc:
        return redirect(target, message=exc.message, level="danger")

    log.info("%s متن ربات را به‌روزرسانی کرد (%d کلید)", staff.login or staff.name, saved)
    return redirect(target, message=f"{fa_digits(saved)} متن ذخیره شد.")


@router.post("/texts/{key}/reset")
async def reset_text(
    key: str,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    target = _url(group=form_str(form, "group"), q=form_str(form, "q"))
    if key not in texts.catalog:
        return redirect(target, message="این کلید در متن‌های پیش‌فرض وجود ندارد.", level="danger")

    try:
        await texts.reset_text(session, key)
        await texts.load(session, force=True)
        await session.commit()
    except AppError as exc:
        return redirect(target, message=exc.message, level="danger")

    log.info("%s متن %s را به پیش‌فرض بازگرداند", staff.login or staff.name, key)
    return redirect(target, message="متن به نسخه‌ی پیش‌فرض بازگشت.")


__all__ = ["router"]
