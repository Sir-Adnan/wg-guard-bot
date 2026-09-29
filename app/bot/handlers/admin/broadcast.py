"""Broadcast from the bot (the web panel offers the same feature with previews)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminCB, NavCB
from app.bot.filters import IsAdmin
from app.bot.keyboards import KeyboardBuilder
from app.bot.states import AdminStates
from app.bot.utils import answer_callback, show
from app.core.errors import AppError
from app.core.logging import get_logger
from app.db.models import Staff
from app.services.broadcast import AUDIENCES, broadcasts

log = get_logger(__name__)
router = Router(name="admin_broadcast")


@router.callback_query(AdminCB.filter(F.action == "broadcast"), IsAdmin())
async def start_broadcast(callback: CallbackQuery, session: AsyncSession, staff: Staff, state: FSMContext) -> None:
    await answer_callback(callback)
    await state.set_state(AdminStates.entering_broadcast)
    counts = {key: await broadcasts.preview_count(session, key) for key in AUDIENCES}
    listing = "\n".join(f"• {label}: {counts.get(key, 0)} نفر" for key, label in AUDIENCES.items())
    await show(
        callback,
        "<b>📣 پیام همگانی</b>\n\n"
        "متن پیام را بفرستید. می‌توانید از HTML ساده مثل <code>&lt;b&gt;</code> استفاده کنید.\n\n"
        f"<b>گروه‌های مخاطب:</b>\n{listing}\n\n"
        "<i>برای انصراف /cancel را بزنید.</i>",
    )


@router.message(AdminStates.entering_broadcast, F.text)
async def preview_broadcast(message: Message, session: AsyncSession, staff: Staff, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if len(text) < 3:
        await show(message, "⚠️ متن پیام خیلی کوتاه است.")
        return

    await state.update_data(broadcast_text=text)
    await state.set_state(AdminStates.confirming_broadcast)

    kb = KeyboardBuilder(session=session, columns=2)
    for key, label in AUDIENCES.items():
        count = await broadcasts.preview_count(session, key)
        await kb.add(
            "admin.broadcast",
            text=f"{label} ({count})",
            callback=AdminCB(action="broadcast_send", target_id=hash(key) & 0xFFFF, page=0).pack(),
        )
    kb.row()
    await kb.add("menu.cancel", callback=AdminCB(action="menu").pack())

    await show(
        message,
        f"<b>پیش‌نمایش پیام</b>\n\n{text}\n\n——————————\nگروه مخاطب را انتخاب کنید:",
        keyboard=kb.build(),
    )


@router.callback_query(AdminCB.filter(F.action == "broadcast_audience"), IsAdmin())
async def legacy_audience(callback: CallbackQuery) -> None:
    await answer_callback(callback, "لطفاً از پنل وب برای ارسال همگانی استفاده کنید.", alert=True)


@router.callback_query(AdminCB.filter(F.action == "broadcast_send"), IsAdmin())
async def send_broadcast(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, staff: Staff, state: FSMContext
) -> None:
    data = await state.get_data()
    text = str(data.get("broadcast_text") or "")
    if not text:
        await state.clear()
        await callback.answer("متن پیام پیدا نشد. دوباره تلاش کنید.", show_alert=True)
        return

    keys = list(AUDIENCES.keys())
    index = callback_data.target_id % len(keys)
    audience = keys[index]

    try:
        broadcast = await broadcasts.create(session, staff, text=text, audience=audience, start_immediately=False)
        await session.commit()
    except AppError as exc:
        await state.clear()
        await callback.answer(exc.message[:190], show_alert=True)
        return

    await state.clear()
    await answer_callback(callback, "ارسال آغاز شد…")
    await broadcasts.start(broadcast.id)
    await show(
        callback,
        f"📣 ارسال به <b>{AUDIENCES[audience]}</b> آغاز شد.\n"
        f"تعداد گیرندگان: <b>{broadcast.total}</b>\n\n"
        "گزارش نهایی پس از پایان ارسال ارسال می‌شود.",
        keyboard=await __back_keyboard(session),
    )


async def __back_keyboard(session: AsyncSession):
    kb = KeyboardBuilder(session=session, columns=1)
    await kb.add("menu.main", callback=NavCB(to="main").pack())
    return kb.build()


__all__ = ["router"]
