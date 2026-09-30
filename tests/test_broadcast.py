"""Broadcast (پیام همگانی): audience packing, delivery, keyboard and the panel form.

Two layers are exercised here:

* the **pure** helpers — the audience index round trip and keyboard validation —
  where a mistake silently sends the wrong thing to thousands of people;
* the **delivery seam** (:meth:`BroadcastService._deliver`) and the panel POST
  that creates and starts a campaign, with the process-wide notifier bound to
  the recording bot double from ``conftest``.

The module is database-marked like every other DB-touching test; the pure tests
request no fixtures, so they never open a connection even when they run.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from app.core.errors import ValidationError
from app.db.models import Broadcast, BroadcastStatus
from app.services.broadcast import (
    AUDIENCE_KEYS,
    AUDIENCES,
    FALLBACK_AUDIENCE,
    MAX_BUTTONS,
    BroadcastButton,
    audience_index,
    audience_is_known,
    audience_key,
    audience_label,
    broadcasts,
    keyboard_for,
    validate_buttons,
)
from app.services.notifications import Media

pytestmark = pytest.mark.db

PANEL_PASSWORD = "Owner-pass-123"
BROADCAST_PATH = "/panel/broadcast"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def world_bot(recording_bot):
    """Bind the **process-wide** notifier — the service uses the singleton."""
    from app.services.notifications import notifier as world

    world.bind(recording_bot)  # type: ignore[arg-type]
    yield recording_bot
    world.bind(None)  # type: ignore[arg-type]


def _extract_csrf(html: str) -> str:
    marker = 'name="csrf_token" value="'
    start = html.find(marker)
    assert start != -1, "the broadcast page has no CSRF token"
    start += len(marker)
    return html[start : html.find('"', start)]


def _form_data(csrf: str, **overrides: str) -> dict[str, str]:
    """The panel form exactly as a browser submits it."""
    data = {
        "csrf_token": csrf,
        "text": "سلام {name} عزیز",
        "audience": "all",
        "media_type": "",
        "media_file_id": "",
        "scheduled_at": "",
        "buttons": "",
    }
    data.update(overrides)
    return data


async def _create(session, **kwargs: Any) -> Broadcast:
    text = kwargs.pop("text", "سلام {name}")
    row = await broadcasts.create(session, None, text=text, start_immediately=False, **kwargs)
    await session.commit()
    return row


async def _wait_for_status(session, broadcast_id: int, status: BroadcastStatus, *, timeout: float = 15.0) -> Broadcast:
    """Let the background campaign finish; it shares this event loop."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        row = await session.get(Broadcast, broadcast_id)
        if row is not None:
            await session.refresh(row)
            if row.status is status:
                return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"broadcast {broadcast_id} never reached {status}")


# ---------------------------------------------------------------------------
# Audience packing (pure)
# ---------------------------------------------------------------------------
def test_every_audience_key_round_trips() -> None:
    """The callback carries an index; every key must survive the round trip."""
    for key in AUDIENCE_KEYS:
        index = audience_index(key)
        assert audience_is_known(index), key
        assert audience_key(index) == key
    # The fallback is index 0 by construction — the packers rely on it.
    assert AUDIENCE_KEYS[0] == FALLBACK_AUDIENCE


def test_an_unknown_audience_means_all() -> None:
    assert audience_index("does_not_exist") == 0
    assert audience_key(len(AUDIENCE_KEYS)) == FALLBACK_AUDIENCE
    assert audience_key(-1) == FALLBACK_AUDIENCE
    assert not audience_is_known(len(AUDIENCE_KEYS))
    assert not audience_is_known(-1)


def test_every_audience_has_a_persian_label() -> None:
    for key in AUDIENCE_KEYS:
        label = audience_label(key)
        assert label == AUDIENCES[key]
        assert label != key


# ---------------------------------------------------------------------------
# Inline keyboard
# ---------------------------------------------------------------------------
def test_a_javascript_url_is_refused() -> None:
    with pytest.raises(ValidationError):
        validate_buttons([BroadcastButton(text="بد", url="javascript:alert(1)")])


def test_an_empty_label_is_refused() -> None:
    with pytest.raises(ValidationError):
        validate_buttons([BroadcastButton(text="   ", url="https://t.me/x")])


