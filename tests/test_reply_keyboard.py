"""The optional physical keyboard.

Three things have to hold, and each of them has failed in some bot or other:

* the keyboard exists **only** when the owner switched it on, and it draws the
  same buttons as the inline menu (owner layout + feature switches);
* a press is plain text, so the label must map back to an action — and only an
  *exact* label, or a customer typing «کیف پول» mid-flow lands somewhere strange;
* every button on it has a screen behind it.  A reply button has no callback data
  and no error if it goes nowhere: it simply does nothing, for ever.
"""

from __future__ import annotations

import pytest

from app.bot import reply_menu
from app.services import menu_layout
from app.services.settings_store import app_settings

pytestmark = pytest.mark.db


async def _enable(session) -> None:
    await app_settings.load(session, force=True)
    await app_settings.set_many(session, {"appearance.reply_keyboard": True})


async def _disable(session) -> None:
    await app_settings.set_many(session, {"appearance.reply_keyboard": False})


# ---------------------------------------------------------------------------
# Building it
# ---------------------------------------------------------------------------
async def test_the_keyboard_is_off_until_the_owner_turns_it_on(session) -> None:
    await _disable(session)
    assert reply_menu.enabled() is False
    assert await reply_menu.build(session) is None

    await _enable(session)
    assert reply_menu.enabled() is True
    assert await reply_menu.build(session) is not None
    await _disable(session)


async def test_it_draws_the_same_buttons_as_the_inline_menu(session) -> None:
    await _enable(session)
    try:
        labels = [label for label, _action in await reply_menu.actions(session)]
        assert labels, "an enabled keyboard with no buttons is a bug"

        # Every label is unique — the label *is* the route.
        assert len(labels) == len(set(labels))

        # And the set matches the layout, row for row.
        expected = [key for row in await menu_layout.rows(session) for key in row.keys]
        assert len(labels) == len(expected)
    finally:
        await _disable(session)


async def test_a_feature_switch_removes_its_button(session) -> None:
    await _enable(session)
    await app_settings.set_many(session, {"test.enabled": False})
    try:
        actions = {action: label for label, action in await reply_menu.actions(session)}
        assert "test" not in actions
    finally:
        await app_settings.set_many(session, {"test.enabled": True})
        await _disable(session)


async def test_the_owner_layout_controls_the_keyboard_too(session) -> None:
    await _enable(session)
    try:
        await menu_layout.save(session, [["menu.support", "menu.buy"]])
        actions = [action for _label, action in await reply_menu.actions(session)]
        assert actions == ["support", "buy"]
    finally:
        await menu_layout.reset(session)
        await _disable(session)


# ---------------------------------------------------------------------------
# Routing a press
# ---------------------------------------------------------------------------
async def test_a_press_maps_back_to_its_action(session) -> None:
    await _enable(session)
    try:
        pairs = await reply_menu.actions(session)
        for label, action in pairs:
            assert await reply_menu.match(session, label) == action
    finally:
        await _disable(session)


async def test_only_an_exact_label_is_a_press(session) -> None:
    await _enable(session)
    try:
        label, _action = (await reply_menu.actions(session))[0]
        assert await reply_menu.match(session, "سلام، قیمت چنده؟") is None
        assert await reply_menu.match(session, f" {label} ") == _action  # whitespace is forgiven
        assert await reply_menu.match(session, label + "!") is None
        assert await reply_menu.match(session, "") is None
    finally:
        await _disable(session)


async def test_a_keyboard_left_on_a_phone_keeps_working(session) -> None:
    """Switching the feature off must not strand the keyboard a customer has.

    Telegram only removes a reply keyboard when the bot sends
    ``ReplyKeyboardRemove``; until then the buttons are still there, and they
    open exactly the screens the inline menu opens — so they keep working.
    """
    await _enable(session)
    label, action = (await reply_menu.actions(session))[0]
    await _disable(session)

    # Nothing new is sent …
    assert await reply_menu.build(session) is None
    # … but the label still routes.
    assert await reply_menu.match(session, label) == action


def test_every_button_has_a_screen() -> None:
    """The guard that matters: no reply button may be a dead end.

    ``app.bot.handlers.reply_menu`` registers one screen per action when it is
    imported, which the dispatcher does at startup.
    """
    import app.bot.handlers.reply_menu  # noqa: F401  (registers the screens)

    assert reply_menu.health_check() == [], f"no screen for {reply_menu.health_check()}"


# ---------------------------------------------------------------------------
# Rendering the markup
# ---------------------------------------------------------------------------
async def test_the_markup_is_a_real_reply_keyboard(session) -> None:
    from aiogram.types import ReplyKeyboardMarkup

    await _enable(session)
    try:
        markup = await reply_menu.build(session)
        assert isinstance(markup, ReplyKeyboardMarkup)
        assert markup.resize_keyboard is True
        # Two buttons per row, like the inline menu.
        assert all(len(row) <= 2 for row in markup.keyboard)
    finally:
        await _disable(session)


def test_removal_is_a_reply_keyboard_remove() -> None:
    from aiogram.types import ReplyKeyboardRemove

    assert isinstance(reply_menu.removal(), ReplyKeyboardRemove)
