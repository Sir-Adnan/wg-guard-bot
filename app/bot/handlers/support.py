"""Support tickets (customer side)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import MenuCB, SupportCB
from app.bot.keyboards import KeyboardBuilder
from app.bot.menus import support_menu, ticket_actions
from app.bot.states import SupportStates
from app.bot.utils import alert_text, answer_callback, show, truncate
from app.core.errors import AppError
from app.core.jalali import jalali_datetime
from app.core.logging import get_logger
from app.db.models import Ticket, TicketStatus, User
from app.services.notifications import notifier
from app.services.settings_store import app_settings
from app.services.texts import html_escape, texts
from app.services.tickets import tickets

log = get_logger(__name__)
router = Router(name="support")

STATUS_LABELS = {
    TicketStatus.OPEN: "🟡 در انتظار پاسخ",
    TicketStatus.ANSWERED: "🟢 پاسخ داده شده",
    TicketStatus.PENDING: "🟠 در انتظار شما",
    TicketStatus.CLOSED: "⚪️ بسته‌شده",
}


# ---------------------------------------------------------------------------
@router.callback_query(MenuCB.filter(F.action == "support"))
async def open_support(event: Message | CallbackQuery, session: AsyncSession) -> None:
    await answer_callback(event)
    await show(event, await texts.get("support.title", session), keyboard=await support_menu(session))


@router.callback_query(SupportCB.filter(F.action == "menu"))
async def back_support(callback: CallbackQuery, session: AsyncSession) -> None:
    await answer_callback(callback)
    await show(callback, await texts.get("support.title", session), keyboard=await support_menu(session))


# ---------------------------------------------------------------------------
# New ticket
# ---------------------------------------------------------------------------
@router.callback_query(SupportCB.filter(F.action == "new"))
async def new_ticket(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await answer_callback(callback)
    await state.set_state(SupportStates.entering_subject)
    await show(callback, await texts.get("support.ask_subject", session))


@router.message(SupportStates.entering_subject, F.text)
async def receive_subject(message: Message, session: AsyncSession, state: FSMContext) -> None:
    subject = truncate((message.text or "").strip(), 120)
    if len(subject) < 3:
        await show(message, "⚠️ موضوع خیلی کوتاه است. لطفاً کمی توضیح دهید.")
        return
    await state.update_data(subject=subject)
    await state.set_state(SupportStates.entering_message)
    await show(message, await texts.get("support.ask_message", session))


@router.message(SupportStates.entering_message)
async def receive_message(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    data = await state.get_data()
    subject = str(data.get("subject") or "بدون موضوع")
    text = (message.text or message.caption or "").strip()
    file_id = None
    file_type = None
    if message.photo:
        file_id, file_type = message.photo[-1].file_id, "photo"
    elif message.document:
        file_id, file_type = message.document.file_id, "document"

    if not text and not file_id:
        await show(message, "⚠️ لطفاً متن پیام یا یک تصویر ارسال کنید.")
        return

    try:
        ticket = await tickets.create(
            session, user, subject=subject, text=text or "—", file_id=file_id, file_type=file_type
        )
    except AppError as exc:
        await state.clear()
        await show(message, f"⚠️ {html_escape(exc.message)}")
        return

    await state.clear()
    await show(
        message,
        await texts.get("support.created", session, code=ticket.ticket_code),
        keyboard=await ticket_actions(session, ticket.id),
    )
    await _notify_staff(session, ticket, user, text or "📎 فایل پیوست")


async def _notify_staff(session: AsyncSession, ticket, user: User, text: str) -> None:
    if not app_settings.get_bool("notify.admin_new_ticket", True):
        return
    body = await texts.get("support.admin_notify", session, code=ticket.ticket_code, user=user.mention)
    body += f"\n\n<b>موضوع:</b> {html_escape(ticket.subject)}\n{html_escape(truncate(text, 500))}"

    kb = KeyboardBuilder(session=session, columns=2)
    await kb.add("support.reply", callback=SupportCB(action="reply", ticket_id=ticket.id).pack())
    await kb.add("support.close_ticket", callback=SupportCB(action="close", ticket_id=ticket.id).pack())

    try:
        # Staff reply through the bot; the web panel offers the same thread.
        await notifier.to_staff(session, body, keyboard=kb.build())
    except Exception as exc:  # pragma: no cover
        log.warning("Ticket notification failed: %s", exc)


# ---------------------------------------------------------------------------
# Ticket list & thread
# ---------------------------------------------------------------------------
@router.callback_query(SupportCB.filter(F.action == "list"))
async def list_tickets(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    await answer_callback(callback)
    rows = await tickets.list_for_user(session, user.id)
    if not rows:
        await show(callback, await texts.get("support.empty", session), keyboard=await support_menu(session))
        return

    kb = KeyboardBuilder(session=session, columns=1)
    lines = [await texts.get("support.list_title", session, count=str(len(rows))), ""]
    for ticket in rows:
        status = STATUS_LABELS.get(ticket.status, ticket.status.value)
        lines.append(f"{status} — <code>{ticket.ticket_code}</code> — {html_escape(truncate(ticket.subject, 40))}")
        await kb.add(
            "support.reply",
            text=f"🎫 {ticket.ticket_code}",
            callback=SupportCB(action="view", ticket_id=ticket.id).pack(),
        )
    kb.row()
    await kb.add("menu.back", callback=SupportCB(action="menu").pack())
    await show(callback, "\n".join(lines), keyboard=kb.build())


@router.callback_query(SupportCB.filter(F.action == "view"))
async def view_ticket(callback: CallbackQuery, callback_data: SupportCB, session: AsyncSession, user: User) -> None:
    ticket = await _load(session, callback_data.ticket_id, user)
    if ticket is None:
        await callback.answer("این تیکت پیدا نشد.", show_alert=True)
        return
    await answer_callback(callback)
    await tickets.mark_read_by_user(session, ticket)

    messages = await tickets.messages(session, ticket.id, limit=20)
    lines = [
        f"<b>🎫 تیکت {ticket.ticket_code}</b>",
        f"<b>موضوع:</b> {html_escape(ticket.subject)}",
        f"<b>وضعیت:</b> {STATUS_LABELS.get(ticket.status, ticket.status.value)}",
        "",
    ]
    for entry in messages:
        who = "👤 شما" if entry.sender.value == "user" else "🎧 پشتیبانی"
        lines.append(
            f"<b>{who}</b> — <i>{jalali_datetime(entry.created_at)}</i>\n"
            f"{html_escape(truncate(entry.text or '📎 فایل', 400))}\n"
        )
    await show(callback, "\n".join(lines), keyboard=await ticket_actions(session, ticket.id))


@router.callback_query(SupportCB.filter(F.action == "reply"))
async def reply_ticket(
    callback: CallbackQuery, callback_data: SupportCB, session: AsyncSession, user: User, state: FSMContext
) -> None:
    ticket = await _load(session, callback_data.ticket_id, user)
    if ticket is None:
        await callback.answer("این تیکت پیدا نشد.", show_alert=True)
        return
    if ticket.status == TicketStatus.CLOSED:
        await callback.answer(
            alert_text(await texts.get("support.closed", session, code=ticket.ticket_code)), show_alert=True
        )
        return
    await answer_callback(callback)
    await state.set_state(SupportStates.replying)
    await state.update_data(ticket_id=ticket.id)
    await show(callback, "✍️ پیام خود را بنویسید. برای انصراف /cancel را بزنید.")


@router.message(SupportStates.replying)
async def receive_reply(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    data = await state.get_data()
    ticket = await _load(session, int(data.get("ticket_id") or 0), user)
    if ticket is None:
        await state.clear()
        await show(message, await texts.get("error.expired_action", session))
        return

    text = (message.text or message.caption or "").strip()
    file_id = None
    file_type = None
    if message.photo:
        file_id, file_type = message.photo[-1].file_id, "photo"
    elif message.document:
        file_id, file_type = message.document.file_id, "document"
    if not text and not file_id:
        await show(message, "⚠️ پیام خالی است.")
        return

    try:
        await tickets.add_user_message(session, ticket, text=text or "—", file_id=file_id, file_type=file_type)
    except AppError as exc:
        await state.clear()
        await show(message, f"⚠️ {html_escape(exc.message)}")
        return

    await state.clear()
    await show(
        message,
        await texts.get("support.created", session, code=ticket.ticket_code),
        keyboard=await ticket_actions(session, ticket.id),
    )
    await _notify_staff(session, ticket, user, text or "📎 فایل پیوست")


@router.callback_query(SupportCB.filter(F.action == "close"))
async def close_ticket(callback: CallbackQuery, callback_data: SupportCB, session: AsyncSession, user: User) -> None:
    ticket = await _load(session, callback_data.ticket_id, user)
    if ticket is None:
        await callback.answer("این تیکت پیدا نشد.", show_alert=True)
        return
    await tickets.close(session, ticket)
    await answer_callback(callback, "تیکت بسته شد.")
    await show(
        callback,
        await texts.get("support.closed", session, code=ticket.ticket_code),
        keyboard=await support_menu(session),
    )


# ---------------------------------------------------------------------------
async def _load(session: AsyncSession, ticket_id: int, user: User) -> Ticket | None:
    if not ticket_id:
        return None
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None or ticket.user_id != user.id:
        return None
    return ticket


__all__ = ["STATUS_LABELS", "router"]
