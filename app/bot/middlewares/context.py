"""Session handling and identity resolution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy import select

from app.core.logging import get_logger
from app.db.models import Staff, StaffRole
from app.db.session import get_session_factory
from app.services.settings_store import app_settings, maintenance_mode
from app.services.users import user_service

log = get_logger(__name__)


class DatabaseMiddleware(BaseMiddleware):
    """Open one session per update and commit it when the handler succeeds."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        factory = get_session_factory()
        async with factory() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise


class ContextMiddleware(BaseMiddleware):
    """Resolve the customer + operator behind an update, and gate the bot."""

    def __init__(self) -> None:
        self._bootstrap_done = False

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        session = data.get("session")
        if session is None:  # pragma: no cover - misconfiguration guard
            return await handler(event, data)

        tg_user = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)

        if not self._bootstrap_done:
            await self._bootstrap(session)
            self._bootstrap_done = True

        user, created = await user_service.get_or_create(session, tg_user)
        data["user"] = user
        data["is_new_user"] = created

        staff = await self._resolve_staff(session, tg_user.id)
        data["staff"] = staff
        data["is_staff"] = staff is not None
        data["is_admin"] = staff is not None and staff.role in (StaffRole.OWNER, StaffRole.ADMIN)

        if created:
            await self._announce_new_user(session, user)

        # -- gates ---------------------------------------------------------
        if user.is_blocked and not data["is_staff"]:
            from app.services.texts import texts

            reason = user.block_reason or "—"
            body = await texts.get("start.banned", session, reason=reason)
            await self._reply(event, body)
            return None

        if maintenance_mode() and not data["is_staff"]:
            from app.services.texts import texts

            body = app_settings.get_str("shop.maintenance_message", "").strip() or await texts.get(
                "start.maintenance", session
            )
            await self._reply(event, body)
            return None

        return await handler(event, data)

    # -- helpers -----------------------------------------------------------
    async def _resolve_staff(self, session, telegram_id: int) -> Staff | None:
        from app.core.config import settings

        staff = (
            await session.execute(select(Staff).where(Staff.telegram_id == telegram_id, Staff.is_active.is_(True)))
        ).scalar_one_or_none()
        if staff is not None:
            return staff

        # Bootstrap owners straight from the environment on first contact.
        if telegram_id in settings.admin_id_list:
            staff = Staff(
                telegram_id=telegram_id,
                name="مالک",
                role=StaffRole.OWNER,
                receive_receipts=True,
            )
            session.add(staff)
            await session.flush()
            log.info("Bootstrapped owner staff record for %s", telegram_id)
            return staff
        if telegram_id in settings.support_id_list:
            staff = Staff(
                telegram_id=telegram_id,
                name="پشتیبان",
                role=StaffRole.SUPPORT,
                receive_receipts=True,
            )
            session.add(staff)
            await session.flush()
            log.info("Bootstrapped support staff record for %s", telegram_id)
            return staff
        return None

    async def _bootstrap(self, session) -> None:
        """Seed the owner account and cache warm-up on the first update."""
        from app.core.config import settings
        from app.core.security import hash_password
        from app.services.appearance import appearance
        from app.services.texts import texts

        await app_settings.load(session, force=True)
        await texts.load(session, force=True)
        await appearance.load(session, force=True)

        owner_login = settings.owner_username.strip() or "admin"
        existing = (await session.execute(select(Staff).where(Staff.login == owner_login))).scalar_one_or_none()
        if existing is None:
            password = settings.owner_password or None
            session.add(
                Staff(
                    telegram_id=settings.admin_id_list[0] if settings.admin_id_list else None,
                    name="مالک",
                    role=StaffRole.OWNER,
                    login=owner_login,
                    password_hash=hash_password(password) if password else None,
                    receive_receipts=True,
                )
            )
            await session.flush()
            log.info("Seeded web-panel owner account %r", owner_login)

    async def _announce_new_user(self, session, user) -> None:
        if not app_settings.get_bool("notify.admin_new_user", False):
            return
        from app.services.notifications import notifier

        try:
            await notifier.to_admins(
                session,
                f"🆕 <b>کاربر جدید</b>\n\n{user.mention}\nشناسه: <code>{user.telegram_id}</code>",
                disable_notification=True,
            )
        except Exception as exc:  # pragma: no cover
            log.debug("new-user notice failed: %s", exc)

    @staticmethod
    async def _reply(event: TelegramObject, text: str) -> None:
        from app.services.notifications import notifier

        if isinstance(event, Message):
            await notifier.send(event.chat.id, text)
        elif isinstance(event, CallbackQuery):
            await event.answer(text[:180], show_alert=True)


__all__ = ["ContextMiddleware", "DatabaseMiddleware"]
