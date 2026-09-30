"""Payment and provisioning orchestration for a single order."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import BuyCB, MenuCB, NavCB
from app.bot.keyboards import KB, KeyboardBuilder
from app.bot.menus import card_payment_actions, default_main_menu, payment_methods, service_detail
from app.bot.states import ShopStates
from app.bot.utils import answer_callback, show
from app.core.errors import AppError, InsufficientFunds
from app.core.jalali import jalali_datetime
from app.core.logging import get_logger
from app.core.money import format_amount
from app.db.models import CardAccount, Order, OrderStatus, PaymentMethod, Plan, Service, User
from app.services.catalog import catalog
from app.services.delivery import delivery
from app.services.notifications import notifier
from app.services.orders import order_service
from app.services.provisioning import provisioning
from app.services.settings_store import app_settings, card_payments_enabled, wallet_enabled
from app.services.texts import html_escape, texts

log = get_logger(__name__)
router = Router(name="purchase")


# ---------------------------------------------------------------------------
# Payment method selection
# ---------------------------------------------------------------------------
@router.callback_query(BuyCB.filter(F.action == "wallet"))
async def pay_with_wallet(callback: CallbackQuery, callback_data: BuyCB, session: AsyncSession, user: User) -> None:
    order = await _load_order(session, callback_data.order_id, user)
    if order is None:
        await callback.answer("این سفارش معتبر نیست.", show_alert=True)
        return
    if not wallet_enabled():
        await callback.answer("پرداخت از کیف پول غیرفعال است.", show_alert=True)
        return
    if order.status != OrderStatus.PENDING_PAYMENT:
        await callback.answer("این سفارش قبلاً پرداخت شده است.", show_alert=True)
        return
    if user.balance_rial < order.payable_rial:
        shortage = order.payable_rial - user.balance_rial
        await callback.answer()
        await show(
            callback,
            await texts.get(
                "buy.wallet_insufficient",
                session,
                balance=format_amount(user.balance_rial),
                needed=format_amount(shortage),
            ),
            keyboard=await _wallet_shortage_keyboard(session),
        )
        return

    try:
        await order_service.mark_paid(session, order, method=PaymentMethod.WALLET)
    except InsufficientFunds as exc:
        await callback.answer(exc.message[:190], show_alert=True)
        return

    await answer_callback(callback, "پرداخت انجام شد ✅")
    await finalize_order(callback, session, order)


async def _wallet_shortage_keyboard(session: AsyncSession):
    from app.bot.callbacks import NavCB, WalletCB

    kb = KeyboardBuilder(session=session, columns=1)
    await kb.add("wallet.deposit", callback=WalletCB(action="deposit").pack())
    await kb.add("menu.main", callback=NavCB(to="main").pack())
    return kb.build()


# ---------------------------------------------------------------------------
# Card-to-card
# ---------------------------------------------------------------------------
@router.callback_query(BuyCB.filter(F.action == "card"))
async def pay_with_card(
    callback: CallbackQuery, callback_data: BuyCB, session: AsyncSession, user: User, state: FSMContext
) -> None:
    order = await _load_order(session, callback_data.order_id, user)
    if order is None:
        await callback.answer("این سفارش معتبر نیست.", show_alert=True)
        return
    if not card_payments_enabled():
        await callback.answer("پرداخت کارت به کارت غیرفعال است.", show_alert=True)
        return

    card = (
        (
            await session.execute(
                select(CardAccount).where(CardAccount.is_active.is_(True)).order_by(CardAccount.sort_order.asc())
            )
        )
        .scalars()
        .first()
    )
    if card is None:
        await callback.answer("در حال حاضر شماره کارتی برای واریز ثبت نشده است.", show_alert=True)
        return

    await answer_callback(callback)
    await state.set_state(ShopStates.awaiting_receipt)
    await state.update_data(order_id=order.id)

    body = await texts.get(
        "buy.invoice",
        session,
        order=order.order_code,
        plan=order.plan_name,
        amount=format_amount(order.amount_rial),
        discount=format_amount(order.discount_rial),
        wallet=format_amount(order.wallet_used_rial),
        payable=format_amount(order.payable_rial),
        deadline=jalali_datetime(order.payment_deadline),
    )
    card_body = await texts.get(
        "buy.card_info",
        session,
        card=card.card_number,
        holder=card.holder_name,
        bank=card.bank_name or "—",
        amount=format_amount(order.payable_rial),
        order=order.order_code,
    )
    if card.iban or card.instructions:
        card_body += "\n\n" + await texts.get(
            "buy.card_extra", session, bank=card.bank_name or "—", iban=card.iban or "—"
        )
    if card.instructions:
        card_body += "\n\n" + card.instructions

    await show(callback, f"{body}\n\n{card_body}", keyboard=await card_payment_actions(session, order))


@router.callback_query(BuyCB.filter(F.action == "send_receipt"))
async def prompt_receipt(
    callback: CallbackQuery, callback_data: BuyCB, session: AsyncSession, user: User, state: FSMContext
) -> None:
    order = await _load_order(session, callback_data.order_id, user)
    if order is None:
        await callback.answer("این سفارش معتبر نیست.", show_alert=True)
        return
    await answer_callback(callback)
    await state.set_state(ShopStates.awaiting_receipt)
    await state.update_data(order_id=order.id)
    await show(callback, await texts.get("receipt.ask_photo", session))


@router.callback_query(BuyCB.filter(F.action == "cancel"))
async def cancel_order(
    callback: CallbackQuery, callback_data: BuyCB, session: AsyncSession, user: User, state: FSMContext
) -> None:
    order = await _load_order(session, callback_data.order_id, user)
    if order is not None and order.is_open:
        await order_service.cancel(session, order, reason="لغو توسط کاربر")
    await state.clear()
    await answer_callback(callback)
    await show(
        callback,
        await texts.get("buy.canceled", session, order=order.order_code if order else "—"),
        keyboard=await default_main_menu(session),
    )


# ---------------------------------------------------------------------------
# Discount code
# ---------------------------------------------------------------------------
@router.callback_query(BuyCB.filter(F.action == "discount"))
async def ask_discount(callback: CallbackQuery, callback_data: BuyCB, session: AsyncSession, state: FSMContext) -> None:
    await answer_callback(callback)
    await state.set_state(ShopStates.entering_discount)
    await state.update_data(order_id=callback_data.order_id, purpose="discount")
    await show(callback, "🎟 کد تخفیف خود را ارسال کنید.\nبرای انصراف /cancel را بزنید.")


@router.message(ShopStates.entering_discount, F.text)
async def apply_discount(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("purpose") != "discount":
        return  # another flow owns this state
    order = await _load_order(session, int(data.get("order_id") or 0), user)
    if order is None or order.status != OrderStatus.PENDING_PAYMENT:
        await state.clear()
        await show(message, await texts.get("error.expired_action", session))
        return

    code = (message.text or "").strip()
    plan = order.plan
    if plan is None:
        await state.clear()
        await show(message, await texts.get("error.not_found", session))
        return

    try:
        applied = await order_service.apply_discount_code(session, order, code)
    except AppError as exc:
        await show(message, f"⚠️ {html_escape(exc.message)}")
        return

    await state.set_state(ShopStates.choosing_method)
    await show(
        message,
        await texts.get("buy.choose_method", session, plan=order.plan_name, price=format_amount(order.payable_rial))
        + f"\n\n🎟 کد <code>{applied}</code> اعمال شد.",
        keyboard=await payment_methods(session, order, card=card_payments_enabled(), wallet=wallet_enabled()),
    )


# ---------------------------------------------------------------------------
# Retry provisioning
# ---------------------------------------------------------------------------
@router.callback_query(BuyCB.filter(F.action == "retry"))
async def retry_order(callback: CallbackQuery, callback_data: BuyCB, session: AsyncSession, user: User) -> None:
    order = await _load_order(session, callback_data.order_id, user)
    if order is None or order.status not in (OrderStatus.FAILED, OrderStatus.PAID):
        await callback.answer("این سفارش قابل تلاش دوباره نیست.", show_alert=True)
        return
    await order_service.retry_provisioning(session, order)
    await answer_callback(callback, "در حال تلاش دوباره…")
    await finalize_order(callback, session, order)


# ---------------------------------------------------------------------------
# An unfinished order: continue it, or start a clean one
# ---------------------------------------------------------------------------
@router.callback_query(BuyCB.filter(F.action == "resume"))
async def resume_order(
    callback: CallbackQuery, callback_data: BuyCB, session: AsyncSession, user: User, state: FSMContext
) -> None:
    """Take the customer back to wherever their unfinished order stopped."""
    order = await _load_order(session, callback_data.order_id, user)
    if order is None or not order.is_open:
        await callback.answer(await texts.get("error.expired_action", session), show_alert=True)
        return
    await answer_callback(callback)
    # Whatever half-finished flow was pending (a receipt photo, a discount code)
    # is not what the customer just asked for.
    await state.clear()
    body, keyboard = await order_next_step(session, order)
    await show(callback, body, keyboard=keyboard)


async def order_next_step(session: AsyncSession, order: Order) -> tuple[str, KB]:
    """What an unfinished order still needs, and the buttons that move it on.

    The screen used to be the same two buttons whatever the status, so a
    customer whose receipt was already with the reviewers was invited to send it
    again — and one who had never chosen a payment method was offered a receipt
    button for an order that had no amount due yet.
    """
    if order.status is OrderStatus.AWAITING_REVIEW:
        return await texts.get("buy.awaiting_review", session, order=order.order_code), await default_main_menu(session)
    if order.status in (OrderStatus.PAID, OrderStatus.PROVISIONING):
        return await texts.get("buy.in_progress", session, order=order.order_code), await default_main_menu(session)

    if order.status is OrderStatus.FAILED:
        kb = KeyboardBuilder(session=session, columns=1)
        await kb.add("buy.retry", callback=BuyCB(action="retry", order_id=order.id).pack())
        await kb.add("menu.support", callback=MenuCB(action="support").pack())
        kb.row()
        await kb.add("menu.main", callback=NavCB(to="main").pack())
        body = await texts.get(
            "buy.failed",
            session,
            order=order.order_code,
            reason=html_escape(order.failure_reason or "—"),
        )
        return body, kb.build()

    # Draft or waiting to be paid: the customer still has to pick a method.
    body = await texts.get(
        "buy.choose_method",
        session,
        plan=html_escape(order.plan_name),
        price=format_amount(order.payable_rial),
    )
    return body, await payment_methods(session, order, card=card_payments_enabled(), wallet=wallet_enabled())


@router.callback_query(BuyCB.filter(F.action == "restart"))
async def restart_order(
    callback: CallbackQuery, callback_data: BuyCB, session: AsyncSession, user: User, state: FSMContext
) -> None:
    """Close the unfinished order and start a clean one for the same plan.

    Only an order that has not been paid for is thrown away.  A card payment
    lives outside the wallet, so cancelling an order whose receipt is being
    reviewed would strand the customer's money until a human untangled it — that
    is a support conversation, not a button.
    """
    order = await _load_order(session, callback_data.order_id, user)
    if order is None:
        await callback.answer(await texts.get("error.expired_action", session), show_alert=True)
        return

    if order.status not in (OrderStatus.DRAFT, OrderStatus.PENDING_PAYMENT):
        await callback.answer(await texts.get("buy.cannot_restart", session), show_alert=True)
        body, keyboard = await order_next_step(session, order)
        await show(callback, body, keyboard=keyboard)
        return

    plan = order.plan
    await order_service.cancel(session, order, reason="شروع سفارش تازه توسط کاربر")
    await state.clear()

    if plan is None or not plan.is_active:
        await answer_callback(callback)
        await show(callback, await texts.get("error.not_found", session), keyboard=await default_main_menu(session))
        return
    if not catalog.is_available(plan):
        await answer_callback(callback)
        await show(callback, await texts.get("shop.sold_out", session), keyboard=await default_main_menu(session))
        return

    await answer_callback(callback)
    await begin_order(callback, session, user, plan)


async def begin_order(event: CallbackQuery | Message, session: AsyncSession, user: User, plan: Plan) -> Order | None:
    """Create the order and show the next step.

    Shared by the shop's «همین را می‌خواهم» and by «سفارش تازه», so both routes
    price, create and continue an order the same way.
    """
    try:
        order = await order_service.create(session, user, plan)
    except AppError as exc:
        await answer_callback(event, exc.message[:190], alert=True)
        return None

    if order.payable_rial <= 0:
        await finalize_order(event, session, order)
        return order

    await show(
        event,
        await texts.get(
            "buy.choose_method", session, plan=html_escape(plan.name), price=format_amount(order.payable_rial)
        ),
        keyboard=await payment_methods(session, order, card=card_payments_enabled(), wallet=wallet_enabled()),
    )
    return order


# ---------------------------------------------------------------------------
# Shared finalisation
# ---------------------------------------------------------------------------
async def finalize_order(event: CallbackQuery | Message, session: AsyncSession, order: Order) -> None:
    """Provision the order, then deliver the service — or explain the failure."""
    await show(event, await texts.get("buy.processing", session))

    # Release the transaction before touching the network.
    await session.commit()

    result = await provisioning.provision_order(order.id)
    await session.refresh(order)

    if not result.ok or result.service_id is None:
        reason = result.error or "خطای نامشخص"
        kb = KeyboardBuilder(session=session, columns=1)
        await kb.add("buy.retry", callback=BuyCB(action="retry", order_id=order.id).pack())
        await kb.add("menu.support", callback=MenuCB(action="support").pack())
        kb.row()
        await kb.add("menu.main", callback=NavCB(to="main").pack())
        await show(
            event,
            await texts.get("buy.failed", session, order=order.order_code, reason=html_escape(reason)),
            keyboard=kb.build(),
        )
        await _alert_staff_failure(session, order, reason)
        return

    service = await session.get(Service, result.service_id)
    if service is None:
        return

    intro = await delivery.purchase_success_text(session, service, order.order_code)
    await delivery.deliver_service(session, service, intro=intro)
    await show(event, await delivery.service_summary(session, service), keyboard=await service_detail(session, service))

    await _apply_cashback(session, order)
    if order.user and order.user.referred_by_id:
        await _pay_referral_bonus(session, order)


async def _apply_cashback(session: AsyncSession, order: Order) -> None:
    """Return a configurable percentage of a completed purchase to the wallet."""
    percent = app_settings.get_float("shop.cashback_percent", 0.0)
    if percent <= 0 or order.payable_rial <= 0 or order.is_test:
        return

    buyer = order.user or await session.get(User, order.user_id)
    if buyer is None:
        return

    from app.db.models import PaymentKind
    from app.services.users import user_service

    amount = int(order.payable_rial * percent / 100)
    if amount <= 0:
        return

    await user_service.credit(
        session,
        buyer,
        amount,
        kind=PaymentKind.GIFT,
        method=PaymentMethod.GIFT,
        order_id=order.id,
        description=f"کش‌بک سفارش {order.order_code}",
    )
    await notifier.to_user(
        buyer,
        await texts.get(
            "cashback.notice", session, amount=format_amount(amount), balance=format_amount(buyer.balance_rial)
        ),
    )


async def _alert_staff_failure(session: AsyncSession, order: Order, reason: str) -> None:
    await notifier.to_admins(
        session,
        "⚠️ <b>ساخت سرویس ناموفق بود</b>\n\n"
        f"سفارش: <code>{order.order_code}</code>\n"
        f"کاربر: {order.user.mention if order.user else '—'}\n"
        f"دلیل: {html_escape(reason)}",
        disable_notification=False,
    )


async def _pay_referral_bonus(session: AsyncSession, order: Order) -> None:
    """Credit the referrer a share of a completed purchase."""
    percent = app_settings.get_float("shop.referral_percent", 0.0)
    if percent <= 0 or order.payable_rial <= 0:
        return
    from app.core.jalali import now_utc
    from app.db.models import PaymentKind
    from app.services.users import user_service

    referrer = await session.get(User, order.user.referred_by_id)  # type: ignore[arg-type]
    if referrer is None:
        return
    bonus = int(order.payable_rial * percent / 100)
    if bonus <= 0:
        return
    await user_service.credit(
        session,
        referrer,
        bonus,
        kind=PaymentKind.REFERRAL,
        method=PaymentMethod.REFERRAL,
        order_id=order.id,
        description=f"پاداش معرفی — سفارش {order.order_code}",
    )
    referrer.referral_earnings_rial = int(referrer.referral_earnings_rial) + bonus
    buyer = order.user
    if buyer is not None:
        buyer.meta = {**(buyer.meta or {}), "referral_paid_at": now_utc().isoformat()}
    await notifier.to_user(
        referrer,
        await texts.get("profile.referral_stats", session, count="—", earnings=format_amount(bonus)),
    )


# ---------------------------------------------------------------------------
async def _load_order(session: AsyncSession, order_id: int, user: User) -> Order | None:
    if not order_id:
        return None
    order = await session.get(Order, order_id)
    if order is None or order.user_id != user.id:
        return None
    return order


@router.message(ShopStates.choosing_method)
async def ignore_stray(message: Message, session: AsyncSession) -> None:
    await show(message, await texts.get("error.expired_action", session))


__all__ = ["finalize_order", "router"]
