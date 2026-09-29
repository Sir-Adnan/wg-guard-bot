"""Customer lookup and quick actions from the bot."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminCB
from app.bot.filters import IsAdmin, IsStaff
from app.bot.keyboards import KeyboardBuilder
from app.bot.states import AdminStates
from app.bot.utils import answer_callback, show
from app.core.errors import AppError
from app.core.jalali import jalali_datetime
from app.core.logging import get_logger
from app.core.money import format_amount, parse_user_amount
from app.db.models import Staff, User
from app.services.audit import audit
from app.services.notifications import notifier
from app.services.texts import html_escape
from app.services.users import user_service

log = get_logger(__name__)
router = Router(name="admin_users")


@router.callback_query(AdminCB.filter(F.action == "users"), IsStaff())
async def ask_user_query(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_callback(callback)
    await state.set_state(AdminStates.searching_user)
    await show(
        callback,
        "<b>🔍 جست‌وجوی کاربر</b>\n\n"
        "شناسه عددی، نام کاربری یا بخشی از نام را بفرستید.\n"
        "<i>برای انصراف /cancel را بزنید.</i>",
    )


@router.message(AdminStates.searching_user, F.text, IsStaff())
async def search_user(message: Message, session: AsyncSession, staff: Staff, state: FSMContext) -> None:
    query = (message.text or "").strip()
    if query.startswith("/"):
        return
    rows, total = await user_service.search(session, query, limit=8)
    await state.clear()
    if not rows:
        await show(message, "نتیجه‌ای پیدا نشد.")
        return

    kb = KeyboardBuilder(session=session, columns=1)
    lines = [f"<b>نتایج جست‌وجو</b> ({total} مورد)\n"]
    for user in rows:
        lines.append(
            f"👤 <b>{html_escape(user.display_name)}</b> — <code>{user.telegram_id}</code>\n"
            f"   موجودی: {format_amount(user.balance_rial)} | "
            f"{'⛔️ مسدود' if user.is_blocked else 'فعال'}"
        )
        await kb.add(
            "admin.users",
            text=f"👤 {user.display_name}",
            callback=AdminCB(action="user", target_id=user.id).pack(),
        )
    kb.row()
    await kb.add("menu.back", callback=AdminCB(action="menu").pack())
    await show(message, "\n".join(lines), keyboard=kb.build())


@router.callback_query(AdminCB.filter(F.action == "user"), IsStaff())
async def user_card(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    await answer_callback(callback)
    user = await session.get(User, callback_data.target_id)
    if user is None:
        await show(callback, "کاربر پیدا نشد.")
        return
    stats = await user_service.stats(session, user)

    body = (
        f"<b>👤 {html_escape(user.display_name)}</b>\n\n"
        f"شناسه: <code>{user.telegram_id}</code>\n"
        f"نام کاربری: {'@' + user.username if user.username else '—'}\n"
        f"موجودی: <b>{format_amount(user.balance_rial)}</b>\n"
        f"سفارش‌ها: {stats.orders_total} (پرداخت‌شده {stats.orders_paid})\n"
        f"سرویس فعال: {stats.services_active}\n"
        f"معرفی‌شده‌ها: {stats.referrals}\n"
        f"عضویت: {jalali_datetime(user.created_at)}\n"
        f"آخرین فعالیت: {jalali_datetime(user.last_seen_at)}\n"
        f"وضعیت: {'⛔️ مسدود — ' + html_escape(user.block_reason or '') if user.is_blocked else '✅ فعال'}"
        + (f"\n\n<b>یادداشت:</b> {html_escape(user.note)}" if user.note else "")
    )
    await show(callback, body, keyboard=await _user_actions(session, user))


async def _user_actions(session: AsyncSession, user: User):
    kb = KeyboardBuilder(session=session, columns=2)
    await kb.add("admin.users", text="💰 تغییر موجودی", callback=AdminCB(action="balance", target_id=user.id).pack())
    if user.is_blocked:
        await kb.add("admin.users", text="✅ رفع مسدودی", callback=AdminCB(action="unblock", target_id=user.id).pack())
    else:
        await kb.add("admin.users", text="⛔️ مسدود کردن", callback=AdminCB(action="block", target_id=user.id).pack())
    await kb.add("admin.users", text="✍️ یادداشت", callback=AdminCB(action="note", target_id=user.id).pack())
    await kb.add("admin.users", text="📨 پیام", callback=AdminCB(action="dm", target_id=user.id).pack())
    kb.row()
    await kb.add("menu.back", callback=AdminCB(action="menu").pack())
    return kb.build()


# ---------------------------------------------------------------------------
@router.callback_query(AdminCB.filter(F.action == "block"), IsAdmin())
async def block_user(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, staff: Staff) -> None:
    user = await session.get(User, callback_data.target_id)
    if user is None:
        await callback.answer("کاربر پیدا نشد.", show_alert=True)
        return
    await user_service.set_blocked(session, user, True, "مسدودسازی توسط مدیر")
    await audit.record(session, "user.block", actor=staff, user_id=user.id, entity="user", entity_id=user.id)
    await answer_callback(callback, "کاربر مسدود شد.")
    await user_card(callback, callback_data, session)


@router.callback_query(AdminCB.filter(F.action == "unblock"), IsAdmin())
async def unblock_user(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, staff: Staff) -> None:
    user = await session.get(User, callback_data.target_id)
    if user is None:
        await callback.answer("کاربر پیدا نشد.", show_alert=True)
        return
    await user_service.set_blocked(session, user, False)
    await audit.record(session, "user.unblock", actor=staff, user_id=user.id, entity="user", entity_id=user.id)
    await answer_callback(callback, "مسدودی برداشته شد.")
    await user_card(callback, callback_data, session)


@router.callback_query(AdminCB.filter(F.action == "balance"), IsAdmin())
async def ask_balance(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, state: FSMContext
) -> None:
    user = await session.get(User, callback_data.target_id)
    if user is None:
        await callback.answer("کاربر پیدا نشد.", show_alert=True)
        return
    await answer_callback(callback)
    await state.set_state(AdminStates.entering_balance)
    await state.update_data(target_user_id=user.id)
    await show(
        callback,
        f"<b>💰 تغییر موجودی {html_escape(user.display_name)}</b>\n\n"
        f"موجودی فعلی: {format_amount(user.balance_rial)}\n\n"
        "مبلغ را به تومان بنویسید. برای کاهش موجودی، علامت منفی بگذارید.\n"
        "مثال: <code>500000</code> یا <code>-250000</code>",
    )


@router.message(AdminStates.entering_balance, F.text)
async def apply_balance(message: Message, session: AsyncSession, staff: Staff, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    negative = raw.startswith("-") or raw.startswith("−")
    amount = parse_user_amount(raw.lstrip("-−"))
    data = await state.get_data()
    user = await session.get(User, int(data.get("target_user_id") or 0))
    await state.clear()

    if user is None or amount <= 0:
        await show(message, "⚠️ مبلغ وارد شده معتبر نیست.")
        return

    try:
        if negative:
            await user_service.debit(session, user, amount, description="اصلاح موجودی توسط مدیر", staff_id=staff.id)
        else:
            await user_service.credit(
                session,
                user,
                amount,
                description="افزایش موجودی توسط مدیر",
                staff_id=staff.id,
            )
    except AppError as exc:
        await show(message, f"⚠️ {html_escape(exc.message)}")
        return

    await audit.record(
        session,
        "user.balance",
        actor=staff,
        user_id=user.id,
        entity="user",
        entity_id=user.id,
        meta={"delta_rial": -amount if negative else amount},
    )
    await show(
        message,
        f"✅ موجودی جدید: <b>{format_amount(user.balance_rial)}</b>",
    )
    await notifier.to_user(
        user,
        f"<b>موجودی کیف پول شما به‌روزرسانی شد.</b>\n\nموجودی جدید: {format_amount(user.balance_rial)}",
    )


@router.callback_query(AdminCB.filter(F.action == "note"), IsStaff())
async def ask_note(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, state: FSMContext) -> None:
    user = await session.get(User, callback_data.target_id)
    if user is None:
        await callback.answer("کاربر پیدا نشد.", show_alert=True)
        return
    await answer_callback(callback)
    await state.set_state(AdminStates.entering_note)
    await state.update_data(target_user_id=user.id, mode="note")
    await show(callback, "✍️ یادداشت داخلی درباره این کاربر را بنویسید.")


@router.message(AdminStates.entering_note, F.text)
async def save_note(message: Message, session: AsyncSession, staff: Staff, state: FSMContext) -> None:
    data = await state.get_data()
    user = await session.get(User, int(data.get("target_user_id") or 0))
    await state.clear()
    if user is None:
        await show(message, "⚠️ کاربر پیدا نشد.")
        return
    user.note = (message.text or "").strip()[:2000]
    await audit.record(session, "user.note", actor=staff, user_id=user.id, entity="user", entity_id=user.id)
    await show(message, "✅ یادداشت ذخیره شد.")


@router.callback_query(AdminCB.filter(F.action == "dm"), IsStaff())
async def ask_dm(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, state: FSMContext) -> None:
    user = await session.get(User, callback_data.target_id)
    if user is None:
        await callback.answer("کاربر پیدا نشد.", show_alert=True)
        return
    await answer_callback(callback)
    await state.set_state(AdminStates.messaging_user)
    await state.update_data(target_user_id=user.id, mode="dm")
    await show(callback, "📨 متن پیام برای کاربر را بنویسید.")


@router.message(AdminStates.messaging_user, F.text)
async def send_direct_message(message: Message, session: AsyncSession, staff: Staff, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("mode") != "dm":
        # Belongs to the receipt flow, which owns its own handler.
        return
    user = await session.get(User, int(data.get("target_user_id") or 0))
    await state.clear()
    if user is None:
        await show(message, "⚠️ کاربر پیدا نشد.")
        return
    sent = await notifier.to_user(user, f"<b>پیام از پشتیبانی</b>\n\n{html_escape((message.text or '').strip())}")
    await show(
        message,
        "✅ پیام ارسال شد." if sent is not None else "⚠️ ارسال ناموفق بود (کاربر ربات را بلاک کرده است).",
    )


@router.callback_query(AdminCB.filter(F.action == "export_users"), IsAdmin())
async def export_users(callback: CallbackQuery, session: AsyncSession) -> None:
    from app.services.reports import reports

    await answer_callback(callback, "در حال ساخت فایل…")
    csv_text = await reports.services_csv(session)
    chat_id = callback.message.chat.id if callback.message else None
    if chat_id:
        await notifier.send_upload(chat_id, csv_text.encode("utf-8"), "services.csv", caption="📄 خروجی سرویس‌ها")


__all__ = ["router"]
