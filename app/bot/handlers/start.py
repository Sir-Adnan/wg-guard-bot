"""Entry point: /start, the main menu, profile, referral, rules and channels."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import MenuCB, NavCB, ReferralCB
from app.bot.keyboards import KeyboardBuilder
from app.bot.menus import default_main_menu, wallet_menu
from app.bot.utils import answer_callback, show
from app.core.config import settings
from app.core.jalali import jalali_date
from app.core.logging import get_logger
from app.core.money import format_amount
from app.db.models import User
from app.services.membership import membership
from app.services.notifications import notifier
from app.services.settings_store import app_settings, shop_name
from app.services.texts import html_escape, texts
from app.services.users import user_service

log = get_logger(__name__)
router = Router(name="start")


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
@router.message(CommandStart(deep_link=True))
async def start_with_payload(
    message: Message, command: CommandObject, session: AsyncSession, user: User, staff=None
) -> None:
    payload = (command.args or "").strip()
    if payload.startswith("ref") and len(payload) > 3:
        code = payload[3:]
        if await user_service.apply_referral(session, user, code):
            await show(message, await texts.get("start.joined_ok", session))
    elif payload.startswith("plan") and payload[4:].isdigit():
        await _send_main(message, session, user, staff)
        await _open_plan(message, session, int(payload[4:]))
        return
    await _send_main(message, session, user, staff)


@router.message(CommandStart())
async def start_plain(message: Message, session: AsyncSession, user: User, staff=None) -> None:
    await _send_main(message, session, user, staff)


@router.message(Command("menu"))
async def command_menu(message: Message, session: AsyncSession, user: User, staff=None) -> None:
    await _send_main(message, session, user, staff)


@router.message(Command("help"))
async def command_help(message: Message, session: AsyncSession) -> None:
    await show(message, await texts.get("start.help", session))


@router.message(Command("rules"))
async def command_rules(message: Message, session: AsyncSession) -> None:
    """``/rules`` is advertised in the bot command menu — keep it answering."""
    await show(message, await texts.get("rules.text", session))


@router.callback_query(NavCB.filter(F.to == "main"))
async def nav_main(
    callback: CallbackQuery, callback_data: NavCB, session: AsyncSession, user: User, staff=None
) -> None:
    await answer_callback(callback)
    await show(
        callback, await _welcome_text(session, user), keyboard=await default_main_menu(session, is_staff=bool(staff))
    )


@router.callback_query(NavCB.filter(F.to == "wallet"))
async def nav_wallet(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    await answer_callback(callback)
    await show(
        callback,
        await texts.get("wallet.title", session, balance=format_amount(user.balance_rial)),
        keyboard=await wallet_menu(session),
    )


@router.callback_query(MenuCB.filter(F.action == "profile"))
async def menu_profile(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    await answer_callback(callback)
    stats = await user_service.stats(session, user)
    body = await texts.get(
        "profile.title",
        session,
        id=f"<code>{user.telegram_id}</code>",
        name=html_escape(user.display_name),
        balance=format_amount(user.balance_rial),
        joined=jalali_date(user.created_at),
        orders=str(stats.orders_total),
        services=str(stats.services_active),
    )
    kb = KeyboardBuilder(session=session, columns=1)
    await kb.add("menu.referral", callback=MenuCB(action="referral").pack())
    await kb.add("menu.rules", callback=MenuCB(action="rules").pack())
    kb.row()
    await kb.add("menu.main", callback=NavCB(to="main").pack())
    await show(callback, body, keyboard=kb.build())


@router.callback_query(MenuCB.filter(F.action == "referral"))
async def menu_referral(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    await answer_callback(callback)
    if not app_settings.get_bool("shop.referral_enabled", True):
        await show(callback, await texts.get("error.not_found", session))
        return

    from app.services.notifications import notifier as _notifier

    username = _notifier.username
    if username is None:
        try:
            username = (await _notifier.bot.get_me()).username
        except Exception:  # pragma: no cover - bot not running
            await show(callback, await texts.get("error.generic", session))
            return
    link = f"https://t.me/{username}?start=ref{user.referral_code}"
    percent = app_settings.get_float("shop.referral_percent", 0.0)
    stats = await user_service.stats(session, user)

    body = await texts.get("profile.referral_link", session, link=link, percent=f"{percent:g}")
    body += "\n\n" + await texts.get(
        "profile.referral_stats",
        session,
        count=str(stats.referrals),
        earnings=format_amount(user.referral_earnings_rial),
    )
    await show(callback, body)


@router.callback_query(MenuCB.filter(F.action == "rules"))
async def menu_rules(callback: CallbackQuery, session: AsyncSession) -> None:
    await answer_callback(callback)
    await show(callback, await texts.get("rules.text", session))


@router.callback_query(MenuCB.filter(F.action == "channels"))
async def menu_channels(callback: CallbackQuery, session: AsyncSession) -> None:
    await answer_callback(callback)
    channels = await membership.active_channels(session)
    if not channels:
        await show(callback, await texts.get("error.not_found", session))
        return
    kb = KeyboardBuilder(session=session, columns=1)
    for channel in channels:
        if channel.invite_link:
            await kb.add("menu.join", url=channel.invite_link, text=channel.title or None)
    kb.row()
    await kb.add("menu.main", callback=NavCB(to="main").pack())
    await show(
        callback,
        await texts.get(
            "start.must_join", session, channels="\n" + "\n".join(f"• {c.title or c.chat_id}" for c in channels) + "\n"
        ),
        keyboard=kb.build(),
    )


@router.callback_query(MenuCB.filter(F.action == "check_join"))
async def check_join(callback: CallbackQuery, session: AsyncSession, user: User, staff=None) -> None:
    status = await membership.check(session, user.telegram_id, force=True)
    await membership.invalidate(user.telegram_id)
    if not status.ok:
        await callback.answer(
            await texts.get("start.not_joined", session, channels=status.missing_titles), show_alert=True
        )
        if callback.message is not None:
            await notifier.edit(
                callback.message.chat.id,
                callback.message.message_id,
                await membership.join_prompt(session, status.missing),
                keyboard=await membership.join_keyboard(session, status.missing),
            )
        return

    await answer_callback(callback)
    await show(
        callback,
        await _welcome_text(session, user),
        keyboard=await default_main_menu(session, is_staff=bool(staff)),
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def _send_main(message: Message, session: AsyncSession, user: User, staff=None) -> None:
    status = await membership.check(session, user.telegram_id)
    if not status.ok:
        from app.services.notifications import notifier

        await notifier.send(
            message.chat.id,
            await membership.join_prompt(session, status.missing),
            keyboard=await membership.join_keyboard(session, status.missing),
        )
        return
    await show(
        message,
        await _welcome_text(session, user),
        keyboard=await default_main_menu(session, is_staff=bool(staff)),
    )


async def _welcome_text(session: AsyncSession, user: User) -> str:
    return await texts.get(
        "start.welcome",
        session,
        name=html_escape(user.first_name or "دوست عزیز"),
        shop=html_escape(shop_name()),
    )


async def _open_plan(message: Message, session: AsyncSession, plan_id: int) -> None:
    """Deep-link straight into a plan (`?start=plan12`)."""
    from app.services.catalog import catalog

    try:
        plan = await catalog.get(session, plan_id, require_active=True)
    except Exception:
        return
    from app.bot.handlers.shop import render_plan

    text, keyboard = await render_plan(session, plan)
    await show(message, text, keyboard=keyboard)


@router.message(Command("id"))
async def command_id(message: Message, user: User, staff=None) -> None:
    rows = [f"<b>شناسه تلگرام شما:</b> <code>{user.telegram_id}</code>"]
    if staff is not None:
        rows.append(f"<b>نقش:</b> {staff.role.value}")
    rows.append(f"<b>کد معرف شما:</b> <code>{user.referral_code or '—'}</code>")
    await message.answer("\n".join(rows))


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery) -> None:
    await answer_callback(callback)


@router.message(Command("version"))
async def command_version(message: Message) -> None:
    from app import __version__

    await message.answer(f"<b>WG-Guard Bot</b> نسخه <code>{__version__}</code>\nمحیط: <code>{settings.env}</code>")


@router.message(Command("cancel"))
async def command_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await show(message, await texts.get("common.canceled"))


@router.callback_query(ReferralCB.filter())
async def referral_actions(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    await answer_callback(callback)
    await menu_referral(callback, session, user)


__all__ = ["router"]
