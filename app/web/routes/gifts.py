"""کدهای هدیه — شارژ کیف پول، سرویس رایگان یا درصد جایزه (GiftCode)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError, NotFoundError, ValidationError
from app.core.jalali import now_utc, to_utc
from app.core.money import fa_digits
from app.db.models import GiftCode, Plan, Staff
from app.services.gifts import KIND_LABELS, gifts
from app.web.deps import form_bool, form_dict, form_int, form_money, form_str
from app.web.security import get_db_session, require_manager, verify_csrf
from app.web.templating import redirect, render

router = APIRouter(tags=["gifts"])

BASE = f"{settings.panel_prefix}/gifts"

KINDS: tuple[tuple[str, str], ...] = (
    ("wallet", KIND_LABELS["wallet"]),
    ("plan", KIND_LABELS["plan"]),
    ("percent", KIND_LABELS["percent"]),
)

BULK_MIN, BULK_MAX = 1, 200
#: کدهایی که کمتر از این مدت از ساختشان گذشته باشد «جدید» علامت می‌خورند.
FRESH_MINUTES = 5


# ---------------------------------------------------------------------------
# کمک‌کننده‌ها
# ---------------------------------------------------------------------------
def _optional_int(form: dict[str, Any], key: str) -> int | None:
    return None if form_str(form, key) == "" else form_int(form, key, 0)


def _expiry(form: dict[str, Any]) -> datetime | None:
    raw = form_str(form, "expires_at")
    if not raw:
        return None
    try:
        return to_utc(datetime.fromisoformat(raw))
    except ValueError as exc:
        raise ValidationError("تاریخ و ساعت انقضا معتبر نیست.") from exc


def _payload(form: dict[str, Any], *, generate_code: bool, plan_ids: set[int] | None = None) -> dict[str, Any]:
    kind = form_str(form, "kind", "wallet")
    if kind not in KIND_LABELS:
        raise ValidationError("نوع کد هدیه نامعتبر است.")

    code = form_str(form, "code").upper().replace(" ", "")
    if code and not code.startswith("GIFT-"):
        code = code[:32]
    if not code and not generate_code:
        raise ValidationError("کد هدیه نمی‌تواند خالی باشد.")

    if kind == "wallet":
        value = form_money(form, "value", 0)
        if value <= 0:
            raise ValidationError("مبلغ شارژ کیف پول باید بزرگ‌تر از صفر باشد.")
    elif kind == "percent":
        value = min(max(form_int(form, "value", 0), 0), 100)
        if value <= 0:
            raise ValidationError("درصد جایزه باید بین ۱ تا ۱۰۰ باشد.")
    else:
        value = form_int(form, "plan_id", 0)
        if value <= 0:
            raise ValidationError("پلن هدیه را انتخاب کنید.")
        if plan_ids is not None and value not in plan_ids:
            raise ValidationError("پلن انتخاب‌شده در فهرست پلن‌های فعال نیست.")

    return {
        "code": code or None,
        "kind": kind,
        "value": value,
        "max_uses": _optional_int(form, "max_uses"),
        "per_user_limit": max(form_int(form, "per_user_limit", 1), 1),
        "expires_at": _expiry(form),
        "note": form_str(form, "note") or None,
    }


async def _get(session: AsyncSession, gift_id: int) -> GiftCode:
    row = await session.get(GiftCode, gift_id)
    if row is None:
        raise NotFoundError("کد هدیه مورد نظر پیدا نشد.")
    return row


async def _plans_for_select(session: AsyncSession) -> list[Plan]:
    return list(
        (
            await session.execute(
                select(Plan)
                .where(Plan.is_active.is_(True), Plan.is_test.is_(False))
                .order_by(Plan.sort_order.asc(), Plan.id.asc())
            )
        ).scalars()
    )


# ---------------------------------------------------------------------------
# نمایش
# ---------------------------------------------------------------------------
@router.get("/gifts")
async def list_gifts(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    rows = await gifts.list_all(session, limit=200)
    plans = await _plans_for_select(session)
    cutoff = now_utc() - timedelta(minutes=FRESH_MINUTES)
    fresh = [row for row in rows if row.created_at is not None and (to_utc(row.created_at) or cutoff) >= cutoff]
    return render(
        request,
        "gifts.html",
        {
            "page_title": "کدهای هدیه",
            "page_subtitle": f"{fa_digits(len(rows))} کد ثبت شده",
            "rows": rows,
            "kinds": KINDS,
            "kind_labels": KIND_LABELS,
            "plans": plans,
            "plan_names": {plan.id: plan.name for plan in plans},
            "fresh": fresh,
            "bulk_min": BULK_MIN,
            "bulk_max": BULK_MAX,
            "fresh_minutes": FRESH_MINUTES,
            "base_url": BASE,
        },
    )


# ---------------------------------------------------------------------------
# نوشتن
# ---------------------------------------------------------------------------
@router.post("/gifts")
async def create_gift(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        plans = await _plans_for_select(session)
        data = _payload(form, generate_code=True, plan_ids={plan.id for plan in plans})
        is_active = form_bool(form, "is_active")
        row = await gifts.create(session, **{k: v for k, v in data.items() if k != "code"}, code=data["code"])
        if not is_active:
            await gifts.toggle(session, row.id, False)
        code = row.code
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    state = "" if is_active else " (غیرفعال)"
    return redirect(BASE, message=f"کد هدیه «{code}» ساخته شد{state}.")


@router.post("/gifts/bulk")
async def bulk_create_gifts(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    """تولید گروهی کد برای مسابقه‌ها و کمپین‌ها."""
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        plans = await _plans_for_select(session)
        data = _payload(form, generate_code=True, plan_ids={plan.id for plan in plans})
        count = min(max(form_int(form, "count", BULK_MIN), BULK_MIN), BULK_MAX)
        is_active = form_bool(form, "is_active")
        created = await gifts.bulk_create(
            session,
            count=count,
            kind=data["kind"],
            value=data["value"],
            max_uses=data["max_uses"],
            per_user_limit=data["per_user_limit"],
            expires_at=data["expires_at"],
            note=data["note"],
        )
        if not is_active:
            for row in created:
                await gifts.toggle(session, row.id, False)
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    return redirect(
        BASE,
        message=(
            f"{fa_digits(len(created))} کد هدیه ساخته شد؛ "
            "کدهای تازه در کارت «کدهای تازه‌ساخته‌شده» و بالای فهرست آمده‌اند."
        ),
    )


@router.post("/gifts/{gift_id}")
async def update_gift(
    gift_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        plans = await _plans_for_select(session)
        data = _payload(form, generate_code=False, plan_ids={plan.id for plan in plans})
        row = await _get(session, gift_id)
        code = data.pop("code") or row.code
        if code != row.code and await gifts.find(session, code) is not None:
            raise ValidationError("این کد هدیه قبلاً ساخته شده است.")
        row.code = code
        for key, value in data.items():
            setattr(row, key, value)
        row.is_active = form_bool(form, "is_active")
        await session.flush()
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    return redirect(BASE, message=f"کد هدیه «{code}» ذخیره شد.")


@router.post("/gifts/{gift_id}/toggle")
async def toggle_gift(
    gift_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        row = await _get(session, gift_id)
        row = await gifts.toggle(session, gift_id, not row.is_active)
        active, code = row.is_active, row.code
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    state = "فعال" if active else "غیرفعال"
    return redirect(BASE, message=f"کد هدیه «{code}» {state} شد.")


@router.post("/gifts/{gift_id}/delete")
async def delete_gift(
    gift_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        row = await _get(session, gift_id)
        code = row.code
        await gifts.delete(session, gift_id)
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    return redirect(BASE, message=f"کد هدیه «{code}» حذف شد.")


__all__ = ["router"]
