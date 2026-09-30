"""Customer services: list, detail, config/QR/link, rotation, devices and renewals."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import DeviceCB, MenuCB, NavCB, ServiceCB
from app.bot.keyboards import KeyboardBuilder
from app.bot.menus import device_list, service_detail, service_list
from app.bot.states import ServiceStates
from app.bot.utils import alert_text, answer_callback, paginate, show
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.money import parse_user_amount
from app.db.models import OrderKind, Plan, Service, ServiceStatus, User
from app.services.delivery import delivery
from app.services.orders import order_service
from app.services.provisioning import provisioning
from app.services.texts import html_escape, texts

log = get_logger(__name__)
router = Router(name="services")

SERVICES_PER_PAGE = 6


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------
@router.callback_query(MenuCB.filter(F.action == "services"))
async def list_services(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    await answer_callback(callback)
    await _render_list(callback, session, user, page=1)


@router.callback_query(ServiceCB.filter(F.action == "list"))
async def list_services_again(
    callback: CallbackQuery, callback_data: ServiceCB, session: AsyncSession, user: User
) -> None:
    await answer_callback(callback)
    await _render_list(callback, session, user, page=callback_data.page or 1)


@router.callback_query(ServiceCB.filter(F.action == "page"))
async def page_services(callback: CallbackQuery, callback_data: ServiceCB, session: AsyncSession, user: User) -> None:
    await answer_callback(callback)
    await _render_list(callback, session, user, page=callback_data.page or 1)


async def _render_list(event: Message | CallbackQuery, session: AsyncSession, user: User, *, page: int) -> None:
    services = await user_service_list(session, user.id)
    if not services:
        kb = KeyboardBuilder(session=session, columns=1)
        await kb.add("menu.buy", callback=MenuCB(action="buy").pack())
        await kb.add("menu.test", callback=MenuCB(action="test").pack())
        kb.row()
        await kb.add("menu.main", callback=NavCB(to="main").pack())
        await show(event, await texts.get("service.empty", session), keyboard=kb.build())
        return

    slice_, page, total_pages = paginate(services, page, SERVICES_PER_PAGE)
    body = await texts.get("service.list_title", session, count=fa(str(len(services))))
    await show(event, body, keyboard=await service_list(session, slice_, page, total_pages))


async def user_service_list(session: AsyncSession, user_id: int) -> list[Service]:
    rows = list(
        (
            await session.execute(
                select(Service)
                .where(Service.user_id == user_id, Service.status != ServiceStatus.DELETED)
                .order_by(Service.created_at.desc())
            )
        ).scalars()
    )
    return rows


# ---------------------------------------------------------------------------
# Detail & delivery
# ---------------------------------------------------------------------------
@router.callback_query(ServiceCB.filter(F.action == "view"))
async def view_service(callback: CallbackQuery, callback_data: ServiceCB, session: AsyncSession, user: User) -> None:
    service = await _load(session, callback_data.service_id, user)
    if service is None:
        await callback.answer("این سرویس پیدا نشد.", show_alert=True)
        return
    await answer_callback(callback)
    await show(
        callback,
        await delivery.service_summary(session, service),
        keyboard=await service_detail(session, service, page=callback_data.page or 1),
    )


@router.callback_query(ServiceCB.filter(F.action == "config"))
async def send_config(callback: CallbackQuery, callback_data: ServiceCB, session: AsyncSession, user: User) -> None:
    service = await _load(session, callback_data.service_id, user)
    if service is None:
        await callback.answer("این سرویس پیدا نشد.", show_alert=True)
        return
    device = delivery.primary_device(service)
    if device is None:
        await callback.answer("دستگاهی برای این سرویس ثبت نشده است.", show_alert=True)
        return
    await answer_callback(callback, "در حال ارسال…")
    await delivery.send_config(session, service, device)


@router.callback_query(ServiceCB.filter(F.action == "qr"))
async def send_qr(callback: CallbackQuery, callback_data: ServiceCB, session: AsyncSession, user: User) -> None:
    service = await _load(session, callback_data.service_id, user)
    if service is None:
        await callback.answer("این سرویس پیدا نشد.", show_alert=True)
        return
    device = delivery.primary_device(service)
    if device is None:
        await callback.answer("دستگاهی برای این سرویس ثبت نشده است.", show_alert=True)
        return
    await answer_callback(callback, "در حال ساخت QR…")
    await delivery.send_qr(session, service, device)


@router.callback_query(ServiceCB.filter(F.action == "link"))
async def send_link(callback: CallbackQuery, callback_data: ServiceCB, session: AsyncSession, user: User) -> None:
    service = await _load(session, callback_data.service_id, user)
    if service is None:
        await callback.answer("این سرویس پیدا نشد.", show_alert=True)
        return
    await answer_callback(callback)
    if not await delivery.send_subscription(session, service):
        await show(
            callback,
            await texts.get("error.panel_down", session),
            keyboard=await service_detail(session, service, page=callback_data.page or 1),
        )


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------
@router.callback_query(ServiceCB.filter(F.action == "rotate"))
async def ask_rotate(callback: CallbackQuery, callback_data: ServiceCB, session: AsyncSession, user: User) -> None:
    service = await _load(session, callback_data.service_id, user)
    if service is None:
        await callback.answer("این سرویس پیدا نشد.", show_alert=True)
        return
    await answer_callback(callback)
    kb = KeyboardBuilder(session=session, columns=2)
    await kb.add("common.yes", callback=ServiceCB(action="rotate_ok", service_id=service.id).pack())
    await kb.add("common.no", callback=ServiceCB(action="view", service_id=service.id).pack())
    await show(
        callback,
        await texts.get("service.rotate_confirm", session, name=service.wg_username),
        keyboard=kb.build(),
    )


@router.callback_query(ServiceCB.filter(F.action == "rotate_ok"))
async def do_rotate(callback: CallbackQuery, callback_data: ServiceCB, session: AsyncSession, user: User) -> None:
    service = await _load(session, callback_data.service_id, user)
    if service is None:
        await callback.answer("این سرویس پیدا نشد.", show_alert=True)
        return
    await answer_callback(callback, "در حال تغییر کلیدها…")
    # The rotated config and subscription are persisted by ``rotate_access``; the
    # config is re-read from the database below, so both returned values are dropped.
    try:
        _config, _link = await provisioning.rotate_access(session, service)
    except AppError as exc:
        await show(
            callback,
            f"⚠️ {html_escape(exc.message)}",
            keyboard=await service_detail(session, service, page=callback_data.page or 1),
        )
        return

    await show(
        callback,
        await texts.get("service.rotated", session),
        keyboard=await service_detail(session, service, page=callback_data.page or 1),
    )
    device = delivery.primary_device(service)
    if device is not None:
        await session.refresh(device)
        await delivery.send_config(session, service, device)


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------
@router.callback_query(ServiceCB.filter(F.action == "devices"))
async def manage_devices(callback: CallbackQuery, callback_data: ServiceCB, session: AsyncSession, user: User) -> None:
    service = await _load(session, callback_data.service_id, user)
    if service is None:
        await callback.answer("این سرویس پیدا نشد.", show_alert=True)
        return
    await session.refresh(service, ["devices"])
    await answer_callback(callback)
    await show(
        callback,
        f"<b>دستگاه‌های {html_escape(service.wg_username)}</b>\n\nمحدودیت: {fa(str(service.device_limit))} دستگاه",
        keyboard=await device_list(session, service),
    )


@router.callback_query(DeviceCB.filter(F.action == "add"))
async def add_device(callback: CallbackQuery, callback_data: DeviceCB, session: AsyncSession, user: User) -> None:
    service = await _load(session, callback_data.service_id, user)
    if service is None:
        await callback.answer("این سرویس پیدا نشد.", show_alert=True)
        return
    try:
        device = await provisioning.add_device(session, service)
    except AppError as exc:
        await callback.answer(exc.message[:190], show_alert=True)
        return
    await answer_callback(callback, "دستگاه اضافه شد ✅")
    await delivery.send_config(session, service, device)


@router.callback_query(DeviceCB.filter(F.action == "delete"))
async def delete_device(callback: CallbackQuery, callback_data: DeviceCB, session: AsyncSession, user: User) -> None:
    service = await _load(session, callback_data.service_id, user)
    if service is None:
        await callback.answer("این سرویس پیدا نشد.", show_alert=True)
        return
    device = next((d for d in service.devices if d.id == callback_data.device_id), None)
    if device is None:
        await callback.answer("دستگاه پیدا نشد.", show_alert=True)
        return
    if len(service.devices) <= 1:
        await callback.answer("حداقل یک دستگاه باید باقی بماند.", show_alert=True)
        return
    try:
        await provisioning.delete_device(session, device)
    except AppError as exc:
        await callback.answer(exc.message[:190], show_alert=True)
        return
    await answer_callback(callback, "دستگاه حذف شد.")
    await session.refresh(service, ["devices"])
    await show(
        callback,
        f"<b>دستگاه‌های {html_escape(service.wg_username)}</b>",
        keyboard=await device_list(session, service),
    )


@router.callback_query(DeviceCB.filter(F.action == "qr"))
async def device_qr(callback: CallbackQuery, callback_data: DeviceCB, session: AsyncSession, user: User) -> None:
    service = await _load(session, callback_data.service_id, user)
    if service is None:
        await callback.answer("این سرویس پیدا نشد.", show_alert=True)
        return
    device = next((d for d in service.devices if d.id == callback_data.device_id), None)
    if device is None:
        await callback.answer("دستگاه پیدا نشد.", show_alert=True)
        return
    await answer_callback(callback)
    await delivery.send_config(session, service, device)
    await delivery.send_qr(session, service, device)


# ---------------------------------------------------------------------------
# Renewals and add-ons
# ---------------------------------------------------------------------------
@router.callback_query(ServiceCB.filter(F.action == "renew"))
async def renew_service(callback: CallbackQuery, callback_data: ServiceCB, session: AsyncSession, user: User) -> None:
    service = await _load(session, callback_data.service_id, user)
    if service is None:
        await callback.answer("این سرویس پیدا نشد.", show_alert=True)
        return
    if (service.meta or {}).get("paid_next_plan"):
        try:
            await provisioning.sync_service(session, service)
        except AppError:
            await callback.answer(alert_text(await texts.get("service.renew_review", session)), show_alert=True)
            return
    if (service.meta or {}).get("paid_next_plan"):
        key = (
            "service.renew_review"
            if service.meta["paid_next_plan"].get("state") == "needs_review"
            else "service.renew_pending"
        )
        await callback.answer(alert_text(await texts.get(key, session)), show_alert=True)
        return
    if service.auto_renew:
        await callback.answer(alert_text(await texts.get("service.renew_review", session)), show_alert=True)
        return
    from app.panels.base import CAP_NEXT_PLAN
    from app.panels.manager import panel_manager

    try:
        _, _panel, provider = await panel_manager.find_service(session, service.id)
        if not provider.supports(CAP_NEXT_PLAN):
            await callback.answer(
                alert_text(await texts.get("service.autorenew_unsupported", session)), show_alert=True
            )
            return
        remote = await provider.get_user(service.wg_user_id)
        provisioning.apply_remote_state(service, remote)
        if remote.traffic_limit_bytes is None and remote.duration_seconds is None and remote.expires_at is None:
            await callback.answer(alert_text(await texts.get("service.renew_unavailable", session)), show_alert=True)
            return
        if await provider.next_plan(service.wg_user_id) is not None:
            await callback.answer(alert_text(await texts.get("service.renew_review", session)), show_alert=True)
            return
    except AppError as exc:
        await callback.answer(alert_text(exc.message), show_alert=True)
        return
    if service.plan_id is None:
        await callback.answer("پلن این سرویس در دسترس نیست.", show_alert=True)
        return

    plan = await session.get(Plan, service.plan_id)
    if plan is None:
        await callback.answer("پلن این سرویس حذف شده است.", show_alert=True)
        return

    try:
        order = await order_service.create(session, user, plan, kind=OrderKind.RENEW, service=service)
    except AppError as exc:
        await callback.answer(exc.message[:190], show_alert=True)
        return

    await answer_callback(callback)
    if order.payable_rial <= 0:
        from app.bot.handlers.purchase import finalize_order

        await finalize_order(callback, session, order)
        return

    from app.bot.menus import payment_methods
    from app.services.settings_store import card_payments_enabled, wallet_enabled

    await show(
        callback,
        await texts.get(
            "service.renew_choose",
            session,
            plan=html_escape(plan.name),
            price=format_balance(order.payable_rial),
            service=html_escape(service.wg_username),
        ),
        keyboard=await payment_methods(session, order, card=card_payments_enabled(), wallet=wallet_enabled()),
    )


@router.callback_query(ServiceCB.filter(F.action == "extra"))
async def extra_traffic(callback: CallbackQuery, callback_data: ServiceCB, session: AsyncSession, user: User) -> None:
    """Sell the current product's volume as an explicitly priced quota add-on."""
    service = await _load(session, callback_data.service_id, user)
    plan = await session.get(Plan, service.plan_id) if service and service.plan_id else None
    if service is None or plan is None:
        await callback.answer(alert_text(await texts.get("service.topup_unavailable", session)), show_alert=True)
        return
    try:
        from app.panels.base import CAP_QUOTA_TOP_UP
        from app.panels.manager import panel_manager

        _, _panel, provider = await panel_manager.find_service(session, service.id)
        if not provider.supports(CAP_QUOTA_TOP_UP):
            await callback.answer(alert_text(await texts.get("service.topup_unavailable", session)), show_alert=True)
            return
        remote = await provider.get_user(service.wg_user_id)
        provisioning.apply_remote_state(service, remote)
        order = await order_service.create(session, user, plan, kind=OrderKind.EXTRA_TRAFFIC, service=service)
    except AppError as exc:
        await callback.answer(alert_text(exc.message), show_alert=True)
        return
    await answer_callback(callback)
    if order.payable_rial <= 0:
        from app.bot.handlers.purchase import finalize_order

        await finalize_order(callback, session, order)
        return
    from app.bot.menus import payment_methods
    from app.core.money import format_gb
    from app.services.settings_store import card_payments_enabled, wallet_enabled

    await show(
        callback,
        await texts.get(
            "service.topup_choose",
            session,
            volume=format_gb(order.traffic_gb),
            price=format_balance(order.payable_rial),
            service=html_escape(service.wg_username),
        ),
        keyboard=await payment_methods(session, order, card=card_payments_enabled(), wallet=wallet_enabled()),
    )


