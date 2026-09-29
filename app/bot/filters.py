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


__all__ = ["HasText", "IsAdmin", "IsOwner", "IsPrivateChat", "IsStaff"]
