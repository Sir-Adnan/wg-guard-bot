"""Admin-panel authentication: signed cookies, CSRF and role checks."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.jalali import now_utc
from app.core.logging import get_logger
from app.core.security import (
    csrf_token_for,
    hash_password,
    load_session,
    sign_session,
    verify_password,
)
from app.db.models import Staff, StaffRole

log = get_logger(__name__)

SESSION_COOKIE = "wggb_session"
FLASH_COOKIE = "wggb_flash"
SESSION_MAX_AGE = 60 * 60 * 12  # 12 hours


@dataclass(slots=True)
class SessionInfo:
    staff_id: int
    sid: str
    role: str

    @property
    def csrf_token(self) -> str:
        return csrf_token_for(self.sid)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
async def authenticate(session: AsyncSession, login: str, password: str) -> Staff | None:
    """Verify credentials; returns the staff row on success."""
    row = (await session.execute(select(Staff).where(Staff.login == login.strip()))).scalar_one_or_none()
    if row is None or not row.is_active or not row.password_hash:
        # Constant-ish time: still run a hash comparison against a dummy value.
        verify_password(password, hash_password("dummy-password-for-timing"))
        return None
    if not verify_password(password, row.password_hash):
        log.warning("Failed panel login for %r", login)
        return None

    row.last_login_at = now_utc()
    await session.flush()
    return row


def issue_session(staff: Staff) -> tuple[str, SessionInfo]:
    sid = secrets.token_urlsafe(16)
    token = sign_session({"sid": sid, "uid": staff.id, "role": staff.role.value})
    return token, SessionInfo(staff_id=staff.id, sid=sid, role=staff.role.value)


def read_session(request: Request) -> SessionInfo | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    data: dict[str, Any] | None = load_session(raw, max_age=SESSION_MAX_AGE)
    if not data:
        return None
    try:
        return SessionInfo(sid=str(data["sid"]), staff_id=int(data["uid"]), role=str(data["role"]))
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------
async def get_db_session():  # pragma: no cover - thin wrapper, exercised via routes
    from app.db.session import get_db

    async for session in get_db():
        yield session


async def current_staff(request: Request, session: AsyncSession = Depends(get_db_session)) -> Staff | None:
    """Resolve the logged-in operator (or ``None``)."""
    info = read_session(request)
    if info is None:
        return None
    staff = await session.get(Staff, info.staff_id)
    if staff is None or not staff.is_active:
        return None
    if staff.role.value != info.role:
        # Role changed since the cookie was issued — force a fresh login.
        return None
    request.state.session_info = info
    return staff


def login_redirect(request: Request) -> RedirectResponse:
    target = request.url.path
    url = f"{settings.panel_prefix}/login"
    if target and target not in ("/", settings.panel_prefix, f"{settings.panel_prefix}/"):
        url += f"?next={target}"
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


async def require_staff(request: Request, staff: Staff | None = Depends(current_staff)) -> Staff:
    if staff is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": login_redirect(request).headers["location"]},
        )
    # Templates read ``request.state.staff`` to render the layout header.
    request.state.staff = staff
    return staff


def require_roles(*roles: StaffRole):
    """Dependency **factory** enforcing the allowed roles.

    Usage::

        staff: Staff = Depends(require_manager())

    The extra call is deliberate: it keeps the check declarative at the
    decorator level and lets a future variant take parameters without changing
    every route signature.
    """

    def factory() -> Callable[..., Awaitable[Staff]]:
        async def checker(staff: Staff = Depends(require_staff)) -> Staff:
            if staff.role not in roles:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="دسترسی کافی ندارید.")
            return staff

        return checker

    return factory


require_owner = require_roles(StaffRole.OWNER)
require_manager = require_roles(StaffRole.OWNER, StaffRole.ADMIN)
require_any = require_roles(StaffRole.OWNER, StaffRole.ADMIN, StaffRole.SUPPORT)


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
def verify_csrf(request: Request, submitted: str | None) -> None:
    """Reject a state-changing request without a valid CSRF token."""
    info = read_session(request)
    if info is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="نشست منقضی شده است.")
    expected = info.csrf_token
    if not submitted or not secrets.compare_digest(submitted, expected):
        log.warning("CSRF validation failed for %s", request.url.path)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="درخواست نامعتبر (CSRF).")


__all__ = [
    "FLASH_COOKIE",
    "SESSION_COOKIE",
    "SESSION_MAX_AGE",
    "SessionInfo",
    "authenticate",
    "current_staff",
    "get_db_session",
    "issue_session",
    "login_redirect",
    "read_session",
    "require_any",
    "require_manager",
    "require_owner",
    "require_roles",
    "require_staff",
    "verify_csrf",
]