# ---------------------------------------------------------------------------
# Auto-renew (queued successor plan on the WG-Guard node)
# ---------------------------------------------------------------------------
@router.callback_query(ServiceCB.filter(F.action == "autorenew"))
async def toggle_autorenew(
    callback: CallbackQuery, callback_data: ServiceCB, session: AsyncSession, user: User
) -> None:
    """A successor is a paid order, never a free toggle or silent cancellation."""
    await renew_service(callback, callback_data, session, user)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def _load(session: AsyncSession, service_id: int, user: User) -> Service | None:
    if not service_id:
        return None
    service = await session.get(Service, service_id)
    if service is None or service.user_id != user.id or service.status == ServiceStatus.DELETED:
        return None
    return service


def fa(value: str | int) -> str:
    from app.core.money import fa_digits

    return fa_digits(value)


def format_balance(rial: int) -> str:
    from app.core.money import format_amount

    return format_amount(rial)


async def count_services(session: AsyncSession, user_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count(Service.id)).where(Service.user_id == user_id, Service.status != ServiceStatus.DELETED)
        )
        or 0
    )


@router.message(ServiceStates.entering_extra_traffic, F.text)
async def extra_traffic_amount(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    amount = parse_user_amount(message.text or "0")
    if amount <= 0:
        await show(message, "⚠️ عدد وارد شده معتبر نیست.")
        return
    await state.clear()
    await show(message, "درخواست شما ثبت شد؛ کارشناسان ما به‌زودی هماهنگ می‌کنند.")


__all__ = ["count_services", "router", "user_service_list"]
