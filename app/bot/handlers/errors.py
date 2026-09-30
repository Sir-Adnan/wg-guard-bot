"""Global error handling, the catch-all reply and the stale-button net.

Registered last on the dispatcher so it only sees updates nothing else wanted.
That is exactly why the callback catch-all belongs here: a button whose payload
no handler answers (an old message from before an update, or a menu the owner
reconfigured) is otherwise **silently ignored** — Telegram keeps the little
clock spinning on the button and the customer assumes the bot is broken.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ErrorEvent, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.menus import default_main_menu
from app.bot.utils import alert_text, show
from app.core.errors import AppError
from app.core.logging import get_logger
from app.db.models import User
from app.services.notifications import notifier
from app.services.texts import html_escape, texts

log = get_logger(__name__)
router = Router(name="errors")


@router.errors()
async def on_error(event: ErrorEvent) -> bool:
    """Log, persist and (for staff-visible errors) surface a friendly message."""
    exception = event.exception
    update = event.update

    if isinstance(exception, AppError):
        log.warning("Handled AppError: %s", exception.message)
        await notifier.record_event("warning", exception.message, source="handler")  # type: ignore[arg-type]
    else:
        log.exception("Unhandled exception while processing an update", exc_info=exception)
        await notifier.report_error(exception, source="update")

    session: AsyncSession | None = None
    try:
        from app.db.session import session_scope

        async with session_scope() as own_session:
            session = own_session
            body = await texts.get("error.generic", session)
    except Exception:  # pragma: no cover - fall back to a static string
        body = "خطای غیرمنتظرهای رخ داد. لطفاً دوباره تلاش کنید."

    try:
        message = update.message or (update.callback_query.message if update.callback_query else None)
        if isinstance(message, Message):
            await notifier.send(message.chat.id, body)
        elif update.callback_query is not None:
            await update.callback_query.answer("خطایی رخ داد. دوباره تلاش کنید.", show_alert=True)
    except Exception:  # pragma: no cover
        pass

    return True


@router.message()
async def fallback(message: Message, session: AsyncSession, user: User, staff=None) -> None:
    """Anything the user types outside a flow gets a helpful nudge."""
    hint = "متوجه نشدم 🤔\n\nبرای ادامه از منوی زیر استفاده کنید. اگر سؤالی دارید، بخش «پشتیبانی» در خدمت شماست."
    body = await texts.get("common.back_menu_hint", session)
    await notifier.send(
        message.chat.id,
        f"{hint}\n\n<i>{html_escape(body)}</i>",
        keyboard=await default_main_menu(session, is_staff=bool(staff)),
    )


@router.callback_query()
async def stale_button(
    callback: CallbackQuery,
    session: AsyncSession,
    state: FSMContext,
    user: User,
    staff=None,
) -> None:
    """A button nothing answers: say so and hand the customer the main menu.

    Also clears the FSM state — a stale button usually means the customer
    wandered off mid-flow, and leaving them in ``awaiting_receipt`` would make
    their next message disappear into a handler they are not expecting.
    """
    log.info("Unanswered callback data: %r", callback.data)
    await state.clear()
    body = await texts.get("error.expired_action", session)
    await callback.answer(alert_text(body), show_alert=True)
    await show(callback, body, keyboard=await default_main_menu(session, is_staff=bool(staff)))


__all__ = ["router"]
