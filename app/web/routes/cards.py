"""کارت‌های بانکی — مقصدهای کارت‌به‌کارت (CardAccount)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError, NotFoundError, ValidationError
from app.core.money import en_digits, fa_digits
from app.db.models import CardAccount, Staff
from app.web.deps import form_bool, form_dict, form_int, form_str
from app.web.security import get_db_session, require_manager, verify_csrf
from app.web.templating import redirect, render

router = APIRouter(tags=["cards"])

BASE = f"{settings.panel_prefix}/cards"

CARD_DIGITS = 16
IBAN_DIGITS = 24


# ---------------------------------------------------------------------------
# کمک‌کننده‌ها
# ---------------------------------------------------------------------------
def _digits(raw: str) -> str:
    """ارقام فارسی/عربی و خط تیره را به رقم لاتین تبدیل می‌کند."""
    return "".join(ch for ch in en_digits(raw) if ch.isdigit())


def _normalise_card(raw: str) -> str:
    number = _digits(raw)
    if len(number) != CARD_DIGITS:
        raise ValidationError(
            f"شماره کارت باید {fa_digits(CARD_DIGITS)} رقم باشد (ارقام فارسی و خط تیره هم پذیرفته می‌شود)."
        )
    return number


def _normalise_iban(raw: str) -> str | None:
    if not raw:
        return None
    iban = en_digits(raw).upper().replace(" ", "").replace("-", "")
    if not iban.startswith("IR"):
        iban = f"IR{iban}"
    body = iban[2:]
    if len(body) != IBAN_DIGITS or not body.isdigit():
        raise ValidationError(f"شبا باید با IR و {fa_digits(IBAN_DIGITS)} رقم باشد.")
    return iban


def _payload(form: dict[str, Any]) -> dict[str, Any]:
    holder = form_str(form, "holder_name")
    if not holder:
        raise ValidationError("نام صاحب کارت نمی‌تواند خالی باشد.")
    return {
        "card_number": _normalise_card(form_str(form, "card_number")),
        "holder_name": holder[:128],
        "bank_name": form_str(form, "bank_name") or None,
        "iban": _normalise_iban(form_str(form, "iban")),
        "instructions": form_str(form, "instructions") or None,
        "sort_order": form_int(form, "sort_order", 0),
        "is_active": form_bool(form, "is_active"),
    }


async def _get(session: AsyncSession, card_id: int) -> CardAccount:
    card = await session.get(CardAccount, card_id)
    if card is None:
        raise NotFoundError("کارت بانکی مورد نظر پیدا نشد.")
    return card


# ---------------------------------------------------------------------------
# نمایش
# ---------------------------------------------------------------------------
@router.get("/cards")
async def list_cards(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    rows = list(
        (
            await session.execute(select(CardAccount).order_by(CardAccount.sort_order.asc(), CardAccount.id.asc()))
        ).scalars()
    )
    return render(
        request,
        "cards.html",
        {
            "page_title": "کارت‌های بانکی",
            "page_subtitle": f"{fa_digits(len(rows))} کارت ثبت شده",
            "rows": rows,
            "active_count": sum(1 for row in rows if row.is_active),
            "base_url": BASE,
        },
    )


# ---------------------------------------------------------------------------
# نوشتن
# ---------------------------------------------------------------------------
@router.post("/cards")
async def create_card(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        data = _payload(form)
        card = CardAccount(**data)
        session.add(card)
        await session.flush()
        holder = card.holder_name
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    return redirect(BASE, message=f"کارت «{holder}» ثبت شد.")


@router.post("/cards/{card_id}")
async def update_card(
    card_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        card = await _get(session, card_id)
        for key, value in _payload(form).items():
            setattr(card, key, value)
        await session.flush()
        holder = card.holder_name
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    return redirect(BASE, message=f"کارت «{holder}» ذخیره شد.")


@router.post("/cards/{card_id}/toggle")
async def toggle_card(
    card_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        card = await _get(session, card_id)
        card.is_active = not card.is_active
        active, label = card.is_active, card.masked
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    state = "فعال" if active else "غیرفعال"
    return redirect(BASE, message=f"کارت {label} {state} شد.")


@router.post("/cards/{card_id}/delete")
async def delete_card(
    card_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        card = await _get(session, card_id)
        label = card.masked
        holder = card.holder_name
        await session.delete(card)
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    return redirect(BASE, message=f"کارت {label} ({holder}) حذف شد.")


__all__ = ["router"]
