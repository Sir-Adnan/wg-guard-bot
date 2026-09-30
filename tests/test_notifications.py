"""Outbound messaging: the guarantee that the *right* method gets the right kwargs.

A receipt photo is the case that matters: ``Bot.send_photo()`` has no
``link_preview_options`` parameter, so passing one raised ``TypeError`` inside
the sender and every receipt copy silently failed to reach a reviewer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.services.notifications import Media, Notifier

pytestmark = pytest.mark.anyio


@dataclass
class _Sent:
    message_id: int = 4242


@dataclass
class _RecordingBot:
    """Minimal stand-in for ``aiogram.Bot`` that records what it was called with."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def _record(self, name: str, kwargs: dict[str, Any]) -> _Sent:
        self.calls.append((name, kwargs))
        return _Sent()

    async def send_message(self, **kwargs: Any) -> _Sent:
        return self._record("send_message", kwargs)

    async def send_photo(self, **kwargs: Any) -> _Sent:
        return self._record("send_photo", kwargs)

    async def send_document(self, **kwargs: Any) -> _Sent:
        return self._record("send_document", kwargs)

    async def send_video(self, **kwargs: Any) -> _Sent:
        return self._record("send_video", kwargs)

    async def send_animation(self, **kwargs: Any) -> _Sent:
        return self._record("send_animation", kwargs)

    async def edit_message_text(self, **kwargs: Any) -> _Sent:
        return self._record("edit_message_text", kwargs)

    async def edit_message_caption(self, **kwargs: Any) -> _Sent:
        return self._record("edit_message_caption", kwargs)

    async def edit_message_media(self, **kwargs: Any) -> _Sent:
        return self._record("edit_message_media", kwargs)


@pytest.fixture
def bot() -> _RecordingBot:
    return _RecordingBot()


@pytest.fixture
def sender(bot: _RecordingBot) -> Notifier:
    notifier = Notifier()
    notifier.bind(bot)  # type: ignore[arg-type]
    return notifier


async def test_a_text_message_disables_the_link_preview(sender: Notifier, bot: _RecordingBot) -> None:
    await sender.send(1, "hello")

    name, kwargs = bot.calls[0]
    assert name == "send_message"
    assert kwargs["link_preview_options"].is_disabled is True
    assert kwargs["text"] == "hello"


async def test_a_disabled_preview_can_be_switched_off(sender: Notifier, bot: _RecordingBot) -> None:
    await sender.send(1, "hello", disable_preview=False)

    assert "link_preview_options" not in bot.calls[0][1]


@pytest.mark.parametrize("kind", ["photo", "document", "video", "animation"])
async def test_a_media_send_never_receives_message_only_keywords(
    sender: Notifier, bot: _RecordingBot, kind: str
) -> None:
    """This is the production ``TypeError`` — one assertion per method."""
    await sender.send(1, "caption", media=Media(kind=kind, file_id="file-id"))  # type: ignore[arg-type]

    name, kwargs = bot.calls[0]
    assert name == f"send_{kind}"
    assert "link_preview_options" not in kwargs
    assert "text" not in kwargs
    assert kwargs["caption"] == "caption"
    assert kwargs[kind] == "file-id"  # the file field matches the method


async def test_an_edit_of_a_media_message_keeps_the_attachment(sender: Notifier, bot: _RecordingBot) -> None:
    assert await sender.edit(1, 5, "new caption", media=Media(kind="photo", file_id="f")) is True

    name, kwargs = bot.calls[0]
    assert name == "edit_message_media"
    assert kwargs["media"].media == "f"
    assert kwargs["media"].caption == "new caption"


async def test_an_edit_without_media_uses_the_text_method(sender: Notifier, bot: _RecordingBot) -> None:
    assert await sender.edit(1, 5, "new text") is True
    assert bot.calls[0][0] == "edit_message_text"


async def test_an_unbound_notifier_drops_the_message_instead_of_raising() -> None:
    """The panel must stay usable when ``BOT_TOKEN`` is unset."""
    notifier = Notifier()

    assert await notifier.send(1, "hello") is None
    assert await notifier.edit(1, 2, "hello") is False
