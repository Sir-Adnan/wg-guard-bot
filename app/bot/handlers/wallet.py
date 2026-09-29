"""Wallet: balance, top-up via card-to-card, and the transaction ledger."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import MenuCB, NavCB, WalletCB
from app.bot.keyboards import KeyboardBuilder
from app.bot.menus import deposit_amounts, wallet_menu
from app.bot.states import ShopStates, WalletStates
from app.bot.utils import answer_callback, show
from app.core.jalali import jalali_datetime
from app.core.logging import get_logger
from app.core.money import format_amount, parse_user_amount, to_toman
from app.db.models import CardAccount, PaymentKind, User
from app.services.settings_store import app_settings, card_payments_enabled
from app.services.texts import texts
from app.services.users import user_service

log = get_logger(__name__)
router = Router(name="wallet")

KIND_TITLES: dict[str, str] = {
    PaymentKind.DEPOSIT.value: "شارژ کیف پول",
    PaymentKind.PURCHASE.value: "خرید سرویس",
    PaymentKind.REFUND.value: "بازگشت وجه",
    PaymentKind.ADJUSTMENT.value: "اصلاح توسط مدیر",
    PaymentKind.REFERRAL.value: "پاداش معرفی",
    PaymentKind.GIFT.value: "هدیه",
}


# ---------------------------------------------------------------------------
@router.callback_query(MenuCB.filter(F.action == "wallet"))
async def open_wallet(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    await answer_callback(callback)
    await _render_wallet(callback, session, user)


@router.callback_query(NavCB.filter(F.to == "wallet"))
async def open_wallet_nav(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    await answer_callback(callback)
    await _render_wallet(callback, session, user)


async def _render_wallet(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    if not app_settings.get_bool("payment.wallet_enabled", True):
        await show(callback, await texts.get("error.not_found", session))
        return
    await show(
        callback,
        await texts.get("wallet.title", session, balance=format_amount(user.balance_rial)),
        keyboard=await wallet_menu(session),
    )


# ---------------------------------------------------------------------------
# Top-up
# ---------------------------------------------------------------------------
@router.callback_query(WalletCB.filter(F.action == "deposit"))
async def choose_amount(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    await answer_callback(callback)
    if not card_payments_enabled():
        await show(callback, "⚠️ شارژ کیف پول در حال حاضر غیرفعال است.", keyboard=await wallet_menu(session))
        return
    await show(
        callback,
        await texts.get("wallet.choose_amount", session),
        keyboard=await deposit_amounts(session),
    )


@router.callback_query(WalletCB.filter(F.action == "custom"))
async def custom_amount(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await answer_callback(callback)
    await state.set_state(WalletStates.entering_amount)
    minimum = format_amount(app_settings.get_int("shop.min_deposit_rial", 500_000))
    await show(callback, await texts.get("wallet.custom_amount", session, min=minimum))


@router.message(WalletStates.entering_amount, F.text)
async def receive_custom_amount(message: Message, session: AsyncSession, state: FSMContext) -> None:
    amount = parse_user_amount(message.text or "")
    minimum = app_settings.get_int("shop.min_deposit_rial", 500_000)
    if amount <= 0:
        await show(message, "⚠️ عدد وارد شده معتبر نیست. دوباره تلاش کنید.")
        return
    if amount < minimum:
        await show(message, await texts.get("wallet.min_amount", session, min=format_amount(minimum)))
        return
    await state.clear()
    await _request_deposit(message, session, state, amount)


@router.callback_query(WalletCB.filter(F.action == "confirm"))
async def confirm_preset(
    callback: CallbackQuery, callback_data: WalletCB, session: AsyncSession, state: FSMContext
) -> None:
    await answer_callback(callback)
    amount = int(callback_data.amount_rial or 0)
    if amount <= 0:
        await show(callback, "⚠️ مبلغ نامعتبر است.")
        return
    await _request_deposit(callback, session, state, amount)


async def _request_deposit(event, session: AsyncSession, state: FSMContext, amount_rial: int) -> None:
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
        await show(event, "⚠️ در حال حاضر شماره کارتی برای واریز ثبت نشده است. با پشتیبانی تماس بگیرید.")
        return

    code = f"DEP-{to_toman(amount_rial)}"
    # Both purchase and deposit receipts are collected in the same FSM state;
    # ``purpose`` in the state data tells the intake handler which one this is.
    await state.set_state(ShopStates.awaiting_receipt)
    await state.update_data(purpose="deposit", amount_rial=amount_rial, dep_ref=code)

    body = await texts.get("wallet.deposit_invoice", session, order=code, amount=format_amount(amount_rial))
    body += "\n\n" + await texts.get(
        "buy.card_info",
        session,
        card=card.card_number,
        holder=card.holder_name,
        bank=card.bank_name or "—",
        amount=format_amount(amount_rial),
        order=code,
    )
    if card.instructions:
        body += "\n\n" + card.instructions
    body += "\n\n" + await texts.get("receipt.ask_photo", session)
    await show(event, body)


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
@router.callback_query(WalletCB.filter(F.action == "history"))
async def history(callback: CallbackQuery, callback_data: WalletCB, session: AsyncSession, user: User) -> None:
    await answer_callback(callback)
    page = max(callback_data.page or 1, 1)
    per_page = 8
    entries = await user_service.history(session, user.id, limit=per_page + 1, offset=(page - 1) * per_page)
    if not entries:
        await show(callback, await texts.get("wallet.empty_history", session), keyboard=await wallet_menu(session))
        return

    has_next = len(entries) > per_page
    lines = [await texts.get("wallet.history_title", session, count=str(len(entries[:per_page]))), ""]
    for entry in entries[:per_page]:
        lines.append(
            await texts.get(
                "wallet.history_row",
                session,
                date=jalali_datetime(entry.created_at),
                title=KIND_TITLES.get(entry.kind.value, entry.kind.value),
                amount=("+" if entry.amount_rial >= 0 else "−") + format_amount(abs(entry.amount_rial)),
                balance=format_amount(entry.balance_after_rial),
            )
        )

    kb = KeyboardBuilder(session=session, columns=2)
    if page > 1:
        await kb.add("common.prev_page", callback=WalletCB(action="history", page=page - 1).pack())
    if has_next:
        await kb.add("common.next_page", callback=WalletCB(action="history", page=page + 1).pack())
    kb.row()
    await kb.add("menu.back", callback=NavCB(to="wallet").pack())

    await show(callback, "\n".join(lines), keyboard=kb.build())


@router.message(WalletStates.choosing_amount, F.text)
async def deposit_awaiting_receipt(message: Message, session: AsyncSession, state: FSMContext) -> None:
    """A reminder if the user types instead of sending a receipt."""
    data = await state.get_data()
    if data.get("purpose") == "deposit":
        await show(message, await texts.get("receipt.ask_photo", session))
    else:
        await state.clear()
        await show(message, await texts.get("error.expired_action", session))


__all__ = ["KIND_TITLES", "router"]
