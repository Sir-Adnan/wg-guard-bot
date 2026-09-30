"""Bot construction: session, storage, dispatcher, middlewares and routers."""

from __future__ import annotations

from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeAllChatAdministrators, BotCommandScopeChat, BotCommandScopeDefault

from app.bot.handlers import build_root_router, detach_router
from app.bot.handlers import errors as error_handlers
from app.bot.middlewares import (
    ContextMiddleware,
    DatabaseMiddleware,
    MembershipMiddleware,
    ThrottleMiddleware,
)
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

USER_COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "شروع و منوی اصلی"),
    ("menu", "منوی اصلی"),
    ("help", "راهنما"),
    ("rules", "قوانین و پشتیبانی"),
    ("keyboard", "نمایش یا حذف کیبورد پایین صفحه"),
    ("cancel", "لغو عملیات جاری"),
)

STAFF_EXTRA_COMMANDS: tuple[tuple[str, str], ...] = (
    ("admin", "پنل مدیریت"),
    ("version", "نسخه ربات"),
)


def create_bot() -> Bot:
    """Build the Bot with the right API server, proxy and defaults."""
    session: AiohttpSession | None = None
    if settings.bot_api_server or settings.bot_proxy:
        options: dict[str, object] = {}
        if settings.bot_api_server:
            options["api"] = settings.bot_api_server
        if settings.bot_proxy:
            options["proxy"] = settings.bot_proxy
        session = AiohttpSession(**options)  # type: ignore[arg-type]
    return Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def create_storage() -> BaseStorage:
    """Redis-backed FSM when available, in-memory otherwise."""
    if not settings.redis_url:
        return MemoryStorage()
    try:
        import redis.asyncio as aioredis
        from aiogram.fsm.storage.redis import RedisStorage

        client = aioredis.from_url(settings.redis_url)
        await client.ping()
        log.info("FSM storage: Redis")
        return RedisStorage(redis=client)
    except Exception as exc:
        log.warning("Redis FSM storage unavailable (%s) — using in-memory state", exc)
        return MemoryStorage()


#: The process-wide dispatcher.  aiogram routers are single-parent singletons,
#: so a second dispatcher would either raise or silently strip the first one of
#: its handlers.  Building once and handing the same instance back is the only
#: behaviour that cannot surprise a caller.
_dispatcher: Dispatcher | None = None


def create_dispatcher(storage: BaseStorage | None = None) -> Dispatcher:
    """Return the process dispatcher, assembling it on first use.

    Safe to call from anywhere and any number of times: subsequent calls return
    the same instance rather than rebuilding the router tree.  Tests that need a
    clean tree call :func:`reset_dispatcher` first.
    """
    global _dispatcher
    if _dispatcher is not None:
        if storage is not None and _dispatcher.storage is not storage:
            log.warning("create_dispatcher() ignored a new storage: the dispatcher already exists")
        return _dispatcher

    dispatcher = Dispatcher(storage=storage or MemoryStorage())

    # -- middlewares -------------------------------------------------------
    dispatcher.update.outer_middleware(DatabaseMiddleware())

    context = ContextMiddleware()
    membership = MembershipMiddleware()
    throttle = ThrottleMiddleware()

    for observer in (dispatcher.message, dispatcher.callback_query):
        observer.middleware(context)
        observer.middleware(membership)
        observer.middleware(throttle)

    # -- routers -----------------------------------------------------------
    # ``build_root_router`` detaches the module-level routers from any previous
    # root first; aiogram refuses to attach one router to two parents.
    dispatcher.include_router(build_root_router())
    detach_router(error_handlers.router)
    dispatcher.include_router(error_handlers.router)

    _dispatcher = dispatcher
    return dispatcher


def reset_dispatcher() -> None:
    """Drop the cached dispatcher so the next call rebuilds the router tree.

    For tests and for an embedded restart.  The old dispatcher is left empty on
    purpose — its routers now belong to the new tree.
    """
    global _dispatcher
    _dispatcher = None


async def setup_bot_commands(bot: Bot) -> None:
    """Publish the command list (staff get two extra entries)."""
    user_commands = [BotCommand(command=name, description=desc) for name, desc in USER_COMMANDS]
    await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())

    staff_commands = [
        BotCommand(command=name, description=desc) for name, desc in (*USER_COMMANDS, *STAFF_EXTRA_COMMANDS)
    ]
    for telegram_id in settings.all_staff_ids:
        try:
            await bot.set_my_commands(staff_commands, scope=BotCommandScopeChat(chat_id=telegram_id))
        except Exception as exc:  # pragma: no cover - staff may not have started the bot yet
            log.debug("Could not set staff commands for %s: %s", telegram_id, exc)

    with suppress(Exception):  # pragma: no cover
        await bot.set_my_commands(user_commands, scope=BotCommandScopeAllChatAdministrators())


async def announce_startup(bot: Bot) -> None:
    """Tell the operators the bot is up (useful on a VPS)."""
    try:
        me = await bot.get_me()
    except Exception as exc:  # pragma: no cover
        log.error("getMe failed — check BOT_TOKEN: %s", exc)
        return

    log.info("Bot online: @%s (id=%s)", me.username, me.id)
    for telegram_id in settings.all_staff_ids:
        try:
            await bot.send_message(
                telegram_id,
                f"✅ <b>ربات با موفقیت اجرا شد.</b>\n\nنام: @{me.username}\nمحیط: <code>{settings.env}</code>",
                disable_notification=True,
            )
        except Exception:  # pragma: no cover
            continue


__all__ = [
    "STAFF_EXTRA_COMMANDS",
    "USER_COMMANDS",
    "announce_startup",
    "create_bot",
    "create_dispatcher",
    "create_storage",
    "setup_bot_commands",
]
