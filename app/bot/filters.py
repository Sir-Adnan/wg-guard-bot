"""Reusable custom filters."""

from __future__ import annotations

from typing import Any

from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject

from app.db.models import Staff, StaffRole


class IsStaff(BaseFilter):
    """True for any active operator (owner, admin or support)."""

    async def __call__(self, event: TelegramObject, **data: Any) -> bool:
        return data.get("staff") is not None


class IsAdmin(BaseFilter):
    """True for owners and admins (not support)."""

    async def __call__(self, event: TelegramObject, **data: Any) -> bool:
        staff: Staff | None = data.get("staff")
        return staff is not None and staff.role in (StaffRole.OWNER, StaffRole.ADMIN)


class IsOwner(BaseFilter):
    async def __call__(self, event: TelegramObject, **data: Any) -> bool:
        staff: Staff | None = data.get("staff")
        return staff is not None and staff.role == StaffRole.OWNER


class IsPrivateChat(BaseFilter):
    async def __call__(self, event: TelegramObject, **data: Any) -> bool:
        chat = data.get("event_chat")
        return bool(chat and chat.type == "private")


class HasText(BaseFilter):
    """True when the message carries at least one character of text."""

    async def __call__(self, event: TelegramObject, **data: Any) -> bool:
        text = getattr(event, "text", None)
        return bool(text and text.strip())


class ReplyButton(BaseFilter):
    """True when a message is **exactly** one of the physical keyboard's labels.

    A reply-keyboard press arrives as plain text, so the router that serves it
    has to decide "is this a button?" *before* claiming the message: a handler
    whose filters match ends the dispatch, and anything it silently ignores never
    reaches the catch-all.  Answering a question with silence is worse than
    answering it badly.

    Returns the resolved action as filter data (``reply_action``) so the handler
    does not have to resolve the label a second time.
    """

    async def __call__(self, event: TelegramObject, **data: Any) -> bool | dict[str, Any]:
        from app.bot import reply_menu

        session = data.get("session")
        text = (getattr(event, "text", None) or "").strip()
        if session is None or not text:
            return False
        action = await reply_menu.match(session, text, is_staff=bool(data.get("is_staff")))
        return {"reply_action": action} if action else False


__all__ = ["HasText", "IsAdmin", "IsOwner", "IsPrivateChat", "IsStaff", "ReplyButton"]
