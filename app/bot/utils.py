"""Small helpers shared by every handler.

The important one is :func:`show` — it gives every screen the same behaviour
whether it was opened from a button (edit in place) or from a command (send a
new message), and it transparently retries without styling when needed.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import suppress
from typing import Any, TypeVar

from aiogram.types import CallbackQuery, Message, TelegramObject

from app.bot.keyboards import KB
from app.core.logging import get_logger
from app.services.notifications import notifier

log = get_logger(__name__)

T = TypeVar("T")


def chat_id_of(event: TelegramObject) -> int | None:
    if isinstance(event, Message):
        return event.chat.id
    if isinstance(event, CallbackQuery) and event.message is not None:
        return event.message.chat.id
    return None


async def show(
    event: TelegramObject,
    text: str,
    *,
    keyboard: KB | None = None,
    media: Any = None,
    disable_preview: bool = True,
) -> Message | None:
    """Render a screen: edit the current message when possible, else send new."""
    if isinstance(event, CallbackQuery) and event.message is not None:
        if media is None:
            edited = await notifier.edit(event.message.chat.id, event.message.message_id, text, keyboard=keyboard)
            if edited:
                return None
        else:
            edited = await notifier.edit(
                event.message.chat.id, event.message.message_id, text, keyboard=keyboard, media=media
            )
            if edited:
                return None

    chat_id = chat_id_of(event)
    if chat_id is None:
        return None
    return await notifier.send(chat_id, text, keyboard=keyboard, media=media, disable_preview=disable_preview)


async def replace_media(
    event: TelegramObject,
    text: str,
    media: Any,
    *,
    keyboard: KB | None = None,
) -> Message | None:
    """Force a media message (needed when switching between text and photo)."""
    return await show(event, text, keyboard=keyboard, media=media)


def paginate(items: Sequence[T], page: int, per_page: int = 6) -> tuple[list[T], int, int]:
    """Return ``(slice, page, total_pages)`` with a 1-based, clamped page."""
    per_page = max(per_page, 1)
    total = max((len(items) + per_page - 1) // per_page, 1)
    page = min(max(page, 1), total)
    start = (page - 1) * per_page
    return list(items[start : start + per_page]), page, total


async def answer_callback(event: TelegramObject, text: str = "", *, alert: bool = False) -> None:
    if isinstance(event, CallbackQuery):
        with suppress(Exception):  # pragma: no cover - callback already answered
            await event.answer(text[:190], show_alert=alert)


def parse_int(text: str | None, *, persian: bool = True) -> int | None:
    """Parse a user-typed number, tolerating Persian digits and separators."""
    if not text:
        return None
    value = text.strip()
    if persian:
        from app.core.money import en_digits

        value = en_digits(value)
    value = value.replace(",", "").replace("٬", "").replace("،", "").replace(" ", "")
    try:
        return int(value)
    except ValueError:
        return None


def truncate(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def notify_user(event_or_user, text: str, *, keyboard: KB | None = None) -> None:
    """Send an out-of-band message without disturbing the current screen."""
    from app.db.models import User

    if isinstance(event_or_user, User):
        await notifier.to_user(event_or_user, text, keyboard=keyboard)
        return
    chat_id = chat_id_of(event_or_user)
    if chat_id is not None:
        await notifier.send(chat_id, text, keyboard=keyboard)


__all__ = [
    "answer_callback",
    "chat_id_of",
    "notify_user",
    "paginate",
    "parse_int",
    "replace_media",
    "show",
    "truncate",
]
