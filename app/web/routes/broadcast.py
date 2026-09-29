"""ارسال همگانی — نوشتن، زمان‌بندی و پیگیری پیام‌های گروهی.

منطق ارسال در :mod:`app.services.broadcast` است؛ این ماژول فقط فرم پنل را
اعتبارسنجی می‌کند، کارزار را می‌سازد و به سرویس می‌سپارد.  اجرای واقعی در
پس‌زمینه و با فاصله‌ی زمانی (throttle) انجام می‌شود تا تلگرام ربات را محدود نکند.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError, ValidationError
from app.core.jalali import jalali_datetime, now_utc, to_utc
from app.core.money import en_digits, fa_digits
from app.db.models import BroadcastStatus, Staff
from app.services.broadcast import AUDIENCES, broadcasts
from app.services.notifications import Media
from app.web.deps import form_dict, form_str
from app.web.security import get_db_session, require_manager, verify_csrf
from app.web.templating import redirect, render

router = APIRouter(tags=["broadcast"])

BASE = f"{settings.panel_prefix}/broadcast"

#: نوع پیوست؛ کلید خالی یعنی پیام فقط متن است.
MEDIA_TYPES: tuple[tuple[str, str], ...] = (
    ("", "بدون فایل"),
    ("photo", "عکس"),
    ("video", "ویدیو"),
    ("document", "سند"),
)

#: سطح پیام‌های خطا، مطابق قرارداد پنل (``danger``/``success`` برای موفقیت).
ERROR_LEVEL = "error"


# ---------------------------------------------------------------------------
# کمک‌کننده‌ها
# ---------------------------------------------------------------------------
def _audience_label(key: str) -> str:
    return AUDIENCES.get(key, key)


def _media(form: dict[str, Any]) -> Media | None:
    """پیوست اختیاری پیام را از فرم می‌سازد (file_id تلگرام)."""
    kind = form_str(form, "media_type")
    file_id = form_str(form, "media_file_id")

    if not kind:
        return None
    if kind not in {key for key, _label in MEDIA_TYPES if key}:
        raise ValidationError("نوع فایل پیوست نامعتبر است.")
    if not file_id:
        raise ValidationError("برای پیوست فایل، شناسه فایل (file_id) را هم وارد کنید.")
    return Media(kind=kind, file_id=file_id)  # type: ignore[arg-type]


def _scheduled_at(form: dict[str, Any]) -> datetime | None:
    """ورودی ``datetime-local`` مرورگر → زمان آگاه از منطقه UTC.

    فقط زمان آینده معتبر است؛ خالی یا گذشته یعنی «همین حالا ارسال شود».
    """
    raw = en_digits(form_str(form, "scheduled_at")).strip()
    if not raw:
        return None
    try:
        parsed = to_utc(datetime.fromisoformat(raw))
    except ValueError as exc:
        raise ValidationError("زمان‌بندی وارد‌شده معتبر نیست.") from exc
    if parsed is None or parsed <= now_utc():
        return None
    return parsed


# ---------------------------------------------------------------------------
# نمایش
# ---------------------------------------------------------------------------
@router.get("/broadcast")
async def broadcast_page(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    audiences = [
        {"key": key, "label": label, "count": await broadcasts.preview_count(session, key)}
        for key, label in AUDIENCES.items()
    ]
    rows = await broadcasts.list_recent(session, limit=25)
    reach = audiences[0]["count"] if audiences else 0

    return render(
        request,
        "broadcast.html",
        {
            "page_title": "ارسال همگانی",
            "page_subtitle": (f"{fa_digits(reach)} کاربر قابل پیام‌رسانی · {fa_digits(len(rows))} کارزار اخیر"),
            "audiences": audiences,
            "rows": rows,
            "base_url": BASE,
            # قالب برای دکمه‌های شروع/لغو وضعیت زنده‌ی تسک‌ها را می‌پرسد.
            "broadcasts": broadcasts,
            "audience_labels": AUDIENCES,
            "media_types": MEDIA_TYPES,
        },
    )


# ---------------------------------------------------------------------------
# ساخت کارزار
# ---------------------------------------------------------------------------
@router.post("/broadcast")
async def create_broadcast(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        text = form_str(form, "text")
        if not text:
            raise ValidationError("متن پیام نمی‌تواند خالی باشد.")

        audience = form_str(form, "audience", "all")
        if audience not in AUDIENCES:
            raise ValidationError("گروه مخاطبان نامعتبر است.")

        media = _media(form)
        scheduled_at = _scheduled_at(form)

        broadcast = await broadcasts.create(
            session,
            staff,
            text=text,
            audience=audience,
            media=media,
            start_immediately=False,
        )
        broadcast_id = broadcast.id
        total = broadcast.total
        label = _audience_label(audience)

        if scheduled_at is not None:
            # ارسال زمان‌بندی‌شده در وضعیت پیش‌نویس می‌ماند تا زمانش برسد.
            broadcast.scheduled_at = scheduled_at
            await session.commit()
            return redirect(
                BASE,
                message=(
                    f"ارسال همگانی برای «{label}» با {fa_digits(total)} مخاطب "
                    f"برای {jalali_datetime(scheduled_at)} زمان‌بندی شد."
                ),
            )

        # بدون زمان‌بندی: اول ذخیره، بعد سپردن به سرویس (تسک پس‌زمینه سشن خودش را می‌سازد).
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level=ERROR_LEVEL)

    started = await broadcasts.start(broadcast_id)
    if started:
        message = f"ارسال به «{label}» آغاز شد: {fa_digits(total)} مخاطب."
    else:
        message = f"ارسال به «{label}» ثبت شد و به‌زودی آغاز می‌شود."
    return redirect(BASE, message=message)


@router.post("/broadcast/{broadcast_id}/start")
async def start_broadcast(
    broadcast_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        broadcast = await broadcasts.get(session, broadcast_id)
        label = _audience_label(broadcast.audience)
        total = broadcast.total
    except AppError as exc:
        return redirect(BASE, message=exc.message, level=ERROR_LEVEL)

    if not await broadcasts.start(broadcast_id):
        return redirect(BASE, message="این ارسال همین حالا در حال اجراست.", level="info")

    return redirect(BASE, message=f"ارسال به «{label}» با {fa_digits(total)} مخاطب شروع شد.")


@router.post("/broadcast/{broadcast_id}/cancel")
async def cancel_broadcast(
    broadcast_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        broadcast = await broadcasts.get(session, broadcast_id)
        label = _audience_label(broadcast.audience)

        if broadcast.status == BroadcastStatus.DRAFT:
            # کارزار زمان‌بندی‌شده هنوز تسکی ندارد؛ همان‌جا بسته می‌شود.
            broadcast.status = BroadcastStatus.CANCELED
            broadcast.scheduled_at = None
            broadcast.finished_at = now_utc()
            await session.commit()

        await broadcasts.cancel(broadcast_id)
    except AppError as exc:
        return redirect(BASE, message=exc.message, level=ERROR_LEVEL)

    return redirect(BASE, message=f"ارسال همگانی «{label}» لغو شد.")


__all__ = ["router"]
