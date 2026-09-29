"""Forced channel membership gate."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.core.logging import get_logger
from app.services.membership import membership

log = get_logger(__name__)

#: Commands that must work even when the user has not joined yet.
ALLOWED_COMMANDS = {"/start", "/help", "/rules", "/id"}
#: Callback payloads that must bypass the gate (otherwise the user is stuck).
ALLOWED_CALLBACK_PREFIXES = ("mn:check_join", "nv:", "noop")


class MembershipMiddleware(BaseMiddleware):
    """Blocks every interaction until the required channels are joined."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        session = data.get("session")
        user = data.get("user")
        if session is None or user is None:
            return await handler(event, data)

        # Staff never see the gate.
        if data.get("is_staff"):
            return await handler(event, data)
        if not membership.enabled():
            return await handler(event, data)
        if self._is_allowed(event):
            return await handler(event, data)

        status = await membership.check(session, user.telegram_id)
        if status.ok:
            return await handler(event, data)

        await self._prompt(session, event, status.missing)
        return None

    @staticmethod
    def _is_allowed(event: TelegramObject) -> bool:
        if isinstance(event, Message):
            text = (event.text or "").strip()
            command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
            return command in ALLOWED_COMMANDS
        if isinstance(event, CallbackQuery):
            payload = event.data or ""
            return any(payload.startswith(prefix) for prefix in ALLOWED_CALLBACK_PREFIXES)
        return False

    @staticmethod
    async def _prompt(session, event: TelegramObject, missing) -> None:
        from app.services.notifications import notifier
        from app.services.texts import texts

        body = await texts.get("start.must_join", session, channels=_listed(missing))
        keyboard = await membership.join_keyboard(session, missing)

        if isinstance(event, Message):
            await notifier.send(event.chat.id, body, keyboard=keyboard)
        elif isinstance(event, CallbackQuery):
            await event.answer()
            if event.message is not None:
                await notifier.send(event.message.chat.id, body, keyboard=keyboard)


def _listed(channels) -> str:
    return "\n" + "\n".join(f"• {c.title or c.chat_id}" for c in channels) + "\n"


__all__ = ["ALLOWED_CALLBACK_PREFIXES", "ALLOWED_COMMANDS", "MembershipMiddleware"]
