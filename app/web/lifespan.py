"""Application lifespan: database, cache, Telegram bot and the scheduler."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field

from aiogram import Bot, Dispatcher
from fastapi import FastAPI

from app.bot.setup import (
    announce_startup,
    create_bot,
    create_dispatcher,
    create_storage,
    setup_bot_commands,
)
from app.core.cache import cache
from app.core.config import BASE_DIR, settings
from app.core.logging import get_logger, setup_logging
from app.db.session import dispose_engine, ping, session_scope
from app.panels.manager import panel_manager
from app.services.appearance import appearance
from app.services.bootstrap import seed_panel_owner
from app.services.notifications import notifier
from app.services.settings_store import app_settings
from app.services.texts import texts
from app.workers.scheduler import scheduler

log = get_logger(__name__)


@dataclass
class Runtime:
    """Handles owned by the lifespan, exposed for diagnostics and the webhook."""

    bot: Bot | None = None
    dispatcher: Dispatcher | None = None
    polling_task: asyncio.Task[None] | None = None
    storage: object | None = None
    warnings: list[str] = field(default_factory=list)


async def _warm_caches() -> None:
    async with session_scope() as session:
        await app_settings.load(session, force=True)
        await texts.load(session, force=True)
        await appearance.load(session, force=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Boot every subsystem, then tear them down cleanly."""
    setup_logging(settings.log_level, log_dir=BASE_DIR / "logs", json_lines=settings.is_production)
    log.info("Starting %s (env=%s)", settings.app_name, settings.env)

    runtime = Runtime()
    app.state.runtime = runtime

    # -- infrastructure ----------------------------------------------------
    await cache.connect_redis(settings.redis_url)

    if not await ping():
        message = "اتصال به دیتابیس برقرار نشد. مهاجرت‌ها را اجرا کنید: alembic upgrade head"
        log.error(message)
        runtime.warnings.append(message)
    else:
        # The panel has to be usable before the first bot update ever arrives, so the
        # owner account is committed here, in its own session — never inside an
        # update, where a failing handler would roll it back.
        try:
            owner = await seed_panel_owner()
            log.info("Panel login ready: %r", owner.login)
        except Exception as exc:
            message = f"Seeding the panel owner account failed: {exc}"
            log.exception(message)
            runtime.warnings.append(message)

    # -- telegram ----------------------------------------------------------
    if settings.bot_token:
        try:
            runtime.storage = await create_storage()
            runtime.bot = create_bot()
            notifier.bind(runtime.bot)
            runtime.dispatcher = create_dispatcher(runtime.storage)
            app.state.dispatcher = runtime.dispatcher

            me = await runtime.bot.get_me()
            log.info("Authorised as @%s", me.username)

            if settings.bot_mode == "webhook":
                if not settings.webhook_base_url:
                    raise RuntimeError("BOT_MODE=webhook requires WEBHOOK_BASE_URL")
                url = f"{settings.webhook_base_url}{settings.webhook_path}"
                await runtime.bot.set_webhook(
                    url,
                    secret_token=settings.webhook_secret,
                    drop_pending_updates=False,
                    allowed_updates=["message", "callback_query", "chat_join_request", "my_chat_member"],
                )
                log.info("Webhook registered: %s", url)
            else:
                await runtime.bot.delete_webhook(drop_pending_updates=False)
                runtime.polling_task = asyncio.create_task(
                    runtime.dispatcher.start_polling(
                        runtime.bot,
                        allowed_updates=["message", "callback_query", "chat_join_request", "my_chat_member"],
                        handle_signals=False,
                    ),
                    name="bot-polling",
                )
                log.info("Long polling started")

            await setup_bot_commands(runtime.bot)
            await _warm_caches()
            await announce_startup(runtime.bot)
        except Exception as exc:
            message = f"راه‌اندازی ربات ناموفق بود: {exc}"
            log.exception(message)
            runtime.warnings.append(message)
            await notifier.record_event("critical", message, source="startup")  # type: ignore[arg-type]
    else:
        message = "BOT_TOKEN تنظیم نشده است؛ فقط پنل وب اجرا می‌شود."
        log.warning(message)
        runtime.warnings.append(message)

    # -- background jobs ---------------------------------------------------
    if not scheduler.running:
        scheduler.start()

    try:
        yield
    finally:
        log.info("Shutting down…")
        scheduler.shutdown()
        from app.services.broadcast import broadcasts

        await broadcasts.shutdown()

        if runtime.polling_task is not None:
            runtime.polling_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await runtime.polling_task
        if runtime.dispatcher is not None:
            with suppress(Exception):  # pragma: no cover
                await runtime.dispatcher.storage.close()
        if runtime.bot is not None:
            with suppress(Exception):  # pragma: no cover
                await runtime.bot.session.close()
        await panel_manager.close()
        await cache.close()
        await dispose_engine()
        log.info("Shutdown complete")


def build_lifespan():
    return lifespan


__all__ = ["Runtime", "build_lifespan", "lifespan"]
