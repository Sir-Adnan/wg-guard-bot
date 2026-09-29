"""کارکنان و دسترسی — مدیریت اپراتورها و حساب‌های پنل (فقط مالک).

سه نقش وجود دارد: مالک (همه‌چیز)، مدیر (همه‌چیز جز کارکنان) و پشتیبان
(رسیدها، تیکت‌ها و مشاهده‌ی کاتالوگ).  این ماژول علاوه بر اعتبارسنجی فرم،
قواعد ایمنی را هم نگه می‌دارد: سیستم هرگز بدون مالک فعال نمی‌ماند و هیچ‌کس
نمی‌تواند حساب خودش را غیرفعال یا حذف کند.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError, NotFoundError, ValidationError
from app.core.money import en_digits, fa_digits
from app.core.security import hash_password, password_strength_error
from app.db.models import Staff, StaffRole
from app.services.audit import audit
from app.web.deps import form_bool, form_dict, form_str
from app.web.security import get_db_session, require_owner, verify_csrf
from app.web.templating import redirect, render

router = APIRouter(tags=["staff"])

BASE = f"{settings.panel_prefix}/staff"

#: نقش‌ها به ترتیب قدرت، برای نمایش در فرم و نشان‌ها.
ROLES: tuple[tuple[str, str], ...] = (
    ("owner", "مالک"),
    ("admin", "مدیر"),
    ("support", "پشتیبان"),
)

ROLE_LABELS = dict(ROLES)

#: ترتیب نمایش جدول: مالک‌ها بالا، سپس مدیر و پشتیبان.
_ROLE_ORDER = {StaffRole.OWNER: 0, StaffRole.ADMIN: 1, StaffRole.SUPPORT: 2}

#: سطح پیام‌های خطا، مطابق قرارداد پنل.
ERROR_LEVEL = "error"


# ---------------------------------------------------------------------------
# کمک‌کننده‌ها
# ---------------------------------------------------------------------------
def _role(form: dict[str, Any]) -> StaffRole:
    raw = form_str(form, "role", "support")
    try:
        return StaffRole(raw)
    except ValueError as exc:
        raise ValidationError("نقش انتخاب‌شده معتبر نیست.") from exc


def _telegram_id(form: dict[str, Any]) -> int | None:
    """شناسه‌ی عددی تلگرام؛ خالی یعنی اپراتور فقط از پنل استفاده می‌کند."""
    raw = en_digits(form_str(form, "telegram_id")).replace(" ", "")
    if not raw:
        return None
    if not raw.isdigit():
        raise ValidationError("شناسه تلگرام باید فقط عدد باشد؛ مثلاً ۱۲۳۴۵۶۷۸۹.")
    return int(raw)


async def _get(session: AsyncSession, staff_id: int) -> Staff:
    row = await session.get(Staff, staff_id)
    if row is None:
        raise NotFoundError("کارمند مورد نظر پیدا نشد.")
    return row


async def _active_owner_count(session: AsyncSession, *, exclude_id: int | None = None) -> int:
    stmt = select(func.count(Staff.id)).where(Staff.role == StaffRole.OWNER, Staff.is_active.is_(True))
    if exclude_id is not None:
        stmt = stmt.where(Staff.id != exclude_id)
    return int(await session.scalar(stmt) or 0)


async def _ensure_owner_survives(session: AsyncSession, row: Staff, *, role: StaffRole, is_active: bool) -> None:
    """آخرین مالک فعال را نمی‌توان غیرفعال، تنزل یا حذف کرد."""
    if not (row.is_owner and row.is_active):
        return
    if role == StaffRole.OWNER and is_active:
        return
    if await _active_owner_count(session, exclude_id=row.id) == 0:
        raise ValidationError(
            "این تنها مالک فعال سیستم است؛ تا وقتی مالک فعال دیگری نساخته‌اید "
            "نمی‌توانید او را غیرفعال کنید، نقشش را پایین بیاورید یا حذفش کنید."
        )


async def _check_unique(
    session: AsyncSession,
    *,
    login: str | None,
    telegram_id: int | None,
    exclude_id: int | None = None,
) -> None:
    """یکتایی نام کاربری و شناسه تلگرام را پیش از نوشتن بررسی می‌کند."""
    if login:
        stmt = select(Staff.id).where(func.lower(Staff.login) == login.lower())
        if exclude_id is not None:
            stmt = stmt.where(Staff.id != exclude_id)
        if await session.scalar(stmt) is not None:
            raise ValidationError(f"نام کاربری «{login}» قبلاً برای کارمند دیگری ثبت شده است.")

    if telegram_id is not None:
        stmt = select(Staff.id).where(Staff.telegram_id == telegram_id)
        if exclude_id is not None:
            stmt = stmt.where(Staff.id != exclude_id)
        if await session.scalar(stmt) is not None:
            raise ValidationError(f"شناسه تلگرام {fa_digits(telegram_id)} قبلاً برای کارمند دیگری ثبت شده است.")


async def _collect(
    session: AsyncSession,
    form: dict[str, Any],
    *,
    actor: Staff,
    row: Staff | None,
) -> dict[str, Any]:
    """فیلدهای فرم را به مقادیر ستون‌های ``Staff`` تبدیل و قواعد ایمنی را بررسی می‌کند.

    ``row=None`` یعنی ساخت کارمند تازه؛ در حالت ویرایش، رمز خالی رمز فعلی را
    دست‌نخورده می‌گذارد.
    """
    name = form_str(form, "name")
    if not name:
        raise ValidationError("نام کارمند نمی‌تواند خالی باشد.")

    role = _role(form)
    login = form_str(form, "login") or None
    password = form_str(form, "password")
    telegram_id = _telegram_id(form)
    is_active = form_bool(form, "is_active")

    await _check_unique(session, login=login, telegram_id=telegram_id, exclude_id=row.id if row else None)

    password_hash = row.password_hash if row is not None else None

    if row is None and login and not password:
        raise ValidationError("برای ساخت دسترسی پنل، رمز عبور را هم وارد کنید.")

    if password:
        weak = password_strength_error(password)
        if weak:
            raise ValidationError(weak)
        password_hash = hash_password(password)
    elif not login:
        # بدون نام کاربری پنل، گذرواژه هم بی‌معناست.
        password_hash = None

    if row is not None:
        if row.id == actor.id and not is_active:
            raise ValidationError("نمی‌توانید حساب خودتان را غیرفعال کنید.")
        await _ensure_owner_survives(session, row, role=role, is_active=is_active)

    return {
        "name": name[:128],
        "telegram_id": telegram_id,
        "role": role,
        "login": login[:64] if login else None,
        "password_hash": password_hash,
        "receive_receipts": form_bool(form, "receive_receipts"),
        "is_active": is_active,
        "note": form_str(form, "note") or None,
    }


def _describe(row: Staff) -> str:
    """خلاصه‌ی خوانا برای گزارش رویداد."""
    label = ROLE_LABELS.get(row.role.value, row.role.value)
    login = row.login or "بدون دسترسی پنل"
    return f"role={row.role.value} ({label}) login={login} active={row.is_active}"


# ---------------------------------------------------------------------------
# نمایش
# ---------------------------------------------------------------------------
@router.get("/staff")
async def list_staff(
    request: Request,
    staff: Staff = Depends(require_owner()),
    session: AsyncSession = Depends(get_db_session),
):
    rows = list((await session.execute(select(Staff))).scalars())
    rows.sort(key=lambda row: (_ROLE_ORDER.get(row.role, 9), not row.is_active, row.name or ""))

    return render(
        request,
        "staff.html",
        {
            "page_title": "کارکنان و دسترسی",
            "page_subtitle": (
                f"{fa_digits(len(rows))} اپراتور · {fa_digits(sum(1 for row in rows if row.is_active))} فعال"
            ),
            "rows": rows,
            "base_url": BASE,
            "roles": ROLES,
            "role_labels": ROLE_LABELS,
            "active_owners": sum(1 for row in rows if row.role == StaffRole.OWNER and row.is_active),
        },
    )


# ---------------------------------------------------------------------------
# ساخت / ویرایش
# ---------------------------------------------------------------------------
@router.post("/staff")
async def create_staff(
    request: Request,
    staff: Staff = Depends(require_owner()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        data = await _collect(session, form, actor=staff, row=None)
        row = Staff(**data)
        session.add(row)
        await session.flush()
        name = row.name or row.login or "کارمند"
        await audit.record(
            session,
            "staff.create",
            actor=staff,
            entity="staff",
            entity_id=row.id,
            description=_describe(row),
        )
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level=ERROR_LEVEL)
    except IntegrityError:
        await session.rollback()
        return redirect(
            BASE,
            message="نام کاربری پنل یا شناسه تلگرام تکراری است؛ مقدار دیگری انتخاب کنید.",
            level=ERROR_LEVEL,
        )

    return redirect(BASE, message=f"کارمند «{name}» ساخته شد.")


@router.post("/staff/{staff_id}")
async def update_staff(
    staff_id: int,
    request: Request,
    staff: Staff = Depends(require_owner()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        row = await _get(session, staff_id)
        data = await _collect(session, form, actor=staff, row=row)
        for key, value in data.items():
            setattr(row, key, value)
        await session.flush()
        name = row.name or row.login or "کارمند"
        await audit.record(
            session,
            "staff.update",
            actor=staff,
            entity="staff",
            entity_id=row.id,
            description=_describe(row),
        )
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level=ERROR_LEVEL)
    except IntegrityError:
        await session.rollback()
        return redirect(
            BASE,
            message="نام کاربری پنل یا شناسه تلگرام تکراری است؛ مقدار دیگری انتخاب کنید.",
            level=ERROR_LEVEL,
        )

    return redirect(BASE, message=f"تغییرات «{name}» ذخیره شد.")


@router.post("/staff/{staff_id}/password")
async def set_staff_password(
    staff_id: int,
    request: Request,
    staff: Staff = Depends(require_owner()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    password = form_str(form, "password")
    confirm = form_str(form, "password_confirm")

    try:
        row = await _get(session, staff_id)
        if not row.login:
            raise ValidationError("این کارمند دسترسی به پنل ندارد؛ ابتدا در فرم ویرایش نام کاربری پنل را تعیین کنید.")
        if password != confirm:
            raise ValidationError("رمز عبور و تکرار آن یکسان نیستند.")
        weak = password_strength_error(password)
        if weak:
            raise ValidationError(weak)

        row.password_hash = hash_password(password)
        await session.flush()
        name = row.name or row.login
        await audit.record(
            session,
            "staff.update",
            actor=staff,
            entity="staff",
            entity_id=row.id,
            description=f"password reset for {row.login}",
        )
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level=ERROR_LEVEL)

    return redirect(BASE, message=f"رمز عبور «{name}» تغییر کرد.")


@router.post("/staff/{staff_id}/toggle")
async def toggle_staff(
    staff_id: int,
    request: Request,
    staff: Staff = Depends(require_owner()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        row = await _get(session, staff_id)
        if row.is_active and row.id == staff.id:
            raise ValidationError("نمی‌توانید حساب خودتان را غیرفعال کنید.")
        if row.is_active:
            await _ensure_owner_survives(session, row, role=row.role, is_active=False)

        row.is_active = not row.is_active
        await session.flush()
        name = row.name or row.login or "کارمند"
        active = row.is_active
        await audit.record(
            session,
            "staff.update",
            actor=staff,
            entity="staff",
            entity_id=row.id,
            description=_describe(row),
        )
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level=ERROR_LEVEL)

    state = "فعال" if active else "غیرفعال"
    return redirect(BASE, message=f"دسترسی «{name}» {state} شد.")


@router.post("/staff/{staff_id}/delete")
async def delete_staff(
    staff_id: int,
    request: Request,
    staff: Staff = Depends(require_owner()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        row = await _get(session, staff_id)
        if row.id == staff.id:
            raise ValidationError("نمی‌توانید حساب خودتان را حذف کنید.")
        # حذف هم مثل غیرفعال‌سازی، سیستم را بی‌مالک نمی‌گذارد.
        await _ensure_owner_survives(session, row, role=StaffRole.ADMIN, is_active=False)

        name = row.name or row.login or "کارمند"
        await session.delete(row)
        await audit.record(
            session,
            "staff.delete",
            actor=staff,
            entity="staff",
            entity_id=staff_id,
            description=f"deleted staff {name}",
        )
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level=ERROR_LEVEL)

    return redirect(BASE, message=f"کارمند «{name}» حذف شد.")


__all__ = ["router"]
