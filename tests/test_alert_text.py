"""Callback alerts are plain text.

Telegram's ``answerCallbackQuery`` does not parse HTML, so a screen's Persian
copy — ``{e:info} <b>…</b>`` — reached the customer as source code:
``<b>این دکمه دیگر معتبر نیست</b>`` in a popup.  Whatever a text key contains,
an alert has to be readable.  (The dispatcher-level check, on a real rendered
message, lives in ``tests/test_staff_callbacks.py``.)
"""

from __future__ import annotations

import json
from pathlib import Path

from app.bot.utils import ALERT_LIMIT, alert_text


def test_tags_are_stripped_and_entities_unescaped() -> None:
    assert alert_text("<b>سلام</b>\n\n<i>دنیا</i>") == "سلام دنیا"
    assert alert_text("۵ > ۳ و ۲ < ۴") == "۵ > ۳ و ۲ < ۴"
    assert alert_text("کارت &lt;۱۲۳۴&gt;") == "کارت <۱۲۳۴>"


def test_a_premium_emoji_tag_keeps_its_unicode_fallback() -> None:
    """``render_emoji`` produces ``<tg-emoji>``; the alert keeps what it wraps."""
    assert alert_text('<tg-emoji emoji-id="123">ℹ️</tg-emoji> این دکمه معتبر نیست') == "ℹ️ این دکمه معتبر نیست"


def test_a_long_body_is_cut_to_the_alert_limit() -> None:
    out = alert_text("ی" * 500)
    assert len(out) == ALERT_LIMIT
    assert out.endswith("…")


def test_every_text_key_that_is_shown_as_an_alert_has_no_markup() -> None:
    """The keys the handlers feed to ``callback.answer`` must stay tag-free."""
    path = Path(__file__).resolve().parent.parent / "app" / "locales" / "fa.json"
    with open(path, encoding="utf-8") as handle:
        messages = json.load(handle)

    for key in (
        "error.expired_action",
        "test.disabled",
        "test.cooldown",
        "support.closed",
        "start.not_joined",
        "shop.sold_out",
        "buy.cannot_restart",
    ):
        out = alert_text(messages[key])
        assert "<" not in out and ">" not in out, f"{key} still shows markup in an alert"
        assert "\n" not in out, f"{key} is not a single line once collapsed"
