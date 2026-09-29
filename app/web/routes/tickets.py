"""تیکت‌های پشتیبانی — گفت‌وگوی دوطرفه‌ی مشتری و کارکنان (Ticket).

هر ردیف فهرست یک مودال دارد که کل گفت‌وگو و فرم پاسخ در آن است.  برای اینکه
باز کردن مودال درخواست تازه‌ای به سرور نزند، آخرین پیام‌های هر تیکتِ فهرست
همراه همان صفحه خوانده می‌شود (یک درخواست، همیشه درست).

پاسخ کارکنان دو کار می‌کند: ردیف :class:`~app.db.models.TicketMessage` را
می‌نویسد و همان متن را در تلگرام برای مشتری می‌فرستد؛ اگر ارسال نشد پیام
فقط در پنل ثبت می‌ماند و به اپراتور اطلاع داده می‌شود.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.money import fa_digits
from app.db.models import Staff, Ticket, TicketMessage, TicketStatus, User
from app.services.notifications import notifier
from app.services.tickets import tickets
from app.web.deps import Page, form_dict, form_str, pagination
from app.web.security import get_db_session, require_any, verify_csrf
from app.web.templating import redirect, render

router = APIRouter(tags=["tickets"])

log = get_logger(__name__)

BASE = f"{settings.panel_prefix}/tickets"

#: زبانه‌های فهرست: کلید، برچسب.
TABS: tuple[tuple[str, str], ...] = (
    ("open", "باز"),
    ("unread", "بدون پاسخ"),
    ("closed", "بسته"),
    ("all", "همه"),
)

TAB_KEYS: frozenset[str] = frozenset(key for key, _label in TABS)

#: برچسب و رنگ نشان وضعیت تیکت.
STATUS_LABELS: dict[str, str] = {
    TicketStatus.OPEN.value: "باز",
    TicketStatus.ANSWERED.value: "پاسخ داده‌شده",
    TicketStatus.PENDING.value: "در انتظار مشتری",
    TicketStatus.CLOSED.value: "بسته",
}

STATUS_TONES: dict[str, str] = {
    TicketStatus.OPEN.value: "warning",
    TicketStatus.ANSWERED.value: "success",
    TicketStatus.PENDING.value: "info",
    TicketStatus.CLOSED.value: "info",
}

#: تعداد پیام‌هایی که داخل مودال هر تیکت نشان داده می‌شود.
THREAD_PREVIEW = 10


# ---------------------------------------------------------------------------
# کمک‌کننده‌ها
# ---------------------------------------------------------------------------
def _tab_filters(tab: str) -> tuple[TicketStatus | None, bool]:
    """زبانه را به پارامترهای :meth:`TicketService.search` تبدیل می‌کند.

    «باز» همان وضعیت ``OPEN`` است، یعنی تیکتی که آخرین پیامش از مشتری است و
    منتظر پاسخ مانده؛ «بدون پاسخ» به‌جای وضعیت، با شمارنده‌ی خوانده‌نشده‌های
    پشتیبانی فیلتر می‌کند (سرویس فقط برابری وضعیت را پشتیبانی می‌کند).
    """
    if tab == "unread":
        return None, True
    if tab == "closed":
        return TicketStatus.CLOSED, False
    if tab == "all":
        return None, False
    return TicketStatus.OPEN, False


async def _tab_counts(session: AsyncSession) -> dict[str, int]:
    """شمارش هر زبانه برای نمایش کنار برچسبش."""
    counts: dict[str, int] = {}
    for key, _label in TABS:
        status, only_unread = _tab_filters(key)
        _rows, total = await tickets.search(session, status=status, only_unread=only_unread, limit=1, offset=0)
        counts[key] = total
    return counts


async def _latest_messages(
    session: AsyncSession, ticket_id: int, *, limit: int = THREAD_PREVIEW
) -> list[TicketMessage]:
    """آخرین پیام‌های یک تیکت، از قدیم به جدید (ترتیب خواندن گفت‌وگو).

    ``tickets.messages`` پیام‌ها را از قدیم به جدید و با ``limit`` می‌برد؛
    مودال به *تازه‌ترین* پیام‌ها نیاز دارد، پس وارونه می‌خوانیم و در پایان
    ترتیب را برمی‌گردانیم.
    """
    rows = (
        await session.execute(
            select(TicketMessage)
            .where(TicketMessage.ticket_id == ticket_id)
            .order_by(TicketMessage.created_at.desc(), TicketMessage.id.desc())
            .limit(limit)
        )
    ).scalars()
    return list(reversed(list(rows)))


async def _thread_totals(session: AsyncSession, ticket_ids: list[int]) -> dict[int, int]:
    """تعداد کل پیام‌های هر تیکت (برای هشدار «فقط بخشی نشان داده می‌شود»)."""
    if not ticket_ids:
        return {}
    rows = await session.execute(
        select(TicketMessage.ticket_id, func.count(TicketMessage.id))
        .where(TicketMessage.ticket_id.in_(ticket_ids))
        .group_by(TicketMessage.ticket_id)
    )
    return {ticket_id: int(count) for ticket_id, count in rows.all()}


def _matches(ticket: Ticket, needle: str) -> bool:
    """جست‌وجوی ساده در کد، موضوع و مشخصات مشتری (روی همان صفحه)."""
    user = ticket.user
    haystack = " ".join(
        filter(
            None,
            (
                ticket.ticket_code,
                ticket.subject,
                user.display_name if user else "",
                str(user.telegram_id) if user else "",
                (user.username or "") if user else "",
            ),
        )
    )
    return needle in haystack.casefold()


def _back(tab: str) -> str:
    """آدرس بازگشت با حفظ زبانه‌ی فعلی."""
    return f"{BASE}?tab={tab}" if tab in TAB_KEYS else BASE


async def _deliver_reply(user: User | None, text: str) -> bool:
    """پاسخ را در تلگرام برای مشتری می‌فرستد؛ ``False`` یعنی نرسید."""
    if user is None:
        return False
    if not notifier.bound:
        # پنل بدون رباتِ روشن (مثلاً در حال تعمیر) هم باید پاسخ را ثبت کند.
        log.info("Ticket reply kept in panel: notifier is not bound to a bot")
        return False
    try:
        return await notifier.to_user(user, f"<b>پاسخ پشتیبانی</b>\n\n{text}") is not None
    except Exception as exc:  # pragma: no cover - شبکه/توکن؛ پاسخ در پنل ثبت شده است
        log.warning("Could not deliver ticket reply to %s: %s", user.telegram_id, exc)
        return False


# ---------------------------------------------------------------------------
# نمایش
# ---------------------------------------------------------------------------
@router.get("/tickets")
async def list_tickets(
    request: Request,
    tab: str = Query("open", description="زبانه فهرست"),
    page: Page = Depends(pagination),
    staff: Staff = Depends(require_any()),
    session: AsyncSession = Depends(get_db_session),
):
    if tab not in TAB_KEYS:
        tab = "open"
    status, only_unread = _tab_filters(tab)

    try:
        rows, total = await tickets.search(
            session, status=status, only_unread=only_unread, limit=page.size, offset=page.offset
        )
        counts = await _tab_counts(session)
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="error")

    page.total = total

    needle = page.search.casefold()
    matched = [row for row in rows if _matches(row, needle)] if needle else rows

    threads: dict[int, list[TicketMessage]] = {}
    for ticket in matched:
        threads[ticket.id] = await _latest_messages(session, ticket.id)
    thread_totals = await _thread_totals(session, [ticket.id for ticket in matched])

    subtitle = f"{fa_digits(total)} تیکت در این زبانه"
    if needle:
        subtitle += f" · {fa_digits(len(matched))} مورد در این صفحه"

    return render(
        request,
        "tickets.html",
        {
            "page_title": "تیکت‌های پشتیبانی",
            "page_subtitle": subtitle,
            "rows": matched,
            "threads": threads,
            "thread_totals": thread_totals,
            "thread_preview": THREAD_PREVIEW,
            "tabs": TABS,
            "tab": tab,
            "counts": counts,
            "q": page.search,
            "status_labels": STATUS_LABELS,
            "status_tones": STATUS_TONES,
            "page": page,
            "base_url": BASE,
        },
    )


# ---------------------------------------------------------------------------
# پاسخ و تغییر وضعیت
# ---------------------------------------------------------------------------
@router.post("/tickets/{ticket_id}/reply")
async def reply_to_ticket(
    ticket_id: int,
    request: Request,
    staff: Staff = Depends(require_any()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))
    tab = form_str(form, "tab", "open")

    text = form_str(form, "text")
    if not text:
        return redirect(_back(tab), message="متن پاسخ نمی‌تواند خالی باشد.", level="error")

    try:
        ticket = await tickets.get(session, ticket_id)
        code = ticket.ticket_code
        user = ticket.user
        await tickets.add_staff_message(session, ticket, staff, text=text)
        await session.commit()
    except AppError as exc:
        return redirect(_back(tab), message=exc.message, level="error")

    if await _deliver_reply(user, text):
        return redirect(_back(tab), message=f"پاسخ تیکت «{code}» ثبت و برای مشتری ارسال شد.")

    return redirect(
        _back(tab),
        message=(
            f"پاسخ تیکت «{code}» ثبت شد، اما ارسال آن در تلگرام ناموفق بود "
            "(ربات در دسترس نیست یا مشتری ربات را بلاک کرده است)."
        ),
        level="error",
    )


@router.post("/tickets/{ticket_id}/close")
async def close_ticket(
    ticket_id: int,
    request: Request,
    staff: Staff = Depends(require_any()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))
    tab = form_str(form, "tab", "open")

    try:
        ticket = await tickets.get(session, ticket_id)
        code = ticket.ticket_code
        await tickets.close(session, ticket, staff=staff)
        await session.commit()
    except AppError as exc:
        return redirect(_back(tab), message=exc.message, level="error")

    return redirect(_back(tab), message=f"تیکت «{code}» بسته شد.")


@router.post("/tickets/{ticket_id}/reopen")
async def reopen_ticket(
    ticket_id: int,
    request: Request,
    staff: Staff = Depends(require_any()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))
    tab = form_str(form, "tab", "open")

    try:
        ticket = await tickets.get(session, ticket_id)
        code = ticket.ticket_code
        await tickets.reopen(session, ticket)
        await session.commit()
    except AppError as exc:
        return redirect(_back(tab), message=exc.message, level="error")

    return redirect(_back(tab), message=f"تیکت «{code}» دوباره باز شد.")


__all__ = ["router"]
