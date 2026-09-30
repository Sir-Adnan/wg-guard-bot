"""Receipt review: one decision per receipt, and every reviewer sees it.

The interesting cases are the ones a single-user smoke test never reaches — two
reviewers deciding at the same moment, a copy whose buttons must disappear, and
a caption long enough that Telegram would refuse the whole message (which would
leave the reviewer with nothing at all).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Payment,
    PaymentKind,
    Receipt,
    ReceiptMedia,
    ReceiptMessage,
    ReceiptStatus,
    Staff,
    StaffRole,
    User,
)
from app.services.receipts import CAPTION_LIMIT, TEXT_LIMIT, fit, receipt_service

pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _reviewer(name: str, telegram_id: int) -> Staff:
    return Staff(
        name=name,
        role=StaffRole.ADMIN,
        login=name,
        telegram_id=telegram_id,
        receive_receipts=True,
        is_active=True,
    )


async def _deposit_receipt(session: AsyncSession, customer: User, amount: int = 500_000) -> Receipt:
    receipt = await receipt_service.create(
        session,
        customer,
        purpose="deposit",
        amount_rial=amount,
        media=ReceiptMedia.TEXT,
        note="واریز کارت به کارت",
        tracking_code="123456",
    )
    await session.flush()
    return receipt


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------
async def test_a_receipt_can_only_be_approved_once(session, customer) -> None:
    first_reviewer = _reviewer("first", 900_001)
    second_reviewer = _reviewer("second", 900_002)
    session.add_all([first_reviewer, second_reviewer])
    receipt = await _deposit_receipt(session, customer)
    await session.flush()

    first = await receipt_service.approve(session, receipt, first_reviewer)
    second = await receipt_service.approve(session, receipt, second_reviewer)

    assert first.accepted is True
    assert second.accepted is False
    assert second.already_reviewed_by == "first"
    assert customer.balance_rial == 500_000  # credited exactly once


async def test_a_stale_reviewer_cannot_credit_a_deposit_twice(session, customer, engine) -> None:
    """The race the read-then-write version lost.

    Reviewer B holds a copy of the row while reviewer A decides.  B's press must
    lose, and must not move money.
    """
    winner = _reviewer("winner", 900_011)
    loser = _reviewer("loser", 900_012)
    session.add_all([winner, loser])
    receipt = await _deposit_receipt(session, customer)
    await session.commit()

    async with AsyncSession(bind=engine, expire_on_commit=False, autoflush=False) as stale_session:
        stale_receipt = await stale_session.get(Receipt, receipt.id)
        assert stale_receipt is not None and stale_receipt.status is ReceiptStatus.PENDING

        first = await receipt_service.approve(session, receipt, winner)
        await session.commit()

        second = await receipt_service.approve(stale_session, stale_receipt, loser)
        await stale_session.commit()

    assert first.accepted is True
    assert second.accepted is False
    assert "همین لحظه" in second.message
    assert second.already_reviewed_by == "winner"

    await session.refresh(customer)
    assert customer.balance_rial == 500_000

    credit_rows = (
        (
            await session.execute(
                select(Payment).where(Payment.user_id == customer.id, Payment.kind == PaymentKind.DEPOSIT)
            )
        )
        .scalars()
        .all()
    )
    assert len(credit_rows) == 1


async def test_a_rejected_receipt_cannot_then_be_approved(session, customer) -> None:
    reviewer = _reviewer("reviewer", 900_021)
    session.add(reviewer)
    receipt = await _deposit_receipt(session, customer)
    await session.flush()

    rejected = await receipt_service.reject(session, receipt, reviewer, reason="مبلغ نادرست است")
    approved = await receipt_service.approve(session, receipt, reviewer)

    assert rejected.accepted is True
    assert approved.accepted is False
    assert customer.balance_rial == 0
    assert receipt.reject_reason == "مبلغ نادرست است"


async def test_an_expired_receipt_cannot_be_decided(session, customer) -> None:
    reviewer = _reviewer("reviewer", 900_031)
    session.add(reviewer)
    receipt = await _deposit_receipt(session, customer)
    receipt.status = ReceiptStatus.EXPIRED
    await session.flush()

    outcome = await receipt_service.approve(session, receipt, reviewer)

    assert outcome.accepted is False
    assert "مهلت" in outcome.message
    assert customer.balance_rial == 0


# ---------------------------------------------------------------------------
# Fan-out to the other reviewers
# ---------------------------------------------------------------------------
async def test_every_copy_is_edited_and_its_buttons_removed(session, customer, bound_notifier, recording_bot) -> None:
    reviewer = _reviewer("reviewer", 900_041)
    session.add(reviewer)
    receipt = await _deposit_receipt(session, customer)
    session.add_all(
        [
            ReceiptMessage(receipt_id=receipt.id, chat_id=111, message_id=1, is_media=False),
            ReceiptMessage(receipt_id=receipt.id, chat_id=222, message_id=2, is_media=False),
        ]
    )
    await session.flush()
    await session.refresh(receipt, ["messages"])
    await receipt_service.approve(session, receipt, reviewer)

    edited = await receipt_service.refresh_copies(session, receipt, reviewer=reviewer, decision="approved")

    assert edited == 2
    assert recording_bot.methods() == ["edit_message_text", "edit_message_text"]
    for _chat_id, kwargs in recording_bot.calls:
        assert kwargs["reply_markup"].inline_keyboard == []  # no stale buttons
        assert kwargs["text"].count("رسید جدید") == 1  # the header is not duplicated
    assert all(copy.edited for copy in receipt.messages)


# ---------------------------------------------------------------------------
# Telegram limits
# ---------------------------------------------------------------------------
def test_fit_trims_and_marks_the_cut() -> None:
    assert fit("short", 10) == "short"
    assert len(fit("x" * 5000, CAPTION_LIMIT)) == CAPTION_LIMIT
    assert fit("x" * 5000, CAPTION_LIMIT).endswith("…")


async def test_a_long_note_still_fits_a_media_caption(session, customer) -> None:
    receipt = await receipt_service.create(
        session,
        customer,
        purpose="deposit",
        amount_rial=1_000,
        media=ReceiptMedia.PHOTO,
        file_id="file-id",
        note="ی" * 3000,
    )
    await session.flush()

    as_caption = await receipt_service.build_staff_caption(session, receipt, limit=CAPTION_LIMIT)
    as_text = await receipt_service.build_staff_caption(session, receipt, limit=TEXT_LIMIT)

    assert len(as_caption) <= CAPTION_LIMIT
    assert len(as_text) <= TEXT_LIMIT
    assert receipt.code in as_caption


async def test_a_photo_receipt_is_broadcast_as_a_photo(session, customer, bound_notifier, recording_bot) -> None:
    """The production ``TypeError``: a media copy must reach the reviewer."""
    session.add(_reviewer("reviewer", 900_051))
    receipt = await receipt_service.create(
        session,
        customer,
        purpose="deposit",
        amount_rial=250_000,
        media=ReceiptMedia.PHOTO,
        file_id="photo-file-id",
    )
    await session.flush()

    delivered = await receipt_service.broadcast(session, receipt)
    await session.refresh(receipt, ["messages"])

    assert len(delivered) == 1
    kwargs = recording_bot.kwargs_of("send_photo")
    assert kwargs["photo"] == "photo-file-id"
    assert len(kwargs["caption"]) <= CAPTION_LIMIT
    assert receipt.messages[0].is_media is True
