"""Panel authentication pages."""

from __future__ import annotations

from contextlib import suppress

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.web.deps import form_dict, form_str
from app.web.security import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    authenticate,
    current_staff,
    get_db_session,
    issue_session,
    read_session,
)
from app.web.templating import flash, render

log = get_logger(__name__)
router = APIRouter(tags=["auth"])

#: Failed attempts per IP before a short lockout.
MAX_ATTEMPTS = 6
LOCKOUT_SECONDS = 300


@router.get("/login")
async def login_page(request: Request, next: str = ""):
    if read_session(request) is not None:
        return RedirectResponse(f"{settings.panel_prefix}/", status_code=303)
    return render(
        request,
        "login.html",
        {"page_title": "ورود به پنل", "next": next or f"{settings.panel_prefix}/", "error": None},
    )


@router.post("/login")
async def login_submit(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    from app.core.cache import cache

    form = await form_dict(request)
    login = form_str(form, "login")
    password = form_str(form, "password")
    target = form_str(form, "next") or f"{settings.panel_prefix}/"
    if not target.startswith(settings.panel_prefix):
        target = f"{settings.panel_prefix}/"

    client_ip = request.client.host if request.client else "unknown"
    bucket = f"login:{client_ip}"
    try:
        attempts = int(await cache.store.get(bucket) or 0)
    except (TypeError, ValueError):
        attempts = 0
    if attempts >= MAX_ATTEMPTS:
        return render(
            request,
            "login.html",
            {
                "page_title": "ورود به پنل",
                "next": target,
                "error": "تعداد تلاش‌های ناموفق زیاد بود. چند دقیقه بعد دوباره امتحان کنید.",
            },
            status_code=429,
        )

    staff = await authenticate(session, login, password)
    if staff is None:
        with suppress(Exception):  # pragma: no cover
            await cache.store.incr(bucket, LOCKOUT_SECONDS)
        log.warning("Failed web login attempt for %r from %s", login, client_ip)
        return render(
            request,
            "login.html",
            {"page_title": "ورود به پنل", "next": target, "error": "نام کاربری یا رمز عبور نادرست است."},
            status_code=401,
        )

    with suppress(Exception):  # pragma: no cover
        await cache.store.delete(bucket)

    token, _info = issue_session(staff)
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=settings.panel_behind_proxy,
        path="/",
    )
    flash(response, f"خوش آمدید {staff.name or staff.login} 👋", "success")
    log.info("Panel login: %s", staff.login)
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    response = RedirectResponse(f"{settings.panel_prefix}/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/profile")
async def profile_page(
    request: Request,
    staff=Depends(current_staff),
    session: AsyncSession = Depends(get_db_session),
):
    """Self-service page so an operator can rotate their own password."""
    if staff is None:
        return RedirectResponse(f"{settings.panel_prefix}/login", status_code=303)

    from app.core.jalali import jalali_datetime

    return render(
        request,
        "profile.html",
        {
            "page_title": "حساب کاربری من",
            "page_subtitle": staff.login or staff.name,
            "staff_row": staff,
            "last_login": jalali_datetime(staff.last_login_at),
            "error": None,
        },
    )


@router.post("/profile/password")
async def change_own_password(
    request: Request,
    staff=Depends(current_staff),
    session: AsyncSession = Depends(get_db_session),
):
    from app.core.jalali import jalali_datetime
    from app.core.security import hash_password, password_strength_error, verify_password
    from app.web.security import verify_csrf
    from app.web.templating import redirect

    if staff is None:
        return RedirectResponse(f"{settings.panel_prefix}/login", status_code=303)

    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    current = form_str(form, "current_password")
    new = form_str(form, "new_password")
    confirm = form_str(form, "confirm_password")

    def fail(message: str):
        return render(
            request,
            "profile.html",
            {
                "page_title": "حساب کاربری من",
                "page_subtitle": staff.login or staff.name,
                "staff_row": staff,
                "last_login": jalali_datetime(staff.last_login_at),
                "error": message,
            },
            status_code=400,
        )

    if not verify_password(current, staff.password_hash):
        return fail("رمز عبور فعلی نادرست است.")
    if new != confirm:
        return fail("رمز عبور جدید و تکرار آن یکسان نیستند.")
    weak = password_strength_error(new)
    if weak:
        return fail(weak)

    staff.password_hash = hash_password(new)
    await session.flush()
    log.info("Password changed for %s", staff.login)
    return redirect(f"{settings.panel_prefix}/profile", message="رمز عبور با موفقیت تغییر کرد.")


__all__ = ["router"]
