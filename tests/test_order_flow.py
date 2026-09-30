"""An unfinished order must never be a dead end.

«یک سفارش ناتمام برای این پلن دارید» used to offer exactly two buttons: send a
receipt, or cancel.  For an order that had not reached the receipt step yet, both
were wrong — and a customer who simply wanted to buy was stuck.  The screen now
offers the two doors the message always promised: continue that order, or close
it and start a clean one.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.bot.callbacks import BuyCB, PlanCB
from app.bot.setup import reset_dispatcher
from app.db.models import Order, OrderStatus, ReceiptMedia, ReceiptStatus
from app.services.orders import order_service
from app.services.receipts import receipt_service

pytestmark = pytest.mark.db

CUSTOMER_TELEGRAM_ID = 111_222_333


@pytest.fixture(autouse=True)
def _clean_dispatcher():
    reset_dispatcher()
    yield
    reset_dispatcher()


async def _orders(session) -> list[Order]:
    return list((await session.execute(select(Order).order_by(Order.id.asc()))).scalars())


async def _press(bot, payload: str, feed_update) -> None:
    await feed_update(bot, callback=payload, telegram_id=CUSTOMER_TELEGRAM_ID)


async def test_a_second_purchase_offers_both_doors(session, customer, plan, bound_notifier, recording_bot, feed_update):
    order = await order_service.create(session, customer, plan)
    await session.commit()

    await _press(recording_bot, PlanCB(action="buy", plan_id=plan.id).pack(), feed_update)

    bodies = " ".join(recording_bot.texts())
    assert "سفارش ناتمام" in bodies, f"the unfinished-order screen never appeared: {recording_bot.texts()}"
    payloads = _keyboard_payloads(recording_bot)
    assert BuyCB(action="resume", order_id=order.id).pack() in payloads, "no way to continue the order"
    assert BuyCB(action="restart", order_id=order.id).pack() in payloads, "no way to start a fresh order"


async def test_continuing_a_draft_order_returns_to_the_payment_step(
    session, customer, plan, bound_notifier, recording_bot, feed_update
):
    order = await order_service.create(session, customer, plan)
    await session.commit()

    await _press(recording_bot, BuyCB(action="resume", order_id=order.id).pack(), feed_update)

    bodies = " ".join(recording_bot.texts())
    assert "انتخاب روش پرداخت" in bodies, f"the payment step never appeared: {recording_bot.texts()}"
    payloads = _keyboard_payloads(recording_bot)
    assert BuyCB(action="wallet", order_id=order.id).pack() in payloads


async def test_continuing_an_order_under_review_does_not_ask_for_another_receipt(
    session, customer, plan, bound_notifier, recording_bot, feed_update
):
    order = await order_service.create(session, customer, plan)
    await order_service.mark_awaiting_review(session, order)
    await session.commit()

    await _press(recording_bot, BuyCB(action="resume", order_id=order.id).pack(), feed_update)

    bodies = " ".join(recording_bot.texts())
    assert "در حال بررسی" in bodies, f"the review status was not shown: {recording_bot.texts()}"
    payloads = _keyboard_payloads(recording_bot)
    assert BuyCB(action="send_receipt", order_id=order.id).pack() not in payloads, "a receipt was asked for twice"


async def test_starting_a_fresh_order_closes_the_old_one(
    session, customer, plan, bound_notifier, recording_bot, feed_update
):
    old = await order_service.create(session, customer, plan)
    await session.commit()

    await _press(recording_bot, BuyCB(action="restart", order_id=old.id).pack(), feed_update)

    await session.refresh(old)
    assert old.status is OrderStatus.CANCELED, "the unfinished order was left open alongside the new one"
    orders = await _orders(session)
    assert len(orders) == 2, "no fresh order was created"
    fresh = next(row for row in orders if row.id != old.id)
    assert fresh.status in (OrderStatus.DRAFT, OrderStatus.PENDING_PAYMENT)
    assert "انتخاب روش پرداخت" in " ".join(recording_bot.texts())


async def test_a_paid_order_is_never_thrown_away_by_starting_over(
    session, customer, plan, bound_notifier, recording_bot, feed_update
):
    """A card payment lives outside the wallet: cancelling it needs a human."""
    old = await order_service.create(session, customer, plan)
    await order_service.mark_awaiting_review(session, old)
    receipt = await receipt_service.create(
        session,
        customer,
        purpose="purchase",
        amount_rial=old.payable_rial,
        media=ReceiptMedia.TEXT,
        order=old,
    )
    await session.commit()

    await _press(recording_bot, BuyCB(action="restart", order_id=old.id).pack(), feed_update)

    await session.refresh(old)
    assert old.status is OrderStatus.AWAITING_REVIEW, "a paid order was cancelled by one button press"
    await session.refresh(receipt)
    assert receipt.status is ReceiptStatus.PENDING, "the pending receipt was decided by a button press"
    assert len(await _orders(session)) == 1, "a second order was created for an order under review"
    bodies = " ".join(recording_bot.texts()) + " " + " ".join(recording_bot.alerts())
    assert "پشتیبانی" in bodies, f"the customer was not told why: {recording_bot.texts()} {recording_bot.alerts()}"


def _keyboard_payloads(bot) -> set[str]:
    """Every callback payload the bot drew, across all keyboards."""
    out: set[str] = set()
    for _name, kwargs in bot.calls:
        markup = kwargs.get("reply_markup")
        for row in getattr(markup, "inline_keyboard", []) or []:
            for button in row:
                if button.callback_data:
                    out.add(button.callback_data)
    return out
