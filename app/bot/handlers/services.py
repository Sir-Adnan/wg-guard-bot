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
from app.bot.utils import answer_callback, paginate, show
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
        await texts.get("buy.choose_method", session, plan=plan.name, price=format_balance(order.payable_rial))
        + f"\n\n<b>سرویس:</b> <code>{html_escape(service.wg_username)}</code>",
        keyboard=await payment_methods(session, order, card=card_payments_enabled(), wallet=wallet_enabled()),
    )


@router.callback_query(ServiceCB.filter(F.action == "extra"))
async def extra_traffic(callback: CallbackQuery, callback_data: ServiceCB, session: AsyncSession, user: User) -> None:
    """Offer the cheapest non-test plan as a traffic top-up."""
    service = await _load(session, callback_data.service_id, user)
    if service is None:
        await callback.answer("این سرویس پیدا نشد.", show_alert=True)
        return
    await answer_callback(callback)
    await show(
        callback,
        "📶 برای خرید حجم اضافه، یک پلن مناسب انتخاب کنید و آن را به همین سرویس اضافه می‌کنیم.\n"
        "کافی است از فروشگاه، پلن مورد نظر را بخرید و سپس با پشتیبانی هماهنگ کنید.",
    )


# ---------------------------------------------------------------------------
# Auto-renew (queued successor plan on the WG-Guard node)
# ---------------------------------------------------------------------------
@router.callback_query(ServiceCB.filter(F.action == "autorenew"))
async def toggle_autorenew(
    callback: CallbackQuery, callback_data: ServiceCB, session: AsyncSession, user: User
) -> None:
    """Queue (or cancel) the successor plan so the node renews automatically."""
    service = await _load(session, callback_data.service_id, user)
    if service is None:
        await callback.answer("این سرویس پیدا نشد.", show_alert=True)
        return
    if service.plan_id is None:
        await callback.answer("پلن این سرویس مشخص نیست.", show_alert=True)
        return

    plan = await session.get(Plan, service.plan_id)
    if plan is None:
        await callback.answer("پلن این سرویس حذف شده است.", show_alert=True)
        return
    if not plan.wg_plan_id:
        await callback.answer(await texts.get("service.autorenew_unsupported", session), show_alert=True)
        return

    from app.panels.base import UnsupportedCapability
    from app.panels.manager import panel_manager

    try:
        _, _panel, provider = await panel_manager.find_service(session, service.id)
        if service.auto_renew:
            await provider.clear_next_plan(service.wg_user_id)
            service.auto_renew = False
            message = await texts.get("service.autorenew_off", session, name=service.wg_username)
        else:
            await provider.queue_next_plan(service.wg_user_id, plan.wg_plan_id, carry_unused_traffic=True)
            service.auto_renew = True
            message = await texts.get("service.autorenew_on", session, name=service.wg_username)
        await session.flush()
    except UnsupportedCapability:
        await callback.answer(await texts.get("service.autorenew_unsupported", session), show_alert=True)
        return
    except AppError as exc:
        await callback.answer(html_escape(exc.message)[:190], show_alert=True)
        return

    await answer_callback(callback, "انجام شد ✅")
    await show(callback, message, keyboard=await service_detail(session, service, page=callback_data.page or 1))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def _load(session: AsyncSession, service_id: int, user: User) -> Service | None:
    if not service_id:
        return None
    service = await session.get(Service, service_id)
    if service is None or service.user_id != user.id:
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
