"""The physical (reply) keyboard's button presses.

A reply-keyboard press is a plain text message, so this router has to be the one
place that decides "is this text a menu button?" — and it must be conservative:
it only looks at messages with **no FSM state** (a customer typing a discount
code or a receipt note is mid-flow, not pressing a menu), and it only accepts an
**exact** label.

The screens themselves are the inline menu's: each entry in :data:`_SCREENS`
registers the same function the inline button calls (see
:mod:`app.bot.reply_menu`), so there is exactly one implementation per screen.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import reply_menu
from app.bot.handlers import gift as gift_handlers
from app.bot.handlers import guides as guide_handlers
from app.bot.handlers import services as service_handlers
from app.bot.handlers import shop as shop_handlers
from app.bot.handlers import start as start_handlers
from app.bot.handlers import support as support_handlers
from app.bot.handlers import test_service as test_handlers
from app.bot.handlers import wallet as wallet_handlers
from app.bot.handlers.admin import panel as admin_panel
from app.bot.utils import show
from app.core.logging import get_logger
from app.db.models import Staff, User
from app.services.notifications import notifier
from app.services.texts import texts

log = get_logger(__name__)
router = Router(name="reply_menu")


# ---------------------------------------------------------------------------
# One screen per action — all of them shared with the inline menu
# ---------------------------------------------------------------------------
@reply_menu.screen("main")
async def _main(event: Message | CallbackQuery, session: AsyncSession, user: User, staff: Staff | None = None) -> None:
    await start_handlers._send_main(event, session, user, staff)


@reply_menu.screen("buy")
async def _buy(event: Message | CallbackQuery, session: AsyncSession, user: User, staff: Staff | None = None) -> None:
    await shop_handlers.render_catalog(event, session, action="home")


@reply_menu.screen("services")
async def _services(
    event: Message | CallbackQuery, session: AsyncSession, user: User, staff: Staff | None = None
) -> None:
    await service_handlers._render_list(event, session, user, page=1)


@reply_menu.screen("wallet")
async def _wallet(
    event: Message | CallbackQuery, session: AsyncSession, user: User, staff: Staff | None = None
) -> None:
    await wallet_handlers._render_wallet(event, session, user)


@reply_menu.screen("test")
async def _test(event: Message | CallbackQuery, session: AsyncSession, user: User, staff: Staff | None = None) -> None:
    await test_handlers._render(event, session, user)


@reply_menu.screen("support")
async def _support(
    event: Message | CallbackQuery, session: AsyncSession, user: User, staff: Staff | None = None
) -> None:
    await support_handlers.open_support(event, session)


@reply_menu.screen("guides")
async def _guides(
    event: Message | CallbackQuery, session: AsyncSession, user: User, staff: Staff | None = None
) -> None:
    await guide_handlers._render_sections(event, session)


@reply_menu.screen("gift")
async def _gift(event: Message | CallbackQuery, session: AsyncSession, user: User, staff: Staff | None = None) -> None:
    await gift_handlers.open_gift(event, session, user)


@reply_menu.screen("profile")
async def _profile(
    event: Message | CallbackQuery, session: AsyncSession, user: User, staff: Staff | None = None
) -> None:
    await start_handlers.menu_profile(event, session, user)


@reply_menu.screen("referral")
async def _referral(
    event: Message | CallbackQuery, session: AsyncSession, user: User, staff: Staff | None = None
) -> None:
    await start_handlers.menu_referral(event, session, user)


@reply_menu.screen("rules")
async def _rules(event: Message | CallbackQuery, session: AsyncSession, user: User, staff: Staff | None = None) -> None:
    await start_handlers.menu_rules(event, session)


@reply_menu.screen("channels")
async def _channels(
    event: Message | CallbackQuery, session: AsyncSession, user: User, staff: Staff | None = None
) -> None:
    await start_handlers.menu_channels(event, session)


@reply_menu.screen("admin")
async def _admin(event: Message | CallbackQuery, session: AsyncSession, user: User, staff: Staff | None = None) -> None:
    if staff is None:  # pragma: no cover - only offered to staff
        await show(event, await texts.get("error.not_found", session))
        return
    await admin_panel.admin_menu_cb(event, session, staff)


# ---------------------------------------------------------------------------
# Showing and hiding the keyboard
# ---------------------------------------------------------------------------
async def send_keyboard(message: Message, session: AsyncSession, *, is_staff: bool = False) -> bool:
    """Send the keyboard under the hint line; ``False`` when it is switched off.

    A separate message on purpose: Telegram carries **one** ``reply_markup`` per
    message, so a screen cannot hold both its inline buttons and the reply
    keyboard.
    """
    markup = await reply_menu.build(session, is_staff=is_staff)
    if markup is None:
        return False
    await notifier.send(message.chat.id, await texts.get(reply_menu.HINT_KEY, session), reply_keyboard=markup)
    return True


@router.message(Command("keyboard"))
async def command_keyboard(
    message: Message, session: AsyncSession, staff: Staff | None = None, state: FSMContext | None = None
) -> None:
    """``/keyboard`` sends it, ``/keyboard off`` takes it away."""
    if state is not None:
        await state.clear()

    argument = (message.text or "").split(maxsplit=1)
    wants_off = len(argument) > 1 and argument[1].strip().lower() in {"off", "خاموش", "حذف"}

    if wants_off:
        await notifier.send(
            message.chat.id,
            await texts.get("menu.reply_keyboard_removed", session),
            reply_keyboard=reply_menu.removal(),
        )
        return

    if not await send_keyboard(message, session, is_staff=staff is not None):
        await show(message, await texts.get("menu.reply_keyboard_disabled", session))


# ---------------------------------------------------------------------------
# A press
# ---------------------------------------------------------------------------
@router.message(StateFilter(None), F.text)
async def reply_button(message: Message, session: AsyncSession, user: User, staff: Staff | None = None) -> None:
    """Route an exact menu label; anything else belongs to the catch-all.

    Deliberately *not* gated on the setting: a keyboard the owner switched off
    may still be sitting on a customer's phone, and its buttons are exactly the
    inline menu's actions — so pressing one keeps working instead of doing
    nothing at all.  The setting controls whether we *send* the keyboard.
    """
    action = await reply_menu.match(session, message.text or "", is_staff=staff is not None)
    if action is None:
        return

    log.debug("Reply keyboard press %r → %s", message.text, action)
    await reply_menu.open(action, message, session, user, staff)


__all__ = ["router", "send_keyboard"]
