"""Walking into a category must show that category — and only what it holds.

Two complaints from a live install, both about the same screen: the front page's
«پیشنهاد ویژه»/«همه سرویس‌ها» doors kept following the customer down into every
sub-category, and the number of buttons per row could be configured in the panel
without anything on the screen changing.
"""

from __future__ import annotations

import pytest

from app.bot.callbacks import CatCB
from app.bot.setup import reset_dispatcher
from app.services import categories
from app.services.settings_store import app_settings

pytestmark = pytest.mark.db

CUSTOMER_TELEGRAM_ID = 111_222_333


@pytest.fixture(autouse=True)
def _clean_dispatcher():
    reset_dispatcher()
    yield
    reset_dispatcher()


@pytest.fixture(autouse=True)
async def _clean_settings(session):
    await app_settings.load(session, force=True)
    yield
    for key in ("appearance.category_columns", "appearance.plan_columns"):
        await app_settings.reset(session, key)
    await app_settings.load(session, force=True)


def _keyboard(bot) -> list[list[str]]:
    """The last inline keyboard the bot drew, as rows of button texts."""
    for _name, kwargs in reversed(bot.calls):
        markup = kwargs.get("reply_markup")
        rows = getattr(markup, "inline_keyboard", None)
        if rows:
            return [[button.text for button in row] for row in rows]
    return []


def _flat(bot) -> str:
    return " | ".join(text for row in _keyboard(bot) for text in row)


async def _open(bot, feed_update, payload: str) -> None:
    bot.calls.clear()
    await feed_update(bot, callback=payload, telegram_id=CUSTOMER_TELEGRAM_ID)


# ---------------------------------------------------------------------------
# What each level offers
# ---------------------------------------------------------------------------
async def test_the_front_page_offers_the_doors_out_of_the_shop(
    session, customer, plan, bound_notifier, recording_bot, feed_update
):
    from app.db.models import Plan

    root = await categories.create(session, name="سرویس‌ها")
    child = await categories.create(session, name="اقتصادی", parent_id=root.id)
    plan.category_id = child.id
    plan.is_featured = True
    session.add(Plan(name="ویژه", price_rial=1_000, is_active=True, is_featured=True))
    await session.commit()

    await _open(recording_bot, feed_update, CatCB(action="home").pack())

    drawn = _flat(recording_bot)
    assert "سرویس‌ها" in drawn
    assert "پیشنهاد ویژه" in drawn
    assert "همه سرویس‌ها" in drawn


async def test_a_category_shows_its_children_and_no_way_back_to_the_catalogue(
    session, customer, bound_notifier, recording_bot, feed_update
):
    root = await categories.create(session, name="سرویس‌ها")
    await categories.create(session, name="اقتصادی", parent_id=root.id)
    await categories.create(session, name="حرفه‌ای", parent_id=root.id)
    await session.commit()

    await _open(recording_bot, feed_update, CatCB(action="open", category_id=root.id).pack())

    drawn = _flat(recording_bot)
    assert "اقتصادی" in drawn and "حرفه‌ای" in drawn
    assert "پیشنهاد ویژه" not in drawn, "the front page's doors followed the customer into a category"
    assert "همه سرویس‌ها" not in drawn, "the front page's doors followed the customer into a category"
    assert "بازگشت" in drawn


async def test_a_category_with_plans_and_children_keeps_both(
    session, customer, plan, bound_notifier, recording_bot, feed_update
):
    root = await categories.create(session, name="سرویس‌ها")
    await categories.create(session, name="اقتصادی", parent_id=root.id)
    plan.category_id = root.id
    await session.commit()

    await _open(recording_bot, feed_update, CatCB(action="open", category_id=root.id).pack())

    drawn = _flat(recording_bot)
    assert plan.name in drawn, "the node's own plans disappeared"
    assert "اقتصادی" in drawn, "the node's sub-categories disappeared"
    assert "پیشنهاد ویژه" not in drawn and "همه سرویس‌ها" not in drawn


# ---------------------------------------------------------------------------
# How many buttons share a row
# ---------------------------------------------------------------------------
async def test_category_columns_setting_changes_the_row_shape(
    session, customer, bound_notifier, recording_bot, feed_update
):
    root = await categories.create(session, name="سرویس‌ها")
    for name in ("الف", "ب", "پ", "ت"):
        await categories.create(session, name=name, parent_id=root.id)
    await session.commit()

    payload = CatCB(action="open", category_id=root.id).pack()
    await _open(recording_bot, feed_update, payload)
    assert [len(row) for row in _keyboard(recording_bot)][:-1] == [2, 2]  # the shipped default

    await app_settings.set_many(session, {"appearance.category_columns": 1})
    await session.commit()  # the bot opens its own connection for every update
    await _open(recording_bot, feed_update, payload)
    assert [len(row) for row in _keyboard(recording_bot)][:-1] == [1, 1, 1, 1]

    await app_settings.set_many(session, {"appearance.category_columns": 4})
    await session.commit()
    await _open(recording_bot, feed_update, payload)
    assert [len(row) for row in _keyboard(recording_bot)][:-1] == [4]


async def test_a_stored_value_outside_the_range_cannot_break_a_keyboard(
    session, customer, bound_notifier, recording_bot, feed_update
):
    """An old row (or a hand-edited one) is clamped, never sent to Telegram."""
    root = await categories.create(session, name="سرویس‌ها")
    for name in ("الف", "ب", "پ", "ت", "ث", "ج"):
        await categories.create(session, name=name, parent_id=root.id)
    await session.commit()

    await app_settings.set_many(session, {"appearance.category_columns": 99})
    await session.commit()
    await _open(recording_bot, feed_update, CatCB(action="open", category_id=root.id).pack())

    widths = [len(row) for row in _keyboard(recording_bot)][:-1]
    assert max(widths) <= 4, f"an out-of-range column count reached the keyboard: {widths}"


async def test_plan_columns_setting_is_honoured(session, customer, plan, bound_notifier, recording_bot, feed_update):
    from app.db.models import Plan

    root = await categories.create(session, name="سرویس‌ها")
    plan.category_id = root.id
    session.add_all(
        [
            Plan(name="پلن دو", price_rial=2_000, is_active=True, category_id=root.id),
            Plan(name="پلن سه", price_rial=3_000, is_active=True, category_id=root.id),
        ]
    )
    await session.commit()

    payload = CatCB(action="open", category_id=root.id).pack()
    await _open(recording_bot, feed_update, payload)
    # One plan per row by default …
    assert all(len(row) == 1 for row in _keyboard(recording_bot)[:-1])

    await app_settings.set_many(session, {"appearance.plan_columns": 2})
    await session.commit()
    await _open(recording_bot, feed_update, payload)
    plan_rows = [row for row in _keyboard(recording_bot)[:-1] if any("پلن" in text for text in row)]
    assert plan_rows and all(len(row) == 2 for row in plan_rows), f"plans did not pair up: {plan_rows}"
