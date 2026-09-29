"""کدهای تخفیف — ساخت و مدیریت کوپن‌ها (DiscountCode)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError, NotFoundError, ValidationError
from app.core.jalali import to_utc
from app.core.money import fa_digits
from app.core.security import random_code
from app.db.models import DiscountCode, DiscountKind, Staff
from app.services.discounts import discounts
from app.web.deps import form_bool, form_dict, form_int, form_money, form_str
from app.web.security import get_db_session, require_manager, verify_csrf
from app.web.templating import redirect, render

router = APIRouter(tags=["discounts"])

BASE = f"{settings.panel_prefix}/discounts"

KINDS: tuple[tuple[str, str], ...] = (
    ("percent", "درصدی"),
    ("amount", "مبلغ ثابت"),
)

CODE_PREFIX = "OFF"


# ---------------------------------------------------------------------------
# کمک‌کننده‌ها
# ---------------------------------------------------------------------------
def _optional_int(form: dict[str, Any], key: str) -> int | None:
    return None if form_str(form, key) == "" else form_int(form, key, 0)


def _optional_money(form: dict[str, Any], key: str) -> int | None:
    return None if form_str(form, key) == "" else form_money(form, key, 0)


def _expiry(form: dict[str, Any]) -> datetime | None:
    """ورودی ``datetime-local`` مرورگر → زمان آگاه از منطقه UTC."""
    raw = form_str(form, "expires_at")
    if not raw:
        return None
    try:
        return to_utc(datetime.fromisoformat(raw))
    except ValueError as exc:
        raise ValidationError("تاریخ و ساعت انقضا معتبر نیست.") from exc


def _payload(form: dict[str, Any], *, generate_code: bool) -> dict[str, Any]:
    code = form_str(form, "code").upper().replace(" ", "")
    if not code:
        if not generate_code:
            raise ValidationError("کد تخفیف نمی‌تواند خالی باشد.")
        code = f"{CODE_PREFIX}-{random_code(8)}"

    kind = form_str(form, "kind", "percent")
    if kind not in {key for key, _label in KINDS}:
        raise ValidationError("نوع تخفیف نامعتبر است.")

    if kind == "amount":
        value = form_money(form, "value", 0)
        if value <= 0:
            raise ValidationError("مبلغ تخفیف باید بزرگ‌تر از صفر باشد.")
    else:
        value = min(max(form_int(form, "value", 0), 0), 100)
        if value <= 0:
            raise ValidationError("درصد تخفیف باید بین ۱ تا ۱۰۰ باشد.")

    return {
        "code": code[:32],
        "kind": DiscountKind(kind),
        "value": value,
        "max_discount_rial": _optional_money(form, "max_discount_rial"),
        "min_amount_rial": form_money(form, "min_amount_rial", 0),
        "max_uses": _optional_int(form, "max_uses"),
        "per_user_limit": max(form_int(form, "per_user_limit", 1), 1),
        "expires_at": _expiry(form),
        "note": form_str(form, "note") or None,
    }


async def _get(session: AsyncSession, code_id: int) -> DiscountCode:
    row = await session.get(DiscountCode, code_id)
    if row is None:
        raise NotFoundError("کد تخفیف مورد نظر پیدا نشد.")
    return row


# ---------------------------------------------------------------------------
# نمایش
# ---------------------------------------------------------------------------
@router.get("/discounts")
async def list_discounts(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    rows = await discounts.list_all(session, limit=200)
    return render(
        request,
        "discounts.html",
        {
            "page_title": "کدهای تخفیف",
            "page_subtitle": f"{fa_digits(len(rows))} کد ثبت شده",
            "rows": rows,
            "kinds": KINDS,
            "code_prefix": CODE_PREFIX,
            "base_url": BASE,
        },
    )


# ---------------------------------------------------------------------------
# نوشتن
# ---------------------------------------------------------------------------
@router.post("/discounts")
async def create_discount(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        data = _payload(form, generate_code=True)
        is_active = form_bool(form, "is_active")
        row = await discounts.create(session, **data)
        if not is_active:
            await discounts.toggle(session, row.id, False)
        code = row.code
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    state = "" if is_active else " (غیرفعال)"
    return redirect(BASE, message=f"کد تخفیف «{code}» ساخته شد{state}.")


@router.post("/discounts/{code_id}")
async def update_discount(
    code_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        data = _payload(form, generate_code=False)
        row = await _get(session, code_id)
        clash = await discounts.find(session, data["code"])
        if clash is not None and clash.id != code_id:
            raise ValidationError("این کد تخفیف قبلاً ساخته شده است.")
        for key, value in data.items():
            setattr(row, key, value)
        row.is_active = form_bool(form, "is_active")
        await session.flush()
        code = row.code
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    return redirect(BASE, message=f"کد تخفیف «{code}» ذخیره شد.")


@router.post("/discounts/{code_id}/toggle")
async def toggle_discount(
    code_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        row = await _get(session, code_id)
        row = await discounts.toggle(session, code_id, not row.is_active)
        active, code = row.is_active, row.code
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    state = "فعال" if active else "غیرفعال"
    return redirect(BASE, message=f"کد تخفیف «{code}» {state} شد.")


@router.post("/discounts/{code_id}/delete")
async def delete_discount(
    code_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        row = await _get(session, code_id)
        code = row.code
        await discounts.delete(session, code_id)
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    return redirect(BASE, message=f"کد تخفیف «{code}» حذف شد.")


__all__ = ["router"]
