"""Keyboard layout rules.

Telegram rejects a row with more than eight buttons, and ``aiogram`` raises
``ValueError: Row size N is not allowed`` while building the markup — the whole
screen dies before it is sent.

Regression: ``KeyboardBuilder(columns=...)`` stored the value and then never read
it, so every keyboard put all of its buttons in a single row.  The main menu has
ten buttons, which is why ``/start`` answered «خطای غیرمنتظره‌ای رخ داد» on a
fresh installation.
"""

from __future__ import annotations

from app.bot.keyboards import MAX_ROW_BUTTONS, KeyboardBuilder
from app.bot.menus import MAIN_MENU_LAYOUT, default_main_menu, main_menu


async def _build(columns: int, count: int):
    kb = KeyboardBuilder(session=None, columns=columns)
    for index in range(count):
        await kb.add("menu.main", callback=f"cb:{index}")
    return kb.build()


def _widths(markup) -> list[int]:
    return [len(row) for row in markup.inline_keyboard]


async def test_columns_is_the_number_of_buttons_per_row():
    assert _widths((await _build(columns=2, count=5)).markup) == [2, 2, 1]


async def test_an_over_wide_row_is_split_rather_than_rejected():
    assert _widths((await _build(columns=99, count=19)).markup) == [8, 8, 3]


async def test_no_keyboard_can_exceed_the_telegram_limit():
    for columns in (1, 2, 3, 5, 8, 20):
        for count in (1, 7, 8, 9, 17):
            widths = _widths((await _build(columns=columns, count=count)).markup)
            assert sum(widths) == count, "every button must survive"
            assert all(1 <= width <= MAX_ROW_BUTTONS for width in widths)


async def test_an_explicit_row_break_is_still_honoured():
    kb = KeyboardBuilder(session=None, columns=2)
    await kb.add("menu.main", callback="a")
    kb.row()
    await kb.add("menu.back", callback="b")
    await kb.add("menu.main", callback="c", new_row=True)

    assert _widths(kb.build().markup) == [1, 1, 1]


async def test_the_main_menu_fits_on_a_fresh_installation():
    """The exact screen that broke: every shipped button, every feature enabled."""
    kb = await main_menu(None, show_test=True, show_wallet=True, show_guides=True, show_gift=True)

    widths = _widths(kb.markup)
    assert all(width <= MAX_ROW_BUTTONS for width in widths)
    # The shipped layout is the guide's channel button included; with no session
    # the owner's layout does not apply, so every key must be drawn.
    assert sum(widths) == len(MAIN_MENU_LAYOUT)


async def test_the_rows_follow_the_shipped_layout():
    from app.bot.menus import MENU_ACTIONS
    from app.services.menu_layout import DEFAULT_ROWS

    kb = await main_menu(None)

    widths = _widths(kb.markup)
    assert widths == [len(row) for row in DEFAULT_ROWS]
    assert set(MENU_ACTIONS) == {key for row in DEFAULT_ROWS for key in row}, (
        "every laid-out button needs an action, and every action a place in the default layout"
    )


async def test_the_gated_main_menu_also_fits():
    """Switches change the button count; none of them may produce a wide row."""
    kb = await default_main_menu(None)

    assert all(width <= MAX_ROW_BUTTONS for width in _widths(kb.markup))
