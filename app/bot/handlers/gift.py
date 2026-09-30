"""Gift / redeem codes (کد هدیه)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import GiftCB, MenuCB
from app.bot.menus import gift_menu
from app.bot.states import ShopStates
from app.bot.utils import answer_callback, show
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.money import format_amount
from app.db.models import User
from app.services.gifts import gifts
from app.services.notifications import notifier
from app.services.settings_store import app_settings
from app.services.texts import html_escape, texts

log = get_logger(__name__)
router = Router(name="gift")


def enabled() -> bool:
    # The key must be the one the admin panel writes (``shop.gift_enabled``).
    # A key that is not a SettingSpec can never be changed by the operator, so
    # the fallback below would silently win forever.
    return app_settings.get_bool("shop.gift_enabled", True)


@router.callback_query(MenuCB.filter(F.action == "gift"))
async def open_gift(event: Message | CallbackQuery, session: AsyncSession, user: User) -> None:
    await answer_callback(event)
    if not enabled():
        await show(event, await texts.get("gift.disabled", session))
        return
    await show(
        event,
        await texts.get("gift.title", session),
        keyboard=await gift_menu(session, available=True),
    )


@router.callback_query(GiftCB.filter(F.action == "redeem"))
async def ask_code(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await answer_callback(callback)
    await state.set_state(ShopStates.entering_gift_code)
    await show(callback, await texts.get("gift.ask", session))


@router.message(ShopStates.entering_gift_code, F.text)
async def redeem_code(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    await state.clear()
    if not enabled():
        await show(message, await texts.get("gift.disabled", session))
        return
    await _do_redeem(message, session, user, (message.text or "").strip())


async def _do_redeem(message: Message, session: AsyncSession, user: User, code: str) -> None:
    try:
        result = await gifts.redeem(session, code, user)
    except AppError as exc:
        await show(message, f"⚠️ {html_escape(exc.message)}")
        return

    if result.plan is not None:
        await show(message, await texts.get("gift.success_plan", session, plan=html_escape(result.plan.name)))
        try:
            order = await gifts.create_free_order(session, result, user)
        except AppError as exc:
            await show(message, f"⚠️ {html_escape(exc.message)}")
            return
        from app.bot.handlers.purchase import finalize_order

        await finalize_order(message, session, order)
        return

    await show(
        message,
        await texts.get(
            "gift.success_wallet",
            session,
            amount=format_amount(result.amount_rial),
            balance=format_amount(user.balance_rial),
        ),
        keyboard=await gift_menu(session),
    )
    await notifier.to_admins(
        session,
        "🎁 کد هدیه استفاده شد\n\n"
        f"کاربر: {user.mention}\n"
        f"کد: <code>{result.code.code}</code>\n"
        f"مبلغ: {format_amount(result.amount_rial)}",
        disable_notification=True,
    )


__all__ = ["enabled", "router"]
