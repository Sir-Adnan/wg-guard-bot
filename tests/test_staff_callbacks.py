"""The buttons a reviewer receives must be answered by the real bot.

Every other test in this suite reaches into a service or reads a router's
handler list.  None of them runs an update through the assembled dispatcher,
which is where the interesting mistakes live: a filter that depends on
middleware data, a router included in the wrong order, a handler shadowed by an
earlier one.

A reviewer pressing «تأیید رسید» and getting «این دکمه دیگر معتبر نیست» was
exactly that class of bug: aiogram evaluates a handler's filters *before* the
observer's inner middlewares, and ``IsStaff()`` reads what those middlewares
put in ``data``.  The whole operator surface — receipts, users, orders,
broadcast — was unreachable, and no service-level test could see it.
"""

from __future__ import annotations

import pytest

from app.bot.callbacks import AdminCB, ReceiptCB
from app.bot.setup import reset_dispatcher
from app.db.models import Receipt, ReceiptMedia, ReceiptStatus, Staff, StaffRole, User
from app.services.receipts import receipt_service

pytestmark = pytest.mark.db

STAFF_TELEGRAM_ID = 777_001
CUSTOMER_TELEGRAM_ID = 111_222_333
STALE_ALERT = "معتبر نیست"


@pytest.fixture(autouse=True)
def _clean_dispatcher():
    reset_dispatcher()
    yield
    reset_dispatcher()


async def _staff(session) -> Staff:
    row = Staff(
        name="مالک",
        role=StaffRole.OWNER,
        telegram_id=STAFF_TELEGRAM_ID,
        receive_receipts=True,
        is_active=True,
    )
    session.add(row)
    await session.flush()
    return row


async def _pending_receipt(session, customer: User) -> Receipt:
    receipt = await receipt_service.create(
        session,
        customer,
        purpose="deposit",
        amount_rial=500_000,
        media=ReceiptMedia.TEXT,
        note="واریز کارت به کارت",
    )
    await session.flush()
    return receipt


# ---------------------------------------------------------------------------
# The receipt queue
# ---------------------------------------------------------------------------
async def test_the_reviewers_approve_button_is_answered(
    session, customer, bound_notifier, recording_bot, feed_update
) -> None:
    await _staff(session)
    receipt = await _pending_receipt(session, customer)
    await session.commit()

    await feed_update(
        recording_bot,
        callback=ReceiptCB(action="approve", receipt_id=receipt.id).pack(),
        telegram_id=STAFF_TELEGRAM_ID,
    )

    await session.refresh(receipt)
    assert receipt.status is ReceiptStatus.APPROVED, (
        f"the approve button was not handled (alerts: {recording_bot.alerts()})"
    )
    assert not any(STALE_ALERT in alert for alert in recording_bot.alerts()), "the stale-button net answered instead"
    await session.refresh(customer)
    assert customer.balance_rial == 500_000  # the deposit was credited exactly once


async def test_the_reviewers_reject_button_asks_for_a_reason(
    session, customer, bound_notifier, recording_bot, feed_update
) -> None:
    await _staff(session)
    receipt = await _pending_receipt(session, customer)
    await session.commit()

    await feed_update(
        recording_bot,
        callback=ReceiptCB(action="reject", receipt_id=receipt.id).pack(),
        telegram_id=STAFF_TELEGRAM_ID,
    )

    assert receipt.status is ReceiptStatus.PENDING  # a reason is still required
    assert any("دلیل رد رسید" in body for body in recording_bot.texts()), "the reason prompt never appeared"
    assert not any(STALE_ALERT in alert for alert in recording_bot.alerts()), "the stale-button net answered instead"


# ---------------------------------------------------------------------------
# The rest of the operator surface, and the door that must stay shut
# ---------------------------------------------------------------------------
async def test_the_staff_menu_button_opens_the_admin_menu(session, bound_notifier, recording_bot, feed_update) -> None:
    await _staff(session)
    await session.commit()

    await feed_update(recording_bot, callback=AdminCB(action="menu").pack(), telegram_id=STAFF_TELEGRAM_ID)

    assert any("پنل مدیریت" in body for body in recording_bot.texts()), "no admin menu was drawn"
    assert not any(STALE_ALERT in alert for alert in recording_bot.alerts())


async def test_a_customer_cannot_reach_the_staff_buttons(
    session, customer, bound_notifier, recording_bot, feed_update
) -> None:
    """``IsStaff()`` must still keep a customer out of the operator screens."""
    await _staff(session)
    await session.commit()

    await feed_update(recording_bot, callback=AdminCB(action="menu").pack(), telegram_id=CUSTOMER_TELEGRAM_ID)

    assert not any("پنل مدیریت" in body for body in recording_bot.texts()), "a customer opened the admin menu"
    assert any(STALE_ALERT in alert for alert in recording_bot.alerts()), "the customer's press was silently dropped"
    # …and the alert is readable: Telegram does not parse HTML in an alert.
    for alert in recording_bot.alerts():
        assert "<" not in alert and ">" not in alert, f"markup in an alert: {alert!r}"
        assert "{" not in alert, f"an unresolved placeholder in an alert: {alert!r}"
