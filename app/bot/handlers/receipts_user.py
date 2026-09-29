"""Customer side of the card-to-card receipt flow.

A receipt can be a photo, a screenshot sent as a document, or plain text (some
banks only give a text confirmation).  We accept all three and normalise them
into one :class:`~app.db.models.Receipt` row before fanning it out to reviewers.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import KeyboardBuilder
from app.bot.states import ShopStates
from app.bot.utils import answer_callback, show
from app.core.errors import AppError
from app.core.logging import get_logger
from app.db.models import Order, OrderStatus, ReceiptMedia, User
from app.services.receipts import receipt_service
from app.services.settings_store import app_settings
from app.services.texts import html_escape, texts

log = get_logger(__name__)
router = Router(name="receipts")

ACCEPTED_DOCUMENT_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/webp"}


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
@router.message(ShopStates.awaiting_receipt, F.photo)
async def receipt_photo(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    await _store(
        message,
        session,
        user,
        state,
        media=ReceiptMedia.PHOTO,
        file_id=message.photo[-1].file_id,
    )


@router.message(ShopStates.awaiting_receipt, F.document)
async def receipt_document(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    document = message.document
    if document is None:
        return
    max_bytes = app_settings.get_int("receipt_max_file_mb", 8) * 1024 * 1024
    if document.file_size and document.file_size > max_bytes:
        await show(
            message,
            await texts.get("receipt.too_large", session, max_mb=str(app_settings.get_int("receipt_max_file_mb", 8))),
        )
        return
    if document.mime_type and document.mime_type not in ACCEPTED_DOCUMENT_TYPES:
        await show(message, await texts.get("receipt.invalid_media", session))
        return
    await _store(
        message,
        session,
        user,
        state,
        media=ReceiptMedia.DOCUMENT,
        file_id=document.file_id,
        note=document.file_name,
    )


@router.message(ShopStates.awaiting_receipt, F.text)
async def receipt_text(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    await _store(message, session, user, state, media=ReceiptMedia.TEXT, note=text)


@router.message(ShopStates.awaiting_receipt)
async def receipt_unsupported(message: Message, session: AsyncSession) -> None:
    await show(message, await texts.get("receipt.invalid_media", session))


# ---------------------------------------------------------------------------
async def _store(
    message: Message,
    session: AsyncSession,
    user: User,
    state: FSMContext,
    *,
    media: ReceiptMedia,
    file_id: str | None = None,
    note: str | None = None,
) -> None:
    data = await state.get_data()
    purpose = str(data.get("purpose") or "purchase")

    # -- wallet top-up ----------------------------------------------------
    if purpose == "deposit":
        amount_rial = int(data.get("amount_rial") or 0)
        if amount_rial <= 0:
            await state.clear()
            await show(message, await texts.get("error.expired_action", session))
            return
        await _persist(
            message,
            session,
            user,
            state,
            purpose="deposit",
            amount_rial=amount_rial,
            media=media,
            file_id=file_id,
            note=note,
            order=None,
        )
        return

    # -- order payment ----------------------------------------------------
    order: Order | None = None
    order_id = int(data.get("order_id") or 0)
    if order_id:
        order = await session.get(Order, order_id)

    if order is None or order.user_id != user.id:
        await state.clear()
        await show(message, await texts.get("error.expired_action", session))
        return
    if order.status not in (OrderStatus.PENDING_PAYMENT, OrderStatus.AWAITING_REVIEW):
        await state.clear()
        await show(message, await texts.get("error.expired_action", session))
        return

    await _persist(
        message,
        session,
        user,
        state,
        purpose="purchase",
        amount_rial=order.payable_rial,
        media=media,
        file_id=file_id,
        note=note,
        order=order,
    )


async def _persist(
    message: Message,
    session: AsyncSession,
    user: User,
    state: FSMContext,
    *,
    purpose: str,
    amount_rial: int,
    media: ReceiptMedia,
    file_id: str | None,
    note: str | None,
    order: Order | None,
) -> None:
    tracking = None
    if app_settings.get_bool("payment.receipt_requires_tracking", False):
        tracking = (message.caption or "").strip() or None

    try:
        receipt = await receipt_service.create(
            session,
            user,
            purpose=purpose,
            amount_rial=amount_rial,
            media=media,
            file_id=file_id,
            chat_id=message.chat.id,
            message_id=message.message_id,
            note=note,
            tracking_code=tracking,
            order=order,
        )
    except AppError as exc:
        await state.clear()
        await show(message, f"⚠️ {html_escape(exc.message)}")
        return

    await state.clear()
    try:
        await receipt_service.broadcast(session, receipt)
    except Exception as exc:  # pragma: no cover - notification failure must not lose the receipt
        log.exception("Receipt broadcast failed: %s", exc)

    await show(
        message,
        await texts.get("receipt.received", session, code=receipt.code),
        keyboard=await _after_receipt_keyboard(session),
    )


async def _after_receipt_keyboard(session: AsyncSession):
    from app.bot.callbacks import MenuCB, NavCB

    kb = KeyboardBuilder(session=session, columns=1)
    await kb.add("menu.my_services", callback=MenuCB(action="services").pack())
    await kb.add("menu.support", callback=MenuCB(action="support").pack())
    kb.row()
    await kb.add("menu.main", callback=NavCB(to="main").pack())
    return kb.build()


# ---------------------------------------------------------------------------
# Staff actions that need a text reply from the customer
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("rc:message"))
async def staff_wants_message(callback: CallbackQuery, session: AsyncSession) -> None:
    await answer_callback(callback, "برای پاسخ به کاربر از بخش پشتیبانی پنل یا دکمه «پیام به کاربر» استفاده کنید.")


__all__ = ["router"]
