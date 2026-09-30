"""The main menu's layout: ownable, sparse, and safe to upgrade.

The layout is stored as *overrides* of a code default.  That design has three
consequences worth pinning, because getting any of them wrong is a bug an
operator would see as "my menu keeps coming back":

* nothing stored  → exactly the shipped layout;
* a key saved out → the button is gone, and it does **not** return from the
  default on the next save (removal has to be recorded, not merely omitted);
* a new release adds a menu entry → it appears for every shop, because only the
  keys the owner touched are overridden.
"""

from __future__ import annotations

import pytest

from app.core.errors import ValidationError
from app.services import menu_layout
from app.services.menu_layout import ADDABLE_KEYS, DEFAULT_ROWS

pytestmark = pytest.mark.db


def _flat(rows) -> list[list[str]]:
    return [list(row.keys) for row in rows]


async def test_an_untouched_shop_gets_the_shipped_layout(session) -> None:
    assert _flat(await menu_layout.rows(session)) == [list(row) for row in DEFAULT_ROWS]
    assert _flat(await menu_layout.rows(None)) == [list(row) for row in DEFAULT_ROWS]


async def test_saving_a_layout_reorders_and_regroups(session) -> None:
    await menu_layout.save(session, [["menu.my_services", "menu.buy"], ["menu.support"]])

    assert _flat(await menu_layout.rows(session)) == [["menu.my_services", "menu.buy"], ["menu.support"]]


async def test_removing_a_button_does_not_let_it_come_back(session) -> None:
    """The trap: an absent key falls back to the default, so removal must stick."""
    remaining = [list(row) for row in DEFAULT_ROWS]
    remaining[1] = [key for key in remaining[1] if key != "menu.wallet"]

    await menu_layout.save(session, remaining)
    drawn = _flat(await menu_layout.rows(session))

    assert "menu.wallet" not in {key for row in drawn for key in row}
    assert "menu.wallet" in await menu_layout.hidden_keys(session)

    # Saving again (with the same payload) must not resurrect it.
    await menu_layout.save(session, drawn)
    assert "menu.wallet" not in {key for row in _flat(await menu_layout.rows(session)) for key in row}


async def test_a_button_can_be_added_to_the_menu(session) -> None:
    everything = [list(row) for row in DEFAULT_ROWS]
    everything[0] = [*everything[0], "menu.rules"]

    await menu_layout.save(session, everything)
    drawn = _flat(await menu_layout.rows(session))

    assert "menu.rules" in drawn[0]
    assert "menu.rules" in ADDABLE_KEYS


async def test_an_unknown_key_is_refused_and_nothing_changes(session) -> None:
    with pytest.raises(ValidationError):
        await menu_layout.save(session, [["menu.buy", "menu.definitely-not-real"]])

    assert _flat(await menu_layout.rows(session)) == [list(row) for row in DEFAULT_ROWS]


async def test_an_empty_menu_is_refused(session) -> None:
    with pytest.raises(ValidationError):
        await menu_layout.save(session, [[], []])


async def test_a_duplicated_button_is_kept_once(session) -> None:
    await menu_layout.save(session, [["menu.buy", "menu.buy"], ["menu.support"]])

    drawn = _flat(await menu_layout.rows(session))
    assert drawn == [["menu.buy"], ["menu.support"]]


async def test_reset_restores_the_shipped_layout(session) -> None:
    await menu_layout.save(session, [["menu.buy"]])
    await menu_layout.reset(session)

    assert _flat(await menu_layout.rows(session)) == [list(row) for row in DEFAULT_ROWS]
    assert await menu_layout.hidden_keys(session) == set()


async def test_hidden_buttons_are_offered_again_by_the_editor(session) -> None:
    await menu_layout.save(session, [["menu.buy"]])

    available = await menu_layout.available_keys(session)
    assert "menu.support" in available
    assert "menu.buy" not in available


# ---------------------------------------------------------------------------
# The bot draws the owner's layout
# ---------------------------------------------------------------------------
def _labels(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


async def test_the_bot_menu_follows_the_saved_layout(session) -> None:
    from app.bot.menus import main_menu

    await menu_layout.save(session, [["menu.support"], ["menu.buy", "menu.my_services"]])

    markup = (await main_menu(session)).markup
    labels = _labels(markup)

    assert len(markup.inline_keyboard) == 2
    assert any("پشتیبانی" in label for label in labels)
    assert not any("کیف پول" in label for label in labels)  # still hidden by the layout


async def test_a_feature_switch_removes_its_button_even_if_the_layout_lists_it(session) -> None:
    """Two independent gates: the owner's layout and the shop's feature switch."""
    from app.bot.menus import main_menu

    await menu_layout.save(session, [["menu.wallet", "menu.buy"]])

    labels = _labels((await main_menu(session, show_wallet=False)).markup)

    assert any("خرید" in label for label in labels)
    assert not any("کیف پول" in label for label in labels)