def test_more_than_six_buttons_are_refused() -> None:
    rows = [BroadcastButton(text=f"دکمه {i}", url="https://t.me/x") for i in range(MAX_BUTTONS + 1)]
    with pytest.raises(ValidationError):
        validate_buttons(rows)


async def test_a_dangerous_url_is_dropped_at_delivery_time() -> None:
    """A stored row must not cost the whole campaign — the bad button is skipped."""
    markup = await keyboard_for(
        [
            {"text": "بد", "url": "javascript:alert(1)"},
            {"text": "خوب", "url": "https://t.me/shop"},
        ]
    )
    assert markup is not None
    assert [button.url for row in markup.inline_keyboard for button in row] == ["https://t.me/shop"]


async def test_the_keyboard_is_capped_and_two_per_row() -> None:
    markup = await keyboard_for([{"text": f"دکمه {i}", "url": "https://t.me/x"} for i in range(9)])
    assert markup is not None
    assert sum(len(row) for row in markup.inline_keyboard) == MAX_BUTTONS
    assert all(len(row) <= 2 for row in markup.inline_keyboard)


async def test_emoji_placeholders_resolve_inside_button_labels() -> None:
    markup = await keyboard_for([{"text": "{e:cart} خرید", "url": "https://t.me/x"}])
    assert markup is not None
    label = markup.inline_keyboard[0][0]
    assert "{e:cart}" not in label.text
    assert "خرید" in label.text
    # No custom emoji is configured in a fresh database, so the Unicode twin shows.
    assert "🛒" in label.text


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
async def test_a_composed_message_is_copied_verbatim(session, world_bot) -> None:
    row = await _create(
        session,
        text="متن پست",
        audience="all",
        buttons=[{"text": "{e:cart} خرید", "url": "https://t.me/shop"}],
        source_chat_id=987_654,
        source_message_id=321,
    )

    payload = await broadcasts.prepare(session, row)
    assert payload.copies_source
    assert await broadcasts._deliver(payload, 555_000) is True

    assert world_bot.methods() == ["copy_message"]
    kwargs = world_bot.kwargs_of("copy_message")
    assert kwargs["chat_id"] == 555_000
    assert kwargs["from_chat_id"] == 987_654
    assert kwargs["message_id"] == 321
    keyboard = kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].url == "https://t.me/shop"
    assert "خرید" in keyboard.inline_keyboard[0][0].text


async def test_a_text_campaign_renders_emoji_and_placeholders(session, world_bot) -> None:
    row = await _create(session, text="{e:cart} سلام {name} از {shop}")

    payload = await broadcasts.prepare(session, row)
    assert not payload.copies_source
    assert payload.personalized
    assert await broadcasts._deliver(payload, 111_222, name="<b>علی</b>") is True

    assert world_bot.methods() == ["send_message"]
    kwargs = world_bot.kwargs_of("send_message")
    assert kwargs["chat_id"] == 111_222
    body = kwargs["text"]
    assert "{e:cart}" not in body
    assert "🛒" in body
    assert "{name}" not in body
    # The name is customer data and the message is HTML: it must arrive escaped.
    assert "&lt;b&gt;علی&lt;/b&gt;" in body
    assert "{shop}" not in body


async def test_a_campaign_without_name_is_rendered_once(session, world_bot) -> None:
    row = await _create(session, text="فقط {shop}", audience="all")

    payload = await broadcasts.prepare(session, row)
    assert not payload.personalized
    assert await broadcasts.deliver_one(session, row, 444_555) is True

    assert world_bot.methods() == ["send_message"]
    assert "{shop}" not in world_bot.kwargs_of("send_message")["text"]


async def test_a_media_campaign_uses_send_photo(session, world_bot) -> None:
    row = await _create(session, text="کپشن پیام", media=Media(kind="photo", file_id="AgAC-photo"))

    payload = await broadcasts.prepare(session, row)
    assert await broadcasts._deliver(payload, 777_888) is True

    assert world_bot.methods() == ["send_photo"]
    kwargs = world_bot.kwargs_of("send_photo")
    assert kwargs["photo"] == "AgAC-photo"
    assert kwargs["caption"] == "کپشن پیام"


async def test_a_started_campaign_walks_the_audience_and_finishes(session, world_bot, customer) -> None:
    row = await _create(session, text="سلام", audience="all")
    assert await broadcasts.start(row.id) is True

    finished = await _wait_for_status(session, row.id, BroadcastStatus.DONE)
    assert finished.total == 1  # the single customer fixture
    assert finished.sent == 1
    assert finished.failed == 0
    assert finished.started_at is not None
    assert finished.finished_at is not None
    assert world_bot.methods() == ["send_message"]


