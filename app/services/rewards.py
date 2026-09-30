"""Exactly-once order rewards, shared by wallet and receipt payment flows."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import format_amount, percent_of_rial
from app.db.models import Order, OrderStatus, Payment, PaymentKind, PaymentMethod, User
from app.services.notifications import notifier
from app.services.texts import texts


async def apply_order_rewards(session: AsyncSession, order: Order) -> None:
    if order.status != OrderStatus.COMPLETED or (order.meta or {}).get("reward_policy") != 1:
        return
    await apply_cashback(session, order)
    if order.user and order.user.referred_by_id:
        await pay_referral_bonus(session, order)


async def apply_cashback(session: AsyncSession, order: Order) -> None:
    """Return a configurable percentage of a completed purchase to the wallet."""
    percent = Decimal((order.meta or {}).get("cashback_percent", "0"))
    paid_total = int(order.payable_rial) + int(order.wallet_used_rial)
    if percent <= 0 or paid_total <= 0 or order.is_test:
        return

    buyer = order.user or await session.get(User, order.user_id)
    if buyer is None:
        return

    from app.services.users import user_service

    await session.scalar(select(Order).where(Order.id == order.id).with_for_update(of=Order))
    reference = f"cashback:{order.id}"
    if await session.scalar(
        select(Payment.id).where(Payment.order_id == order.id, Payment.reference == reference).limit(1)
    ):
        return
    amount = percent_of_rial(paid_total, percent)
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
        reference=reference,
    )
    await notifier.to_user(
        buyer,
        await texts.get(
            "cashback.notice", session, amount=format_amount(amount), balance=format_amount(buyer.balance_rial)
        ),
    )


async def pay_referral_bonus(session: AsyncSession, order: Order) -> None:
    """Credit the referrer a share of a completed purchase."""
    percent = Decimal((order.meta or {}).get("referral_percent", "0"))
    paid_total = int(order.payable_rial) + int(order.wallet_used_rial)
    if percent <= 0 or paid_total <= 0 or order.is_test or order.user is None or order.user.referred_by_id is None:
        return
    from app.core.jalali import now_utc
    from app.services.users import user_service

    referrer = await session.get(User, order.user.referred_by_id)
    if referrer is None:
        return
    await session.scalar(select(Order).where(Order.id == order.id).with_for_update(of=Order))
    reference = f"referral:{order.id}"
    if await session.scalar(
        select(Payment.id).where(Payment.order_id == order.id, Payment.reference == reference).limit(1)
    ):
        return
    bonus = percent_of_rial(paid_total, percent)
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
        reference=reference,
    )
    referrer.referral_earnings_rial = int(referrer.referral_earnings_rial) + bonus
    buyer = order.user
    if buyer is not None:
        buyer.meta = {**(buyer.meta or {}), "referral_paid_at": now_utc().isoformat()}
    await notifier.to_user(
        referrer,
        await texts.get("profile.referral_stats", session, count="—", earnings=format_amount(bonus)),
    )
