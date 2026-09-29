"""رویدادهای سیستم — رویدادهای ثبت‌شده و ردپای عملیات کارکنان.

دو زبانه دارد:

* **system** — جدول ``system_events``: هر خطایی که ربات یا پنل ثبت کرده است
  (منبع، سطح، پیام و ``meta``).  همین رویدادها در لاگ کانتینر هم نوشته
  می‌شوند، پس برای دیدن جریان زنده نیازی به این صفحه نیست.
* **audit** — جدول ``audit_logs``: چه کسی، چه زمانی، از کدام IP چه چیزی را
  تغییر داده است.

پاک‌سازی رویدادهای قدیمی و پشتیبان‌گیری از پایگاه داده فقط در اختیار مالک
است؛ هر دو POST در پایان با PRG به همین صفحه برمی‌گردند.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import Select, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.jalali import now_utc
from app.core.logging import get_logger
from app.core.money import fa_digits
from app.db.models import AuditLog, EventLevel, Staff, SystemEvent
from app.web.deps import Page, form_dict, pagination
from app.web.security import get_db_session, require_manager, require_owner, verify_csrf
from app.web.templating import redirect, render
from app.workers.jobs import jobs

router = APIRouter(tags=["logs"])

log = get_logger(__name__)

BASE = f"{settings.panel_prefix}/logs"

#: زبانه‌های صفحه: کلید، برچسب.
TABS: tuple[tuple[str, str], ...] = (
    ("system", "رویدادهای سیستم"),
    ("audit", "ردپای عملیات"),
)

TAB_KEYS: frozenset[str] = frozenset(key for key, _label in TABS)

#: برچسب فارسی سطح رویداد.
LEVEL_LABELS: dict[str, str] = {
    EventLevel.INFO.value: "اطلاع",
    EventLevel.WARNING.value: "هشدار",
    EventLevel.ERROR.value: "خطا",
    EventLevel.CRITICAL.value: "بحرانی",
}

#: رنگ نشان هر سطح (``panel.css`` فقط این رنگ‌ها را دارد).
LEVEL_TONES: dict[str, str] = {
    EventLevel.INFO.value: "info",
    EventLevel.WARNING.value: "warning",
    EventLevel.ERROR.value: "danger",
    EventLevel.CRITICAL.value: "danger",
}

#: رویدادها هم‌زمان در لاگ کانتینر هم نوشته می‌شوند.
LOG_CHANNEL_CMD = "docker compose logs -f bot"

#: عمر نگه‌داری رویدادها پیش از پاک‌سازی دستی.
PURGE_DAYS = 30


# ---------------------------------------------------------------------------
# کمک‌کننده‌ها
# ---------------------------------------------------------------------------
def _count(stmt: Select) -> Select:
    """``SELECT count(*)`` روی همان شرط‌های فهرست."""
    return select(func.count()).select_from(stmt.subquery())


def _pretty(meta: dict[str, Any] | None) -> str:
    """``meta`` را برای نمایش داخل ``<pre>`` مرتب می‌کند."""
    if not meta:
        return ""
    try:
        return json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - مقدارهای ناسازگار
        return str(meta)


# ---------------------------------------------------------------------------
# نمایش
# ---------------------------------------------------------------------------
@router.get("/logs")
async def list_logs(
    request: Request,
    tab: str = Query("system", description="زبانه صفحه"),
    level: str = Query("", description="سطح رویداد"),
    source: str = Query("", description="منبع رویداد"),
    action: str = Query("", description="نوع عملیات"),
    staff_id: int = Query(0, ge=0, description="کارمند انجام‌دهنده"),
    page: Page = Depends(pagination),
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    if tab not in TAB_KEYS:
        tab = "system"
    if level not in LEVEL_LABELS:
        level = ""

    sources: list[str] = []
    actions: list[str] = []
    staff_options: list[Staff] = []
    meta_json: dict[int, str] = {}
    rows: list[Any] = []

    if tab == "system":
        sources = list(
            (await session.execute(select(SystemEvent.source).distinct().order_by(SystemEvent.source.asc()))).scalars()
        )
        stmt = select(SystemEvent)
        if level:
            stmt = stmt.where(SystemEvent.level == EventLevel(level))
        if source:
            stmt = stmt.where(SystemEvent.source == source)

        page.total = int(await session.scalar(_count(stmt)) or 0)
        rows = list(
            (
                await session.execute(
                    stmt.order_by(SystemEvent.created_at.desc(), SystemEvent.id.desc())
                    .limit(page.size)
                    .offset(page.offset)
                )
            ).scalars()
        )
        meta_json = {row.id: _pretty(row.meta) for row in rows}
        extra = "&" + urlencode({"tab": tab, "level": level, "source": source})
        subtitle = f"{fa_digits(page.total)} رویداد ثبت‌شده"
    else:
        actions = list(
            (await session.execute(select(AuditLog.action).distinct().order_by(AuditLog.action.asc()))).scalars()
        )
        staff_options = list(
            (
                await session.execute(
                    select(Staff)
                    .where(Staff.id.in_(select(AuditLog.staff_id).where(AuditLog.staff_id.is_not(None)).distinct()))
                    .order_by(Staff.name.asc())
                )
            ).scalars()
        )
        stmt = select(AuditLog)
        if action:
            stmt = stmt.where(AuditLog.action == action)
        if staff_id:
            stmt = stmt.where(AuditLog.staff_id == staff_id)

        page.total = int(await session.scalar(_count(stmt)) or 0)
        rows = list(
            (
                await session.execute(
                    stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(page.size).offset(page.offset)
                )
            ).scalars()
        )
        meta_json = {row.id: _pretty(row.meta) for row in rows}
        extra = "&" + urlencode({"tab": tab, "action": action, "staff_id": staff_id or ""})
        subtitle = f"{fa_digits(page.total)} رکورد در ردپای عملیات"

    return render(
        request,
        "logs.html",
        {
            "page_title": "رویدادهای سیستم",
            "page_subtitle": subtitle,
            "rows": rows,
            "tabs": TABS,
            "tab": tab,
            "level": level,
            "source": source,
            "action": action,
            "staff_id": staff_id,
            "sources": sources,
            "actions": actions,
            "staff_options": staff_options,
            "level_labels": LEVEL_LABELS,
            "level_tones": LEVEL_TONES,
            "meta_json": meta_json,
            "log_channel_cmd": LOG_CHANNEL_CMD,
            "purge_days": PURGE_DAYS,
            "page": page,
            "base_url": BASE,
            "extra": extra,
        },
    )


# ---------------------------------------------------------------------------
# نگه‌داری (فقط مالک)
# ---------------------------------------------------------------------------
@router.post("/logs/purge")
async def purge_events(
    request: Request,
    staff: Staff = Depends(require_owner()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    cutoff = now_utc() - timedelta(days=PURGE_DAYS)
    result = await session.execute(delete(SystemEvent).where(SystemEvent.created_at < cutoff))
    deleted = int(result.rowcount or 0)
    await session.commit()

    return redirect(
        BASE,
        message=f"{fa_digits(deleted)} رویداد قدیمی‌تر از {fa_digits(PURGE_DAYS)} روز پاک شد.",
    )


@router.post("/logs/backup")
async def backup_database(
    request: Request,
    staff: Staff = Depends(require_owner()),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        path = await jobs.backup_database()
    except Exception as exc:  # pragma: no cover - باینری/شبکه؛ پیام باید به اپراتور برسد
        log.error("Database backup failed: %s", exc)
        return redirect(BASE, message="پشتیبان‌گیری با خطا مواجه شد؛ لاگ کانتینر را ببینید.", level="error")

    if not path:
        return redirect(
            BASE,
            message=("پشتیبان‌گیری انجام نشد: ابزار pg_dump در دسترس نیست یا پشتیبان‌گیری در تنظیمات غیرفعال است."),
            level="error",
        )

    return redirect(BASE, message=f"نسخه پشتیبان «{Path(path).name}» ساخته شد.")


__all__ = ["router"]