# ---------------------------------------------------------------------------
# Panel form
# ---------------------------------------------------------------------------
async def test_the_panel_creates_and_starts_a_campaign(
    signed_in_client: httpx.AsyncClient, owner, customer, session, world_bot
) -> None:
    # The request runs in its own session, so the audience must be committed first.
    await session.commit()

    page = await signed_in_client.get(BROADCAST_PATH)
    assert page.status_code == 200
    assert 'name="buttons"' in page.text  # the keyboard editor lives on the page
    csrf = _extract_csrf(page.text)

    created = await signed_in_client.post(
        BROADCAST_PATH,
        data=_form_data(csrf, buttons="خرید سرویس | https://t.me/shop"),
    )
    assert created.status_code == 303
    assert created.headers["location"].endswith("/panel/broadcast")

    rows = await broadcasts.list_recent(session)
    assert len(rows) == 1
    row = rows[0]
    assert row.audience == "all"
    assert row.buttons == [{"text": "خرید سرویس", "url": "https://t.me/shop"}]
    assert row.total == 1

    finished = await _wait_for_status(session, row.id, BroadcastStatus.DONE)
    assert finished.sent == 1
    assert world_bot.methods() == ["send_message"]
    assert "{name}" not in world_bot.kwargs_of("send_message")["text"]


async def test_the_panel_shows_the_audience_label_not_the_key(
    signed_in_client: httpx.AsyncClient, owner, session
) -> None:
    await _create(session, text="سلام", audience="with_balance")

    page = await signed_in_client.get(BROADCAST_PATH)
    assert page.status_code == 200
    table = page.text.split("<table", 1)[1].split("</table>", 1)[0]
    assert AUDIENCES["with_balance"] in table
    assert "with_balance" not in table


async def test_the_panel_refuses_a_javascript_button(signed_in_client: httpx.AsyncClient, owner, session) -> None:
    page = await signed_in_client.get(BROADCAST_PATH)
    csrf = _extract_csrf(page.text)

    response = await signed_in_client.post(
        BROADCAST_PATH,
        data=_form_data(csrf, buttons="بد | javascript:alert(1)"),
    )
    assert response.status_code == 303
    assert await broadcasts.list_recent(session) == []

    follow_up = await signed_in_client.get(BROADCAST_PATH)
    assert "https://" in follow_up.text  # the flash names the accepted scheme


async def test_an_unbound_notifier_flashes_instead_of_failing(
    signed_in_client: httpx.AsyncClient, owner, session
) -> None:
    """The bot may be off: the operator gets a Persian flash, never a 500."""
    from app.services.notifications import notifier as world

    world.bind(None)  # type: ignore[arg-type]

    page = await signed_in_client.get(BROADCAST_PATH)
    csrf = _extract_csrf(page.text)

    created = await signed_in_client.post(BROADCAST_PATH, data=_form_data(csrf))
    assert created.status_code == 303
    assert "wggb_flash" in created.cookies

    follow_up = await signed_in_client.get(BROADCAST_PATH)
    assert follow_up.status_code == 200
    assert "ربات تلگرام فعال نیست" in follow_up.text

    # The intent survives as a draft, so the operator can retry once the bot is back.
    rows = await broadcasts.list_recent(session)
    assert len(rows) == 1
    assert rows[0].status is BroadcastStatus.DRAFT


async def test_the_start_button_flashes_when_the_bot_is_off(
    signed_in_client: httpx.AsyncClient, owner, session
) -> None:
    """/start on an existing draft must explain itself too, not answer 500."""
    from app.services.notifications import notifier as world

    world.bind(None)  # type: ignore[arg-type]
    row = await _create(session, text="سلام", audience="all")

    page = await signed_in_client.get(BROADCAST_PATH)
    csrf = _extract_csrf(page.text)
    response = await signed_in_client.post(f"{BROADCAST_PATH}/{row.id}/start", data={"csrf_token": csrf})

    assert response.status_code == 303
    assert "wggb_flash" in response.cookies
    follow_up = await signed_in_client.get(BROADCAST_PATH)
    assert "ربات تلگرام فعال نیست" in follow_up.text
