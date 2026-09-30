"""کانال‌ها — کانال‌ها و گروه‌های عضویت اجباری (Channel)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError, NotFoundError, ValidationError
from app.core.money import fa_digits
from app.db.models import Channel, Staff
from app.services.notifications import notifier
from app.services.ordering import next_sort_order
from app.services.settings_store import app_settings
from app.web.deps import form_bool, form_dict, form_int, form_str
from app.web.security import get_db_session, require_manager, verify_csrf
from app.web.templating import redirect, render

router = APIRouter(tags=["channels"])

BASE = f"{settings.panel_prefix}/channels"

KINDS: tuple[tuple[str, str], ...] = (
    ("channel", "کانال"),
    ("group", "گروه"),
)


# ---------------------------------------------------------------------------
# کمک‌کننده‌ها
# ---------------------------------------------------------------------------
def _submitted_sort_order(form: dict[str, Any]) -> int | None:
    """``sort_order`` only when the form carried one; dragging owns it otherwise."""
    raw = form.get("sort_order")
    if raw is None or form_str(form, "sort_order") == "":
        return None
    return form_int(form, "sort_order", 0)


async def _payload(session: AsyncSession, form: dict[str, Any], *, creating: bool) -> dict[str, Any]:
    chat_id = form_str(form, "chat_id")
    if not chat_id:
        raise ValidationError("شناسه کانال نمی‌تواند خالی باشد.")
    if not (chat_id.startswith("@") or chat_id.lstrip("-").isdigit()):
        raise ValidationError("شناسه کانال باید با @ شروع شود یا یک شناسه عددی مثل ‎-1001234567890 باشد.")

    kind = form_str(form, "kind", "channel")
    if kind not in {key for key, _label in KINDS}:
        kind = KINDS[0][0]

    invite_link = form_str(form, "invite_link")
    if invite_link and not invite_link.startswith(("https://", "http://", "tg://")):
        raise ValidationError("لینک دعوت باید با https:// یا tg:// شروع شود.")

    data: dict[str, Any] = {
        "chat_id": chat_id[:64],
        "title": form_str(form, "title")[:128],
        "invite_link": invite_link or None,
        "kind": kind,
        "auto_approve_joins": form_bool(form, "auto_approve_joins"),
        "show_in_menu": form_bool(form, "show_in_menu"),
        "is_active": form_bool(form, "is_active"),
        "note": form_str(form, "note") or None,
    }

    submitted = _submitted_sort_order(form)
    if submitted is not None:
        data["sort_order"] = submitted
    elif creating:
        data["sort_order"] = await next_sort_order(session, "channels")
    return data


async def _get(session: AsyncSession, channel_id: int) -> Channel:
    channel = await session.get(Channel, channel_id)
    if channel is None:
        raise NotFoundError("کانال مورد نظر پیدا نشد.")
    return channel


def _label(channel: Channel) -> str:
    return channel.title or channel.chat_id


# ---------------------------------------------------------------------------
# نمایش
# ---------------------------------------------------------------------------
@router.get("/channels")
async def list_channels(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    rows = list((await session.execute(select(Channel).order_by(Channel.sort_order.asc(), Channel.id.asc()))).scalars())
    await app_settings.load(session)
    return render(
        request,
        "channels.html",
        {
            "page_title": "کانال‌ها",
            "page_subtitle": f"{fa_digits(len(rows))} کانال و گروه",
            "rows": rows,
            "kinds": KINDS,
            "membership_enabled": app_settings.get_bool("membership.enabled", False),
            "bot_bound": notifier.bound,
            "base_url": BASE,
        },
    )


# ---------------------------------------------------------------------------
# نوشتن
# ---------------------------------------------------------------------------
@router.post("/channels")
async def create_channel(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        data = await _payload(session, form, creating=True)
        channel = Channel(**data)
        session.add(channel)
        await session.flush()
        label = _label(channel)
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    return redirect(BASE, message=f"کانال «{label}» ثبت شد.")


@router.post("/channels/{channel_id}")
async def update_channel(
    channel_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        channel = await _get(session, channel_id)
        for key, value in (await _payload(session, form, creating=False)).items():
            setattr(channel, key, value)
        await session.flush()
        label = _label(channel)
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    return redirect(BASE, message=f"کانال «{label}» ذخیره شد.")


@router.post("/channels/{channel_id}/toggle")
async def toggle_channel(
    channel_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        channel = await _get(session, channel_id)
        channel.is_active = not channel.is_active
        active, label = channel.is_active, _label(channel)
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    state = "فعال" if active else "غیرفعال"
    return redirect(BASE, message=f"کانال «{label}» {state} شد.")


@router.post("/channels/{channel_id}/check")
async def check_channel(
    channel_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    """یک فراخوانی ``getChat`` برای اطمینان از دسترسی ربات به کانال."""
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        channel = await _get(session, channel_id)
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    if not notifier.bound:
        return redirect(
            BASE,
            message="ربات در این اجرا به تلگرام متصل نیست؛ بررسی اتصال ممکن نشد.",
            level="danger",
        )

    target: str | int = channel.chat_id
    if channel.chat_id.lstrip("-").isdigit():
        target = int(channel.chat_id)

    try:
        chat = await notifier.bot.get_chat(chat_id=target)
    except Exception as exc:
        reason = str(exc).replace("\n", " ")[:140]
        return redirect(
            BASE,
            message=f"بررسی «{channel.chat_id}» ناموفق بود: {reason}",
            level="danger",
        )

    title = (
        getattr(chat, "title", None)
        or getattr(chat, "full_name", None)
        or getattr(chat, "username", None)
        or str(target)
    )
    if not channel.title:
        # عنوان خالی را از خود تلگرام پر می‌کنیم تا فهرست خوانا شود.
        channel.title = str(title)[:128]
        await session.commit()

    return redirect(BASE, message=f"ارتباط با «{title}» برقرار است.")


@router.post("/channels/{channel_id}/delete")
async def delete_channel(
    channel_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        channel = await _get(session, channel_id)
        label = _label(channel)
        await session.delete(channel)
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    return redirect(BASE, message=f"کانال «{label}» حذف شد.")


__all__ = ["router"]
