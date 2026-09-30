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
from app.services.appearance import appearance
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
        keyboard = await reply_menu.build(session)
        assert keyboard is not None
        markup = keyboard.markup
        assert isinstance(markup, ReplyKeyboardMarkup)
        assert markup.resize_keyboard is True
        # Two buttons per row: a phone wraps a reply keyboard by label width.
        assert all(len(row) <= 2 for row in markup.keyboard)
        assert len(markup.keyboard) >= 1
    finally:
        await _disable(session)


async def test_the_buttons_carry_their_colour_and_premium_emoji(session) -> None:
    """A reply button can be styled too — it was drawn as plain text before.

    ``style`` and ``icon_custom_emoji_id`` exist on ``KeyboardButton`` in the
    same Bot API version that added them for inline buttons, so the physical
    keyboard has no reason to look like a terminal.
    """
    await _enable(session)
    try:
        # The owner configures premium emoji on the emoji itself; every button
        # linked to it borrows both the id and the Unicode fallback.
        await appearance.set_visual(
            session,
            "emoji.cart",
            style=None,
            icon_custom_emoji_id="5368324170671202286",
        )
        keyboard = await reply_menu.build(session)
        assert keyboard is not None

        buttons = [button for row in keyboard.markup.keyboard for button in row]
        styled = next(button for button in buttons if "خرید سرویس" in button.text)
        assert styled.style == "success"
        assert styled.icon_custom_emoji_id == "5368324170671202286"
        # With a premium icon the Unicode emoji is dropped, as it is inline …
        assert not styled.text.startswith("🛒")

        # … and the plain twin keeps it, so an old API server still shows one.
        plain = keyboard.plain()
        assert plain is not None
        plain_buttons = [button for row in plain.keyboard for button in row]
        assert all(button.icon_custom_emoji_id is None for button in plain_buttons)
        assert all(button.style is None for button in plain_buttons)
        assert any(button.text.startswith("🛒") for button in plain_buttons)
    finally:
        await appearance.reset(session, ["emoji.cart"])
        await _disable(session)


async def test_both_label_variants_route_to_the_same_action(session) -> None:
    """A keyboard already on a phone may have been drawn either way."""
    await _enable(session)
    try:
        await appearance.set_visual(session, "emoji.cart", icon_custom_emoji_id="5368324170671202286")
        spec = next(spec for spec in await reply_menu.buttons(session) if spec.visual_key == "menu.buy")
        assert spec.routes == ("خرید سرویس", "🛒 خرید سرویس")
        for label in spec.routes:
            assert await reply_menu.match(session, label) == "buy"
    finally:
        await appearance.reset(session, ["emoji.cart"])
        await _disable(session)


def test_removal_is_a_reply_keyboard_remove() -> None:
    from aiogram.types import ReplyKeyboardRemove

    assert isinstance(reply_menu.removal(), ReplyKeyboardRemove)
