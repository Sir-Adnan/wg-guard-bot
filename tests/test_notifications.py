"""Outbound messaging: the guarantee that the *right* method gets the right kwargs.

A receipt photo is the case that matters: ``Bot.send_photo()`` has no
``link_preview_options`` parameter, so passing one raised ``TypeError`` inside
the sender and every receipt copy silently failed to reach a reviewer.

The doubles live in ``conftest`` (``recording_bot`` / ``sender``) so any test
that needs outbound traffic can assert on it.
"""

from __future__ import annotations

import pytest

from app.services.notifications import Media, Notifier

pytestmark = pytest.mark.anyio


async def test_a_text_message_disables_the_link_preview(sender: Notifier, recording_bot) -> None:
    await sender.send(1, "hello")

    kwargs = recording_bot.kwargs_of("send_message")
    assert kwargs["link_preview_options"].is_disabled is True
    assert kwargs["text"] == "hello"


async def test_a_disabled_preview_can_be_switched_off(sender: Notifier, recording_bot) -> None:
    await sender.send(1, "hello", disable_preview=False)

    assert "link_preview_options" not in recording_bot.kwargs_of("send_message")


@pytest.mark.parametrize("kind", ["photo", "document", "video", "animation"])
async def test_a_media_send_never_receives_message_only_keywords(sender: Notifier, recording_bot, kind: str) -> None:
    """This is the production ``TypeError`` — one assertion per method."""
    await sender.send(1, "caption", media=Media(kind=kind, file_id="file-id"))  # type: ignore[arg-type]

    kwargs = recording_bot.kwargs_of(f"send_{kind}")
    assert "link_preview_options" not in kwargs
    assert "text" not in kwargs
    assert kwargs["caption"] == "caption"
    assert kwargs[kind] == "file-id"  # the file field matches the method


async def test_an_edit_of_a_media_message_keeps_the_attachment(sender: Notifier, recording_bot) -> None:
    assert await sender.edit(1, 5, "new caption", media=Media(kind="photo", file_id="f")) is True

    kwargs = recording_bot.kwargs_of("edit_message_media")
    assert kwargs["media"].media == "f"
    assert kwargs["media"].caption == "new caption"


async def test_an_edit_without_media_uses_the_text_method(sender: Notifier, recording_bot) -> None:
    assert await sender.edit(1, 5, "new text") is True
    assert recording_bot.methods() == ["edit_message_text"]


async def test_an_unbound_notifier_drops_the_message_instead_of_raising() -> None:
    """The panel must stay usable when ``BOT_TOKEN`` is unset."""
    notifier = Notifier()

    assert await notifier.send(1, "hello") is None
    assert await notifier.edit(1, 2, "hello") is False
