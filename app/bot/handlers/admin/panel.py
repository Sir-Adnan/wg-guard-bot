"""Operator panel inside the bot: overview, panels, orders and exports."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminCB, NavCB, OrderCB
from app.bot.filters import IsStaff
from app.bot.keyboards import KeyboardBuilder
from app.bot.menus import admin_menu
from app.bot.utils import answer_callback, show
from app.core.jalali import jalali_date, jalali_datetime
from app.core.logging import get_logger
from app.core.money import format_amount
from app.db.models import OrderStatus, Staff
from app.panels.manager import panel_manager
from app.services.orders import order_service
from app.services.receipts import receipt_service
from app.services.reports import reports
from app.services.texts import html_escape, texts

log = get_logger(__name__)
router = Router(name="admin_panel")

ORDERS_PER_PAGE = 8


@router.message(Command("admin"), IsStaff())
async def admin_command(message: Message, session: AsyncSession, staff: Staff) -> None:
    await _render_menu(message, session, staff)


@router.callback_query(AdminCB.filter(F.action == "menu"), IsStaff())
async def admin_menu_cb(callback: CallbackQuery, session: AsyncSession, staff: Staff) -> None:
    await answer_callback(callback)
    await _render_menu(callback, session, staff)


async def _render_menu(event, session: AsyncSession, staff: Staff) -> None:
    pending = await receipt_service.pending_count(session)
    body = (
        f"<b>🛠 پنل مدیریت</b>\n\n"
        f"👤 {html_escape(staff.name or 'مدیر')} — نقش: <b>{_role_label(staff)}</b>\n"
        f"🧾 رسیدهای در انتظار: <b>{pending}</b>"
    )
    await show(event, body, keyboard=await admin_menu(session, pending_receipts=pending))


def _role_label(staff: Staff) -> str:
    return {"owner": "مالک", "admin": "مدیر", "support": "پشتیبان"}.get(staff.role.value, staff.role.value)


# ---------------------------------------------------------------------------
# Sales overview
# ---------------------------------------------------------------------------
@router.callback_query(AdminCB.filter(F.action == "stats"), IsStaff())
async def admin_stats(callback: CallbackQuery, session: AsyncSession) -> None:
    await answer_callback(callback)
    summary = await reports.summary(session, days=30)

    kb = KeyboardBuilder(session=session, columns=1)
    await kb.add("admin.receipts", callback=AdminCB(action="receipts", page=1).pack())
    await kb.add("admin.orders", callback=AdminCB(action="orders", page=1).pack())
    kb.row()
    await kb.add("menu.main", callback=NavCB(to="main").pack())

    body = (
        "<b>📊 گزارش ۳۰ روز گذشته</b>\n\n"
        f"💰 درآمد: <b>{format_amount(summary.revenue_rial)}</b>\n"
        f"📅 امروز: {format_amount(summary.revenue_today_rial)}\n"
        f"🧾 سفارش‌ها: {summary.orders_total} (تکمیل: {summary.orders_completed})\n"
        f"⏳ در انتظار: {summary.orders_pending} | ❌ ناموفق: {summary.orders_failed}\n"
        f"📈 میانگین سفارش: {format_amount(summary.average_order_rial)}\n\n"
        f"👥 کاربران جدید: {summary.new_users}\n"
        f"🟢 سرویس فعال: {summary.active_services}\n"
        f"⏰ نزدیک به انقضا: {summary.expiring_soon}\n"
        f"👛 مجموع موجودی کاربران: {format_amount(summary.wallet_liability_rial)}\n"
        f"🧾 رسید در انتظار: {summary.pending_receipts}\n"
        f"🎫 تیکت باز: {summary.open_tickets}"
    )
    await show(callback, body, keyboard=kb.build())


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------
@router.callback_query(AdminCB.filter(F.action == "panels"), IsStaff())
async def admin_panels(callback: CallbackQuery, session: AsyncSession) -> None:
    await answer_callback(callback)
    snapshots = await panel_manager.list_snapshots(session)
    if not snapshots:
        await show(
            callback,
            "هیچ پنلی ثبت نشده است.\n\nاز پنل مدیریت وب بخش «پنل‌ها» اولین نود WG-Guard خود را اضافه کنید.",
        )
        return

    lines = ["<b>🖥 وضعیت پنل‌ها</b>\n"]
    for snap in snapshots:
        icon = {"online": "🟢", "degraded": "🟡", "offline": "🔴"}.get(snap.health.value, "⚪️")
        capacity = "∞" if snap.max_services is None else str(snap.max_services)
        lines.append(
            f"{icon} <b>{html_escape(snap.name)}</b>\n"
            f"   سرویس: {snap.service_count}/{capacity} | نسخه: {snap.node_version or '—'}\n"
            f"   <code>{html_escape(snap.base_url)}</code>"
        )
    await show(callback, "\n".join(lines))


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
@router.callback_query(AdminCB.filter(F.action == "orders"), IsStaff())
async def admin_orders(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    await answer_callback(callback)
    page = max(callback_data.page or 1, 1)
    rows, total = await order_service.search(session, limit=ORDERS_PER_PAGE, offset=(page - 1) * ORDERS_PER_PAGE)
    if not rows:
        await show(callback, "سفارشی ثبت نشده است.")
        return

    lines = [f"<b>🧾 سفارش‌ها</b> — مجموع {total}\n"]
    for order in rows:
        user = order.user
        lines.append(
            f"{_status_icon(order.status)} <code>{order.order_code}</code> — {html_escape(order.plan_name)}\n"
            f"   {html_escape(user.display_name if user else '—')} | "
            f"{format_amount(order.payable_rial)} | {jalali_date(order.created_at)}"
        )

    kb = KeyboardBuilder(session=session, columns=2)
    if page > 1:
        await kb.add("common.prev_page", callback=AdminCB(action="orders", page=page - 1).pack())
    if page * ORDERS_PER_PAGE < total:
        await kb.add("common.next_page", callback=AdminCB(action="orders", page=page + 1).pack())
    kb.row()
    await kb.add("menu.back", callback=AdminCB(action="menu").pack())
    await show(callback, "\n".join(lines), keyboard=kb.build())


def _status_icon(status: OrderStatus) -> str:
    return {
        OrderStatus.COMPLETED: "✅",
        OrderStatus.PAID: "🟦",
        OrderStatus.PROVISIONING: "⏳",
        OrderStatus.PENDING_PAYMENT: "🟡",
        OrderStatus.AWAITING_REVIEW: "🧾",
        OrderStatus.FAILED: "❌",
        OrderStatus.CANCELED: "⚪️",
        OrderStatus.EXPIRED: "⌛️",
        OrderStatus.REFUNDED: "↩️",
        OrderStatus.DRAFT: "📝",
    }.get(status, "•")


@router.callback_query(AdminCB.filter(F.action == "export"), IsStaff())
async def admin_export(callback: CallbackQuery, session: AsyncSession) -> None:
    await answer_callback(callback, "در حال ساخت فایل…")
    csv_text = await reports.orders_csv(session, days=90)
    from app.services.notifications import notifier

    chat_id = callback.message.chat.id if callback.message else None
    if chat_id is None:
        return
    await notifier.send_upload(
        chat_id,
        csv_text.encode("utf-8"),
        "orders.csv",
        caption="📄 خروجی سفارش‌های ۹۰ روز گذشته",
    )


# ---------------------------------------------------------------------------
# Pending receipts queue
# ---------------------------------------------------------------------------
@router.callback_query(AdminCB.filter(F.action == "receipts"), IsStaff())
async def admin_receipts(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    await answer_callback(callback)
    page = max(callback_data.page or 1, 1)
    from app.db.models import ReceiptStatus
    from app.services.receipts import receipt_service as rs

    rows, total = await rs.search(
        session,
        status=ReceiptStatus.PENDING,
        limit=ORDERS_PER_PAGE,
        offset=(page - 1) * ORDERS_PER_PAGE,
    )
    if not rows:
        await show(
            callback,
            await texts.get("common.done", session) + "\n\nهیچ رسیدی در انتظار بررسی نیست. 🎉",
        )
        return

    lines = [f"<b>🧾 رسیدهای در انتظار بررسی</b> — {total} مورد\n"]
    for receipt in rows:
        user = receipt.user
        lines.append(
            f"<code>{receipt.code}</code> — {format_amount(receipt.amount_rial)}\n"
            f"   {html_escape(user.display_name if user else '—')} | "
            f"{jalali_datetime(receipt.created_at)}"
        )
    lines.append("\n<i>برای بررسی، روی پیام رسید در چت خودتان اقدام کنید.</i>")

    kb = KeyboardBuilder(session=session, columns=2)
    if page > 1:
        await kb.add("common.prev_page", callback=AdminCB(action="receipts", page=page - 1).pack())
    if page * ORDERS_PER_PAGE < total:
        await kb.add("common.next_page", callback=AdminCB(action="receipts", page=page + 1).pack())
    kb.row()
    await kb.add("menu.back", callback=AdminCB(action="menu").pack())
    await show(callback, "\n".join(lines), keyboard=kb.build())


@router.callback_query(OrderCB.filter(F.action == "retry"), IsStaff())
async def admin_retry_order(callback: CallbackQuery, callback_data: OrderCB, session: AsyncSession) -> None:
    from app.core.errors import AppError
    from app.db.models import Order

    order = await session.get(Order, callback_data.order_id)
    if order is None:
        await callback.answer("سفارش پیدا نشد.", show_alert=True)
        return
    try:
        await order_service.retry_provisioning(session, order)
    except AppError as exc:
        await callback.answer(exc.message[:190], show_alert=True)
        return
    await answer_callback(callback, "در حال تلاش دوباره…")

    from app.bot.handlers.purchase import finalize_order

    await finalize_order(callback, session, order)


__all__ = ["router"]
