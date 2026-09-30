"""Optional physical keyboard (a.k.a. reply keyboard).

An inline menu is part of the message it sits under: scroll, and it is gone.  A
*reply* keyboard stays above the input field until it is removed, which some
customers prefer on a phone — and the owner decides, with «کیبورد فیزیکی» on the
panel's appearance settings.

Two rules keep it honest:

* **the label is the route.**  A reply-keyboard press arrives as plain text with
  no callback data, so the button text must map back to an action.  Labels come
  from the same appearance catalog the inline menu uses and the layout never
  repeats a key (:mod:`app.services.menu_layout`), so the mapping is exact — and
  a message that is not an exact label is left to the catch-all.
* **screens are shared, never copied.**  Every entry in :data:`SCREENS` is the
  same function the inline button calls; those functions take an event and use
  :func:`app.bot.utils.show`, which edits a callback's message or sends a new
  one, so one screen serves both keyboards.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.types import Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import resolve_reply_label
from app.bot.menus import FEATURE_GATED, MENU_ACTIONS
from app.core.logging import get_logger
from app.db.models import User
from app.services import menu_layout
from app.services.settings_store import (
    app_settings,
    reply_keyboard_enabled,
    test_service_enabled,
    wallet_enabled,
)

log = get_logger(__name__)

#: A screen opener: ``(event, session, user, staff)`` — the same shape as a
#: handler, minus the transport.
Screen = Callable[..., Awaitable[None]]

#: ``action`` → the function that draws that screen.  Filled by :func:`screen`
#: from the handler modules at import time (see the bottom of this file for the
#: list, and :func:`screen` for how a module claims an action).
SCREENS: dict[str, Screen] = {}

#: Shown when the keyboard is (re)sent, so the customer knows what it is.
HINT_KEY = "menu.reply_keyboard_hint"


def screen(action: str):
    """Register the function that opens ``action`` for both keyboards."""

    def decorate(func: Screen) -> Screen:
        if action in SCREENS:  # pragma: no cover - a duplicate is a coding error
            raise RuntimeError(f"Two screens claim the {action!r} action")
        SCREENS[action] = func
        return func

    return decorate


def enabled() -> bool:
    return reply_keyboard_enabled()


async def actions(session: AsyncSession | None, *, is_staff: bool = False) -> list[tuple[str, str]]:
    """``[(label, action)]`` for the buttons the customer should see, in order.

    The owner's layout decides the rows; the feature switches decide which of
    those buttons are live, exactly as they do for the inline menu.
    """
    live = {
        "test": test_service_enabled(),
        "wallet": wallet_enabled(),
        "guides": app_settings.get_bool("shop.guides_enabled", True),
        "gift": app_settings.get_bool("shop.gift_enabled", True),
    }
    out: list[tuple[str, str]] = []
    for row in await menu_layout.rows(session):
        for visual_key in row.keys:
            feature = FEATURE_GATED.get(visual_key)
            if feature and not live.get(feature, True):
                continue
            action = MENU_ACTIONS.get(visual_key)
            if action is None:
                continue
            label = await resolve_reply_label(visual_key, session)
            out.append((label, action[0]))
    if is_staff and "admin" not in {action for _label, action in out}:
        out.append((await resolve_reply_label("admin.broadcast", session), "admin"))
    return out


async def labels(session: AsyncSession | None, *, is_staff: bool = False) -> dict[str, str]:
    """``{label: action}`` — the routing table for an incoming text message."""
    return dict(await actions(session, is_staff=is_staff))


async def build(session: AsyncSession | None, *, is_staff: bool = False) -> ReplyKeyboardMarkup | None:
    """The keyboard itself, or ``None`` when the feature is off or empty."""
    if not enabled():
        return None
    buttons = await actions(session, is_staff=is_staff)
    if not buttons:
        return None

    builder = ReplyKeyboardBuilder()
    for label, _action in buttons:
        builder.button(text=label)
    # Two per row, like the inline menu; the layout's own row shape is not
    # reproduced because a phone keyboard wraps by label width anyway.
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True, is_persistent=True)


def removal() -> ReplyKeyboardRemove:
    """Markup that takes the keyboard away again (``/keyboard off``)."""
    return ReplyKeyboardRemove()


async def match(session: AsyncSession | None, text: str, *, is_staff: bool = False) -> str | None:
    """The action a typed message means, or ``None`` when it is not a button."""
    value = (text or "").strip()
    if not value:
        return None
    return (await labels(session, is_staff=is_staff)).get(value)


async def open(action: str, event: Message, session: AsyncSession, user: User | None, staff: Any = None) -> bool:
    """Draw the screen behind ``action``; ``False`` when nothing claims it."""
    opener = SCREENS.get(action)
    if opener is None:
        log.warning("Reply keyboard action %r has no screen — ignored", action)
        return False
    await opener(event, session, user, staff)
    return True


def health_check() -> list[str]:
    """Actions the keyboard can offer but no screen answers.

    Called by the test suite: a button that does nothing is the failure mode this
    whole module exists to prevent.
    """
    wanted = {action for action, _arg in MENU_ACTIONS.values()}
    return sorted(wanted - set(SCREENS) - {"admin"})


__all__ = [
    "HINT_KEY",
    "SCREENS",
    "actions",
    "build",
    "enabled",
    "health_check",
    "labels",
    "match",
    "open",
    "removal",
    "screen",
]
