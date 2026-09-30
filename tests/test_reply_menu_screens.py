"""Every physical button must actually draw its screen.

A reply-keyboard press has no callback data, so a screen that raises does not
look like a broken button — the error handler reports it to the operators and
the customer just sees nothing.  One shipped with ``kb.add(..., event=…)``: a
keyword the keyboard builder has never accepted, so «سرویس تست» and «حساب
کاربری» crashed on every press while every unit test stayed green.

This module feeds the real dispatcher one text message per button, exactly as
Telegram would, and fails if any of them reports a system error.
"""

from __future__ import annotations

import pytest

from app.bot import reply_menu
from app.bot.setup import reset_dispatcher
from app.db.models import Staff, StaffRole
from app.services.settings_store import app_settings

pytestmark = pytest.mark.db

STAFF_TELEGRAM_ID = 777_001
SYSTEM_ERROR_MARKER = "خطای سیستم"


@pytest.fixture(autouse=True)
def _clean_dispatcher():
    reset_dispatcher()
    yield
    reset_dispatcher()


async def _staff(session) -> Staff:
    row = Staff(
        name="مالک",
        role=StaffRole.OWNER,
        telegram_id=STAFF_TELEGRAM_ID,
        receive_receipts=True,
        is_active=True,
    )
    session.add(row)
    await session.flush()
    return row


async def test_every_reply_button_renders_a_screen(session, bound_notifier, recording_bot, feed_update) -> None:
    await app_settings.load(session, force=True)
    await app_settings.set_many(session, {"appearance.reply_keyboard": True})
    await _staff(session)
    await session.commit()

    buttons = await reply_menu.actions(session, is_staff=True)
    assert buttons, "an enabled keyboard with no buttons is a bug"

    for label, action in buttons:
        recording_bot.calls.clear()
        await feed_update(recording_bot, text=label, telegram_id=STAFF_TELEGRAM_ID)

        assert not any(SYSTEM_ERROR_MARKER in body for body in recording_bot.texts()), (
            f"the «{label}» button ({action}) raised: {recording_bot.texts()}"
        )
        assert recording_bot.calls, f"the «{label}» button ({action}) drew nothing at all"


async def test_a_typed_message_that_is_not_a_button_is_left_to_the_catch_all(
    session, bound_notifier, recording_bot, feed_update
) -> None:
    """The reply router must not swallow ordinary text.

    Its handler matches every stateless message, and a matched handler ends the
    dispatch — so a filter that accepted anything would leave a customer who
    typed a question with no answer at all.
    """
    await app_settings.load(session, force=True)
    await app_settings.set_many(session, {"appearance.reply_keyboard": True})
    await _staff(session)
    await session.commit()

    await feed_update(recording_bot, text="سلام، قیمت سرویس یک ماهه چنده؟", telegram_id=STAFF_TELEGRAM_ID)

    assert any("متوجه نشدم" in body for body in recording_bot.texts()), "the fallback never answered"
    assert not any(SYSTEM_ERROR_MARKER in body for body in recording_bot.texts())
