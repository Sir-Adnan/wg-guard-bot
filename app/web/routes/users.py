"""کاربران — فهرست مشتریان، کیف پول، یادداشت و پیام مستقیم (User).

کیف پول فقط از راه :mod:`app.services.users` تغییر می‌کند: هر افزایش یا کاهش
یک ردیف :class:`~app.db.models.Payment` هم می‌نویسد، پس دفتر کل و موجودی
هرگز از هم جدا نمی‌افتند.  ورودی مبلغ در فرم‌ها **تومان** است و پیش از
رسیدن به سرویس با :func:`~app.web.deps.form_money` به ریال تبدیل می‌شود.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.core.money import fa_digits, to_toman
from app.db.models import PaymentKind, PaymentMethod, Staff, User
from app.services.audit import audit
from app.services.notifications import notifier
from app.services.texts import html_escape
from app.services.users import user_service
from app.web.deps import (
    Page,
    form_dict,
    form_money,
    form_str,
    pagination,
)
from app.web.routes.orders import EXPORT_LIMIT, _order_counts, users_csv
from app.web.security import get_db_session, require_any, require_manager, verify_csrf
from app.web.templating import redirect, render

router = APIRouter(tags=["users"])

BASE = f"{settings.panel_prefix}/users"

#: گزینه‌های کشویی وضعیت مسدودی (``None`` یعنی همه).
BLOCKED_CHOICES: tuple[tuple[str, str], ...] = (
    ("", "همه"),
    ("1", "مسدود"),
    ("0", "فعال"),
)


# ---------------------------------------------------------------------------
# کمک‌کننده‌ها
# ---------------------------------------------------------------------------
def _blocked_value(raw: str) -> bool | None:
    """``1`` مسدود، ``0`` فعال و خالی یعنی همه."""
    text = (raw or "").strip()
    if text == "1":
        return True
    if text == "0":
        return False
    return None


async def _blocked_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count(User.id)).where(User.is_blocked.is_(True))) or 0)


async def _wallet_total(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.coalesce(func.sum(User.balance_rial), 0))) or 0)


def _toman_text(rial: int) -> str:
    """مبلغ ریالی را به «۱۲۳,۴۵۶ تومان» تبدیل می‌کند."""
    return f"{fa_digits(f'{to_toman(int(rial or 0)):,}')} تومان"


# ---------------------------------------------------------------------------
# نمایش
# ---------------------------------------------------------------------------
@router.get("/users")
async def list_users(
    request: Request,
    q: str = Query("", description="جست‌وجو در نام، نام کاربری یا شناسه‌ی تلگرام"),
    blocked: str = Query("", description="فیلتر وضعیت مسدودی"),
    page: Page = Depends(pagination),
    staff: Staff = Depends(require_any()),
    session: AsyncSession = Depends(get_db_session),
):
    blocked_filter = _blocked_value(blocked)
    page.search = (q or "").strip()

    try:
        # نام پارامتر جست‌وجو در سرویس ``query`` است (نه ``q``).
        rows, total = await user_service.search(
            session,
            query=page.search,
            limit=page.size,
            offset=page.offset,
            blocked=blocked_filter,
        )
        totals = {
            "all": await user_service.count(session),
            "blocked": await _blocked_count(session),
            "wallet_rial": await _wallet_total(session),
        }
        # آمار هر کاربر برای مودال پروفایل؛ تعداد ردیف‌ها به اندازه‌ی یک صفحه است.
        user_stats = {int(user.id): await user_service.stats(session, user) for user in rows}
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="error")

    page.total = total
    extra = f"&blocked={blocked}" if blocked_filter is not None else ""

    return render(
        request,
        "users.html",
        {
            "page_title": "کاربران",
            "page_subtitle": f"{fa_digits(total)} کاربر با فیلترهای فعلی",
            "rows": rows,
            "page": page,
            "totals": totals,
            "user_stats": user_stats,
            "blocked_choices": BLOCKED_CHOICES,
            "selected_blocked": "" if blocked_filter is None else ("1" if blocked_filter else "0"),
            "base_url": BASE,
            "extra": extra,
        },
    )


# ---------------------------------------------------------------------------
# خروجی CSV
# ---------------------------------------------------------------------------
@router.get("/users/export.csv")
async def export_users(
    request: Request,
    q: str = Query(""),
    blocked: str = Query(""),
    staff: Staff = Depends(require_any()),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        # نام پارامتر جست‌وجو در سرویس ``query`` است (نه ``q``).
        rows, _total = await user_service.search(
            session,
            query=(q or "").strip(),
            limit=EXPORT_LIMIT,
            offset=0,
            blocked=_blocked_value(blocked),
        )
        counts = await _order_counts(session, [int(user.id) for user in rows])
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="error")

    return users_csv(rows, counts)


# ---------------------------------------------------------------------------
# مسدودسازی
# ---------------------------------------------------------------------------
@router.post("/users/{user_id}/block")
async def block_user(
    user_id: int,
    request: Request,
    staff: Staff = Depends(require_any()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    reason = form_str(form, "reason") or "مسدودسازی توسط پشتیبانی"

    try:
        user = await user_service.get(session, user_id)
        await user_service.set_blocked(session, user, True, reason)
        await audit.record(
            session,
            "user.block",
            actor=staff,
            user_id=user.id,
            entity="user",
            entity_id=user.id,
            description=reason,
            meta={"reason": reason},
        )
        name = user.display_name
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="error")

    return redirect(BASE, message=f"کاربر «{name}» مسدود شد.")


@router.post("/users/{user_id}/unblock")
async def unblock_user(
    user_id: int,
    request: Request,
    staff: Staff = Depends(require_any()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        user = await user_service.get(session, user_id)
        await user_service.set_blocked(session, user, False)
        await audit.record(
            session,
            "user.unblock",
            actor=staff,
            user_id=user.id,
            entity="user",
            entity_id=user.id,
        )
        name = user.display_name
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="error")

    return redirect(BASE, message=f"مسدودی کاربر «{name}» برداشته شد.")


# ---------------------------------------------------------------------------
# کیف پول
# ---------------------------------------------------------------------------
@router.post("/users/{user_id}/balance")
async def change_balance(
    user_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    # ورودی اپراتور تومان است؛ ``form_money`` آن را به ریال تبدیل می‌کند.
    delta = form_money(form, "amount", 0)
    if delta == 0:
        return redirect(BASE, message="مبلغ وارد‌شده صفر یا نامعتبر است.", level="error")

    try:
        user = await user_service.get(session, user_id)
        if delta > 0:
            await user_service.credit(
                session,
                user,
                delta,
                kind=PaymentKind.ADJUSTMENT,
                method=PaymentMethod.ADMIN,
                description="افزایش موجودی توسط مدیر",
                staff_id=staff.id,
            )
        else:
            await user_service.debit(
                session,
                user,
                abs(delta),
                kind=PaymentKind.ADJUSTMENT,
                method=PaymentMethod.ADMIN,
                description="کاهش موجودی توسط مدیر",
                staff_id=staff.id,
            )
        await audit.record(
            session,
            "user.balance",
            actor=staff,
            user_id=user.id,
            entity="user",
            entity_id=user.id,
            meta={"delta_rial": delta},
        )
        # مبلغ کیف پول پس از تغییر، پیش از پایان تراکنش خوانده می‌شود.
        balance_rial = int(user.balance_rial)
        name = user.display_name
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="error")

    amount = _toman_text(abs(delta))
    verb = "افزایش" if delta > 0 else "کاهش"
    return redirect(
        BASE,
        message=(f"{verb} موجودی «{name}» به اندازه {amount} انجام شد؛ موجودی جدید: {_toman_text(balance_rial)}."),
    )


# ---------------------------------------------------------------------------
# یادداشت و پیام
# ---------------------------------------------------------------------------
@router.post("/users/{user_id}/note")
async def save_note(
    user_id: int,
    request: Request,
    staff: Staff = Depends(require_any()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        user = await user_service.get(session, user_id)
        user.note = form_str(form, "note")[:2000] or None
        await audit.record(
            session,
            "user.note",
            actor=staff,
            user_id=user.id,
            entity="user",
            entity_id=user.id,
        )
        name = user.display_name
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="error")

    return redirect(BASE, message=f"یادداشت کاربر «{name}» ذخیره شد.")


@router.post("/users/{user_id}/message")
async def message_user(
    user_id: int,
    request: Request,
    staff: Staff = Depends(require_any()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    text = form_str(form, "text")
    if not text:
        return redirect(BASE, message="متن پیام نمی‌تواند خالی باشد.", level="error")

    try:
        user = await user_service.get(session, user_id)
        name = user.display_name
        sent = await notifier.to_user(user, f"<b>پیام از پشتیبانی</b>\n\n{html_escape(text)}")
        await audit.record(
            session,
            "user.message",
            actor=staff,
            user_id=user.id,
            entity="user",
            entity_id=user.id,
            description=text[:200],
            meta={"delivered": sent is not None},
        )
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="error")

    if sent is None:
        return redirect(
            BASE,
            message=f"ارسال پیام به «{name}» ناموفق بود؛ احتمالاً ربات را بلاک کرده است.",
            level="error",
        )
    return redirect(BASE, message=f"پیام برای «{name}» ارسال شد.")


# ---------------------------------------------------------------------------
# سرویس تست
# ---------------------------------------------------------------------------
@router.post("/users/{user_id}/reset-test")
async def reset_test(
    user_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        user = await user_service.get(session, user_id)
        user.test_used_at = None
        await audit.record(
            session,
            "user.test_reset",
            actor=staff,
            user_id=user.id,
            entity="user",
            entity_id=user.id,
        )
        name = user.display_name
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="error")

    return redirect(BASE, message=f"مجوز سرویس تست برای «{name}» بازنشانی شد.")


__all__ = ["router", "users_csv"]
