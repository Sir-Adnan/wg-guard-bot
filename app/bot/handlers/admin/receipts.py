"""Receipt review by operators — the multi-admin approval workflow.

Every reviewer holds a private copy of the same receipt message.  Whoever
decides first wins, and *all* copies are edited immediately afterwards so the
rest of the team sees the status and cannot re-decide.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import ReceiptCB
from app.bot.filters import IsStaff
from app.bot.menus import admin_menu
from app.bot.states import AdminStates
from app.bot.utils import answer_callback, show, truncate
from app.core.jalali import jalali_datetime
from app.core.logging import get_logger
from app.core.money import format_amount, format_rial
from app.db.models import Order, OrderStatus, Receipt, ReceiptStatus, Staff, User
from app.services.notifications import notifier
from app.services.receipts import receipt_service
from app.services.texts import html_escape

log = get_logger(__name__)
router = Router(name="admin_receipts")


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------
@router.callback_query(ReceiptCB.filter(F.action == "approve"), IsStaff())
async def approve_receipt(
    callback: CallbackQuery, callback_data: ReceiptCB, session: AsyncSession, staff: Staff
) -> None:
    receipt = await session.get(Receipt, callback_data.receipt_id)
    if receipt is None:
        await callback.answer("این رسید پیدا نشد.", show_alert=True)
        return

    outcome = await receipt_service.approve(session, receipt, staff)
    if not outcome.accepted:
        who = outcome.already_reviewed_by or "یکی از همکاران"
        await callback.answer(f"{outcome.message}\n({who})", show_alert=True)
        # Make sure this reviewer's copy reflects reality too.
        await receipt_service.refresh_copies(session, receipt, reviewer=None, decision=receipt.status.value)
        return

    await receipt_service.refresh_copies(session, receipt, reviewer=staff, decision="approved")
    await callback.answer("✅ رسید تأیید شد.")
    await receipt_service.notify_customer(session, receipt, approved=True)

    # A purchase receipt that is approved must immediately become a service.
    order = receipt.order
    if order is not None and order.status == OrderStatus.PAID:
        await _provision_after_approval(session, order, receipt)


async def _provision_after_approval(session: AsyncSession, order: Order, receipt: Receipt) -> None:
    from app.services.delivery import delivery
    from app.services.provisioning import provisioning

    await session.commit()
    result = await provisioning.provision_order(order.id)
    await session.refresh(order)

    if not result.ok or result.service_id is None:
        await notifier.to_admins(
            session,
            "⚠️ <b>ساخت سرویس پس از تأیید رسید ناموفق بود</b>\n\n"
            f"سفارش: <code>{order.order_code}</code>\n"
            f"رسید: <code>{receipt.code}</code>\n"
            f"دلیل: {html_escape(result.error or 'نامشخص')}",
        )
        return

    from app.db.models import Service

    service = await session.get(Service, result.service_id)
    if service is None:
        return
    intro = await delivery.purchase_success_text(session, service, order.order_code)
    await delivery.deliver_service(session, service, intro=intro)

    if receipt.user is not None:
        await notifier.to_admins(
            session,
            f"✅ سرویس <code>{service.wg_username}</code> برای سفارش "
            f"<code>{order.order_code}</code> ساخته و تحویل داده شد.",
            disable_notification=True,
        )


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------
@router.callback_query(ReceiptCB.filter(F.action == "reject"), IsStaff())
async def ask_reject_reason(
    callback: CallbackQuery, callback_data: ReceiptCB, session: AsyncSession, state: FSMContext
) -> None:
    receipt = await session.get(Receipt, callback_data.receipt_id)
    if receipt is None:
        await callback.answer("این رسید پیدا نشد.", show_alert=True)
        return
    if receipt.status != ReceiptStatus.PENDING:
        await callback.answer(
            f"این رسید قبلاً بررسی شده است ({receipt_service.status_label(receipt.status)}).",
            show_alert=True,
        )
        return

    await answer_callback(callback)
    await state.set_state(AdminStates.reject_reason)
    await state.update_data(receipt_id=receipt.id)
    await show(
        callback,
        f"<b>دلیل رد رسید <code>{receipt.code}</code> را بنویسید.</b>\n\n"
        "این متن عیناً برای کاربر ارسال می‌شود، پس محترمانه و شفاف بنویسید.\n\n"
        "<i>برای انصراف /cancel را بزنید.</i>",
    )


@router.message(AdminStates.reject_reason, F.text)
async def apply_reject_reason(message: Message, session: AsyncSession, staff: Staff, state: FSMContext) -> None:
    data = await state.get_data()
    receipt = await session.get(Receipt, int(data.get("receipt_id") or 0))
    if receipt is None:
        await state.clear()
        await show(message, "⚠️ رسید پیدا نشد.")
        return

    reason = truncate((message.text or "").strip(), 200) or "بدون توضیح"
    await state.clear()
    await _reject(session, receipt, staff, reason, message)


@router.callback_query(ReceiptCB.filter(F.action == "reject_reason"), IsStaff())
async def reject_with_template(
    callback: CallbackQuery, callback_data: ReceiptCB, session: AsyncSession, staff: Staff, state: FSMContext
) -> None:
    """Kept for API symmetry — a template still requires a typed reason."""
    await answer_callback(callback, "دلیل را به صورت متن بنویسید.", alert=True)


async def _reject(session: AsyncSession, receipt: Receipt, staff: Staff, reason: str, event) -> None:
    outcome = await receipt_service.reject(session, receipt, staff, reason=reason)
    if not outcome.accepted:
        who = outcome.already_reviewed_by or "یکی از همکاران"
        await show(event, f"⚠️ {outcome.message}\n({who})")
        await receipt_service.refresh_copies(session, receipt, reviewer=None, decision=receipt.status.value)
        return

    await receipt_service.refresh_copies(session, receipt, reviewer=staff, decision="rejected")
    await receipt_service.notify_customer(session, receipt, approved=False)
    if isinstance(event, Message):
        await show(
            event,
            f"❌ رسید <code>{receipt.code}</code> رد شد و به کاربر اطلاع داده شد.",
        )
    elif isinstance(event, CallbackQuery):
        await event.answer("❌ رسید رد شد.")


# ---------------------------------------------------------------------------
# Context actions
# ---------------------------------------------------------------------------
@router.callback_query(ReceiptCB.filter(F.action == "view_user"), IsStaff())
async def view_user(callback: CallbackQuery, callback_data: ReceiptCB, session: AsyncSession) -> None:
    receipt = await session.get(Receipt, callback_data.receipt_id)
    if receipt is None or receipt.user is None:
        await callback.answer("کاربر پیدا نشد.", show_alert=True)
        return
    user: User = receipt.user
    from app.services.users import user_service

    stats = await user_service.stats(session, user)
    await answer_callback(callback)
    await show(
        callback,
        f"<b>👤 {html_escape(user.display_name)}</b>\n\n"
        f"شناسه: <code>{user.telegram_id}</code>\n"
        f"نام کاربری: {'@' + user.username if user.username else '—'}\n"
        f"موجودی: {format_amount(user.balance_rial)}\n"
        f"سفارش‌ها: {stats.orders_total} (پرداخت‌شده: {stats.orders_paid})\n"
        f"سرویس فعال: {stats.services_active}\n"
        f"مجموع خرید: {format_rial(stats.spent_rial)}\n"
        f"عضویت: {jalali_datetime(user.created_at)}\n"
        f"وضعیت: {'⛔️ مسدود' if user.is_blocked else '✅ فعال'}",
        keyboard=await admin_menu(session),
    )


@router.callback_query(ReceiptCB.filter(F.action == "message"), IsStaff())
async def message_user(
    callback: CallbackQuery, callback_data: ReceiptCB, session: AsyncSession, state: FSMContext
) -> None:
    receipt = await session.get(Receipt, callback_data.receipt_id)
    if receipt is None or receipt.user is None:
        await callback.answer("کاربر پیدا نشد.", show_alert=True)
        return
    await answer_callback(callback)
    await state.set_state(AdminStates.messaging_user)
    await state.update_data(target_user_id=receipt.user_id, receipt_id=receipt.id, mode="receipt")
    await show(callback, "✍️ متن پیام برای کاربر را بنویسید. /cancel برای انصراف.")


@router.message(AdminStates.messaging_user, F.text, IsStaff())
async def send_user_message(message: Message, session: AsyncSession, staff: Staff, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("mode") != "receipt":
        return  # the user-management router owns this state when mode == "dm"
    user = await session.get(User, int(data.get("target_user_id") or 0))
    await state.clear()
    if user is None:
        await show(message, "⚠️ کاربر پیدا نشد.")
        return

    body = (message.text or "").strip()
    sent = await notifier.to_user(
        user,
        f"<b>پیام از پشتیبانی</b>\n\n{html_escape(body)}",
    )
    await show(
        message,
        "✅ پیام ارسال شد." if sent is not None else "⚠️ ارسال پیام ناموفق بود (کاربر ربات را بلاک کرده است).",
    )


__all__ = ["router"]
