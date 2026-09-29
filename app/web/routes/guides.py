"""آموزش‌ها — مطالب آموزش اتصال، راهنمای خرید و پرسش‌های پرتکرار (Guide).

این مطالب از دکمه‌های ربات به مشتری نشان داده می‌شوند؛ فقط مطلبِ **فعال** و
به ترتیب ``sort_order`` دیده می‌شود.  متن مطلب عمداً HTML تلگرام است
(``<b>``، ``<code>`` و …)، پس در پیش‌نمایشِ پنل هم دست‌نخورده و با ``|safe``
رندر می‌شود تا اپراتور قالب‌بندی را قبل از انتشار ببیند.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError, ValidationError
from app.core.money import fa_digits
from app.db.models import Staff
from app.services.guides import PLATFORM_LABELS, SECTION_LABELS, guides
from app.web.deps import form_bool, form_dict, form_int, form_str
from app.web.security import get_db_session, require_manager, verify_csrf
from app.web.templating import redirect, render

router = APIRouter(tags=["guides"])

BASE = f"{settings.panel_prefix}/guides"

#: فیلتر «همه‌ی بخش‌ها».
ALL = "all"

#: گزینه‌های پیوست مطلب.  ``none`` یعنی بدون فایل.
MEDIA_TYPES: tuple[tuple[str, str], ...] = (
    ("photo", "تصویر"),
    ("document", "فایل"),
    ("none", "بدون پیوست"),
)

MEDIA_VALUES: frozenset[str] = frozenset(key for key, _label in MEDIA_TYPES)

#: سقف فهرست؛ بیشتر از این تعداد مطلب در یک صفحه نمایش داده نمی‌شود.
ROW_LIMIT = 200


# ---------------------------------------------------------------------------
# کمک‌کننده‌ها
# ---------------------------------------------------------------------------
def _payload(form: dict[str, Any]) -> dict[str, Any]:
    """فیلدهای فرم را به ستون‌های :class:`~app.db.models.Guide` تبدیل می‌کند."""
    title = form_str(form, "title")
    if not title:
        raise ValidationError("عنوان مطلب نمی‌تواند خالی باشد.")

    section = form_str(form, "section", "connect")
    if section not in SECTION_LABELS:
        raise ValidationError("بخش انتخابی نامعتبر است.")

    platform = form_str(form, "platform", "all")
    if platform not in PLATFORM_LABELS:
        raise ValidationError("پلتفرم انتخابی نامعتبر است.")

    media_type = form_str(form, "media_type", "none")
    if media_type not in MEDIA_VALUES:
        raise ValidationError("نوع پیوست نامعتبر است.")

    return {
        "title": title[:160],
        "section": section,
        "platform": platform,
        "body": form_str(form, "body"),
        "media_file_id": form_str(form, "media_file_id") or None,
        "media_type": None if media_type == "none" else media_type,
        "sort_order": form_int(form, "sort_order", 0),
        "is_active": form_bool(form, "is_active"),
    }


def _back(form: dict[str, Any]) -> str:
    """آدرس بازگشت با حفظ فیلترهای فهرست."""
    query: dict[str, str] = {}
    section = form_str(form, "back_section", ALL)
    if section and section != ALL:
        query["section"] = section
    keyword = form_str(form, "back_q")
    if keyword:
        query["q"] = keyword
    return f"{BASE}?{urlencode(query)}" if query else BASE


# ---------------------------------------------------------------------------
# فهرست
# ---------------------------------------------------------------------------
@router.get("/guides")
async def list_guides(
    request: Request,
    section: str = Query(ALL, description="فیلتر بخش مطلب"),
    q: str = Query("", description="جست‌وجو در عنوان و متن"),
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    if section not in SECTION_LABELS:
        section = ALL
    keyword = (q or "").strip()

    rows = await guides.all(session, limit=ROW_LIMIT)
    counts = await guides.sections(session)
    all_count = await guides.count(session, active_only=False)

    if section != ALL:
        rows = [row for row in rows if row.section == section]
    if keyword:
        needle = keyword.casefold()
        rows = [row for row in rows if needle in f"{row.title} {row.body}".casefold()]

    subtitle = f"{fa_digits(len(rows))} مطلب"
    if section != ALL:
        subtitle += f" در بخش «{SECTION_LABELS[section]}»"
    if keyword:
        subtitle += f" برای «{keyword}»"

    return render(
        request,
        "guides.html",
        {
            "page_title": "آموزش‌ها",
            "page_subtitle": subtitle,
            "rows": rows,
            "sections": list(SECTION_LABELS.items()),
            "platforms": list(PLATFORM_LABELS.items()),
            "media_types": MEDIA_TYPES,
            "section_labels": SECTION_LABELS,
            "platform_labels": PLATFORM_LABELS,
            "section_counts": counts,
            "all_count": all_count,
            "row_limit": ROW_LIMIT,
            "truncated": all_count > ROW_LIMIT,
            "active_section": section,
            "q": keyword,
            "base_url": BASE,
        },
    )


# ---------------------------------------------------------------------------
# ساخت / ویرایش / حذف
# ---------------------------------------------------------------------------
@router.post("/guides")
async def create_guide(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        guide = await guides.create(session, **_payload(form))
        title = guide.title
        await session.commit()
    except AppError as exc:
        return redirect(_back(form), message=exc.message, level="error")

    return redirect(_back(form), message=f"مطلب «{title}» ساخته شد.")


@router.post("/guides/{guide_id}")
async def update_guide(
    guide_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        data = _payload(form)
        # پیوست جداست: سرویس مقدار ``None`` را «تغییر نده» می‌فهمد، ولی
        # اپراتور باید بتواند پیوست را هم بردارد.
        guide = await guides.update(
            session,
            guide_id,
            **{key: value for key, value in data.items() if key not in ("media_file_id", "media_type")},
        )
        guide.media_file_id = data["media_file_id"]
        guide.media_type = data["media_type"]
        await session.flush()
        title = guide.title
        await session.commit()
    except AppError as exc:
        return redirect(_back(form), message=exc.message, level="error")

    return redirect(_back(form), message=f"مطلب «{title}» ذخیره شد.")


@router.post("/guides/{guide_id}/toggle")
async def toggle_guide(
    guide_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        guide = await guides.get(session, guide_id)
        guide.is_active = not guide.is_active
        await session.flush()
        active, title = guide.is_active, guide.title
        await session.commit()
    except AppError as exc:
        return redirect(_back(form), message=exc.message, level="error")

    state = "فعال" if active else "غیرفعال"
    return redirect(_back(form), message=f"مطلب «{title}» {state} شد.")


@router.post("/guides/{guide_id}/delete")
async def delete_guide(
    guide_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        guide = await guides.get(session, guide_id)
        title = guide.title
        await guides.delete(session, guide_id)
        await session.commit()
    except AppError as exc:
        return redirect(_back(form), message=exc.message, level="error")

    return redirect(_back(form), message=f"مطلب «{title}» حذف شد.")


__all__ = ["router"]
