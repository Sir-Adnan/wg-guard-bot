"""The system-event sink: a severity goes in, one row and one log line come out.

Every caller of ``record_event`` sits on an error path — a bot start-up Telegram
refused, a handler that raised, a receipt nobody could be told about — so this is
the one function that must not raise on its way in. The severity arrives both as
an ``EventLevel`` and as the plain string an error path happens to have at hand.

Deliberately **not** marked ``pytest.mark.anyio``, unlike ``test_notifications.py``:
that mark runs a test on anyio's own event loop while the database fixtures live
on pytest-asyncio's session-scoped loop, and the connection then cannot be closed
(``RuntimeError: … attached to a different loop``). Database tests belong in a
module without the mark.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import EventLevel, SystemEvent
from app.services.notifications import coerce_event_level, notifier


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("info", EventLevel.INFO),
        ("warning", EventLevel.WARNING),
        ("CRITICAL", EventLevel.CRITICAL),
        (" Error ", EventLevel.ERROR),
        (EventLevel.WARNING, EventLevel.WARNING),
        ("nonsense", EventLevel.INFO),
        ("", EventLevel.INFO),
    ],
)
def test_a_severity_resolves_from_either_a_name_or_a_value(given, expected: EventLevel) -> None:
    """The sink is reached from error paths, so it never raises over wording."""
    assert coerce_event_level(given) is expected


@pytest.mark.db
async def test_a_severity_written_as_a_string_is_recorded(session) -> None:
    """The CI failure, pinned.

    ``lifespan`` records a refused bot token as ``"critical"``. The sink asked the
    string for ``.value``, so the *failure handler* raised ``AttributeError`` and
    masked the real error — and the same shape of call sat in the bot's error
    handler, the receipt notifier and the membership check.
    """
    await notifier.record_event("critical", "راه‌اندازی ربات ناموفق بود", source="startup", session=session)

    stored = (await session.execute(select(SystemEvent).where(SystemEvent.source == "startup"))).scalar_one()
    assert stored.level is EventLevel.CRITICAL


@pytest.mark.db
async def test_error_reporting_does_not_persist_untrusted_exception_payloads(session, caplog) -> None:
    await notifier.report_error(ValueError("PrivateKey = secret-canary"), source="redaction", notify=False)
    stored = (await session.execute(select(SystemEvent).where(SystemEvent.source == "redaction"))).scalar_one()
    assert stored.message == "ValueError"
    assert "secret-canary" not in caplog.text
