"""First-run bootstrap: the web-panel owner account.

Why this is its own module
--------------------------
Panel sign-in needs a ``staff`` row with a password hash, and the row has to
exist *before* anyone can reach the panel.  Seeding it from the bot middleware
looks convenient but is wrong: the middleware shares its session with the
handler, so a handler that raises rolls the owner row back — and a fresh
installation stays permanently un-loggable-in until some update happens to
succeed.  Seeding therefore happens at startup, in its own committed session,
and this module owns that row.

Both entry points are idempotent, so they can run on every boot.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import hash_password
from app.db.models import Staff, StaffRole
from app.db.session import session_scope

log = get_logger(__name__)


def owner_login() -> str:
    """The panel login the owner account must have."""
    return settings.owner_username.strip() or "admin"


async def _by_login(session: AsyncSession, login: str) -> Staff | None:
    return (await session.execute(select(Staff).where(Staff.login == login))).scalar_one_or_none()


async def _unclaimed_owner_row(session: AsyncSession) -> Staff | None:
    """The primary admin's bot-created row, before it has panel credentials."""
    ids = settings.admin_id_list
    if not ids:
        return None
    rows = await session.execute(
        select(Staff).where(Staff.telegram_id == ids[0], Staff.login.is_(None)).order_by(Staff.id)
    )
    return rows.scalars().first()


async def _claim_login(session: AsyncSession, login: str) -> tuple[Staff, bool]:
    """Find the operator behind ``login``, or create/adopt one.

    The configured owner login adopts the row the bot created for the primary
    admin instead of inserting a twin: ``staff.telegram_id`` is unique, so two
    rows for one person would break staff resolution on the next update.
    """
    row = await _by_login(session, login)
    if row is not None:
        return row, False

    created = True
    if login == owner_login():
        row = await _unclaimed_owner_row(session)
        created = row is None
    if row is None:
        row = Staff(
            telegram_id=settings.admin_id_list[0] if (login == owner_login() and settings.admin_id_list) else None,
            name="مالک",
            role=StaffRole.OWNER,
            receive_receipts=True,
        )
        session.add(row)

    row.login = login
    await session.flush()
    return row, created


async def set_panel_password(session: AsyncSession, *, login: str, password: str) -> Staff:
    """Force a new panel password for ``login``, creating the account if needed."""
    row, _created = await _claim_login(session, login)
    row.password_hash = hash_password(password)
    await session.flush()
    log.info("Panel password set for %r", login)
    return row


async def ensure_owner_account(session: AsyncSession) -> Staff:
    """Make sure the owner can sign in.

    Only a *missing* hash is filled from ``OWNER_PASSWORD``: a password the
    operator changed inside the panel must survive the next restart.
    """
    login = owner_login()
    row, created = await _claim_login(session, login)

    env_password = settings.owner_password.strip()
    if row.password_hash is None and env_password:
        row.password_hash = hash_password(env_password)
        await session.flush()
        if created:
            log.info("Seeded web-panel owner account %r", login)
        else:
            log.warning("Panel owner %r had no password hash; it was set from OWNER_PASSWORD", login)

    if row.password_hash is None:
        log.error("Panel owner %r has no password hash and OWNER_PASSWORD is empty — the panel cannot be used", login)
    if not row.is_active:
        log.warning("Panel owner %r is disabled; re-enable the account from the panel", login)
    return row


async def seed_panel_owner() -> Staff:
    """Startup entry point: its own session, its own commit."""
    async with session_scope() as session:
        return await ensure_owner_account(session)


__all__ = [
    "ensure_owner_account",
    "owner_login",
    "seed_panel_owner",
    "set_panel_password",
]
