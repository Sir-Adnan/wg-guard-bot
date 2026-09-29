"""Panel owner bootstrap and the password-recovery CLI.

Regression: the owner account used to be created inside a bot update, in the very
session the handler shares.  ``DatabaseMiddleware`` rolls that session back when a
handler raises, so on a fresh installation â€” where ``/start`` crashed â€” the row
never survived, and the panel answered Â«Ù†Ø§Ù… Ú©Ø§Ø±Ø¨Ø±ÛŒ ÛŒØ§ Ø±Ù…Ø² Ø¹Ø¨ÙˆØ± Ù†Ø§Ø¯Ø±Ø³Øª Ø§Ø³ØªÂ» for the
password the installer had just printed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.security import password_strength_error, verify_password
from app.db.models import Staff, StaffRole
from app.services import bootstrap

ADMIN_ID = 733474221
OWNER_PASSWORD = "Panel-Pass-1"

#: Tests that touch PostgreSQL are marked individually, so the password-policy
#: checks below still run in the database-free lane.


@pytest.fixture
def owner_env(monkeypatch):
    """Owner credentials exactly as the installer writes them into ``.env``."""
    monkeypatch.setattr(settings, "owner_username", "admin")
    monkeypatch.setattr(settings, "owner_password", OWNER_PASSWORD)
    monkeypatch.setattr(settings, "admin_ids", str(ADMIN_ID))
    return settings


@pytest.fixture
def app_session_factory(monkeypatch, engine):
    """Point the application's own session factory at the throwaway database.

    The startup seeder and the CLI open their own sessions, so a test that wants
    to exercise them has to redirect ``app.db.session`` rather than pass one in.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    import app.db.session as db_session

    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(db_session, "_session_factory", factory)
    return factory


# ---------------------------------------------------------------------------
# The row itself
# ---------------------------------------------------------------------------
@pytest.mark.db
async def test_the_owner_account_is_created_from_the_environment(session, owner_env):
    row = await bootstrap.ensure_owner_account(session)

    assert row.login == "admin"
    assert row.role is StaffRole.OWNER
    assert row.telegram_id == ADMIN_ID
    assert verify_password(OWNER_PASSWORD, row.password_hash)


@pytest.mark.db
async def test_bootstrapping_twice_changes_nothing(session, owner_env):
    first = await bootstrap.ensure_owner_account(session)
    second = await bootstrap.ensure_owner_account(session)

    assert first.id == second.id
    rows = (await session.execute(select(Staff).where(Staff.login == "admin"))).scalars().all()
    assert len(rows) == 1


@pytest.mark.db
async def test_the_bot_created_admin_row_is_adopted_instead_of_duplicated(session, owner_env):
    """``staff.telegram_id`` is unique â€” two rows for one person break the bot."""
    existing = Staff(telegram_id=ADMIN_ID, name="Ù…Ø§Ù„Ú©", role=StaffRole.OWNER, receive_receipts=True)
    session.add(existing)
    await session.flush()

    row = await bootstrap.ensure_owner_account(session)

    assert row.id == existing.id
    assert row.login == "admin"
    assert verify_password(OWNER_PASSWORD, row.password_hash)


@pytest.mark.db
async def test_a_row_without_a_password_hash_is_repaired(session, owner_env):
    session.add(Staff(login="admin", name="Ù…Ø§Ù„Ú©", role=StaffRole.OWNER, receive_receipts=True))
    await session.flush()

    row = await bootstrap.ensure_owner_account(session)

    assert verify_password(OWNER_PASSWORD, row.password_hash)


@pytest.mark.db
async def test_a_password_changed_in_the_panel_survives_a_restart(session, owner_env):
    await bootstrap.ensure_owner_account(session)
    await bootstrap.set_panel_password(session, login="admin", password="Changed-In-Panel-2")

    row = await bootstrap.ensure_owner_account(session)  # the next boot

    assert verify_password("Changed-In-Panel-2", row.password_hash)
    assert not verify_password(OWNER_PASSWORD, row.password_hash)


# ---------------------------------------------------------------------------
# Startup, the update path, and recovery
# ---------------------------------------------------------------------------
@pytest.mark.db
async def test_a_failing_update_cannot_lose_the_owner(app_session_factory, session, owner_env):
    """The bug, end to end: the row is committed before any update arrives."""
    from aiogram.types import Chat, Message
    from aiogram.types import User as TelegramUser

    from app.bot.middlewares.context import ContextMiddleware, DatabaseMiddleware

    owner = await bootstrap.seed_panel_owner()

    async def failing_handler(event, data):
        raise RuntimeError("handler blew up")

    database = DatabaseMiddleware()
    context = ContextMiddleware()

    async def invoke(event, data):
        async def next_handler(inner_event, inner_data):
            return await context(failing_handler, inner_event, inner_data)

        return await database(next_handler, event, data)

    telegram_user = TelegramUser(id=ADMIN_ID, is_bot=False, first_name="Owner")
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=ADMIN_ID, type="private"),
        from_user=telegram_user,
        text="/start",
    )

    with pytest.raises(RuntimeError):
        await invoke(message, {"event_from_user": telegram_user})

    stored = (await session.execute(select(Staff).where(Staff.login == "admin"))).scalar_one()
    assert stored.id == owner.id, "the rollback took the panel login with it"
    assert verify_password(OWNER_PASSWORD, stored.password_hash)


@pytest.mark.db
async def test_the_cli_sets_a_password_without_echoing_it(app_session_factory, session, capsys):
    from app import cli

    code = await cli.main(["set-password", "--login", "admin", "--password", "Recovered-Pass-9"])

    assert code == 0
    printed = capsys.readouterr().out
    assert "Recovered-Pass-9" not in printed
    assert "admin" in printed

    row = (await session.execute(select(Staff).where(Staff.login == "admin"))).scalar_one()
    assert row.role is StaffRole.OWNER
    assert verify_password("Recovered-Pass-9", row.password_hash)


async def test_the_cli_refuses_a_weak_password(capsys):
    from app import cli

    code = await cli.main(["set-password", "--login", "admin", "--password", "short"])

    assert code == 2
    assert "rejected" in capsys.readouterr().err


def test_generated_passwords_always_satisfy_the_panel_policy():
    """Twenty random characters contain no digit about 3 % of the time."""
    from app import cli

    for _ in range(500):
        password = cli._generate_password()
        assert len(password) == 20
        assert password_strength_error(password) is None


@pytest.mark.db
async def test_the_cli_generates_a_password_that_signs_in(app_session_factory, session, capsys):
    from app import cli

    assert await cli.main(["set-password", "--login", "admin", "--generate"]) == 0
    password = capsys.readouterr().out.split("Generated password: ", 1)[1].splitlines()[0]

    row = (await session.execute(select(Staff).where(Staff.login == "admin"))).scalar_one()
    assert verify_password(password, row.password_hash)
