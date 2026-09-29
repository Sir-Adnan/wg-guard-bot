"""Per-user rate limiting.

A sliding counter in the key/value store (Redis when available, memory
otherwise).  Two windows are enforced: a short burst limit for callbacks and a
longer limit for messages, which is enough to stop button-mashing loops without
annoying normal users.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.core.cache import cache
from app.core.logging import get_logger

log = get_logger(__name__)


class ThrottleMiddleware(BaseMiddleware):
    """Drop updates that exceed the configured rate."""

    def __init__(self, *, callback_limit: int = 20, message_limit: int = 12, window: int = 10) -> None:
        self.callback_limit = callback_limit
        self.message_limit = message_limit
        self.window = window

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Operators are never throttled — they triage receipts in bulk.
        if data.get("is_staff"):
            return await handler(event, data)

        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            bucket, limit, alert = "cb", self.callback_limit, False
        elif isinstance(event, Message):
            # Commands and media (receipts!) must never be dropped.
            if (event.text or "").startswith("/") or event.photo or event.document:
                return await handler(event, data)
            bucket, limit, alert = "msg", self.message_limit, True
        else:
            return await handler(event, data)

        key = f"throttle:{bucket}:{user.id}"
        try:
            count = await cache.store.incr(key, self.window)
        except Exception as exc:  # pragma: no cover - store hiccup must not block users
            log.debug("throttle store error: %s", exc)
            return await handler(event, data)

        if count <= limit:
            return await handler(event, data)

        if alert and count == limit + 1:
            from app.services.notifications import notifier
            from app.services.texts import texts

            session = data.get("session")
            body = await texts.get("error.rate_limited", session)
            await notifier.send(user.id, body)
        elif isinstance(event, CallbackQuery):
            await event.answer("کمی آرامتر! چند لحظه صبر کنید.", show_alert=False)
        return None


__all__ = ["ThrottleMiddleware"]
