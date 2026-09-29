"""سرویس‌ها — فهرست و عملیات روی سرویس‌های ساخته‌شده (Service).

هر عملیاتی که با نود WG-Guard حرف می‌زند دو قاعده را رعایت می‌کند:

1. **قبل** از تماس با نود، تراکنش دیتابیس بسته می‌شود (``await session.commit()``)
   تا یک نود کند، تراکنش پنل را باز نگه ندارد،
2. خطای نود (:class:`~app.core.errors.AppError`) به یک پیام فارسی تبدیل
   می‌شود، نه خطای ۵۰۰.

حذف هم هرگز ردیف را پاک نمی‌کند؛ فقط وضعیت را ``DELETED`` می‌کند چون همین
ردیف‌ها تاریخ فروش و مبنای گزارش‌ها هستند.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.core.jalali import jalali_date
from app.core.money import fa_digits
from app.db.models import Panel, Service, ServiceStatus, Staff, User
from app.panels.client import WGGuardClient
from app.panels.manager import panel_manager
from app.services.catalog import catalog
from app.services.delivery import delivery
from app.services.provisioning import provisioning
from app.web.deps import Page, form_dict, form_int, pagination
from app.web.security import get_db_session, require_any, require_manager, verify_csrf
from app.web.templating import redirect, render

router = APIRouter(tags=["services"])

BASE = f"{settings.panel_prefix}/services"

#: حد بالای تمدید دستی (۱۰ سال) تا اشتباه تایپی یک سرویس را ابدی نکند.
MAX_EXTEND_DAYS = 3650
DEFAULT_EXTEND_DAYS = 30

#: برچسب و رنگ نشان هر وضعیت سرویس.
STATUS_LABELS: dict[str, str] = {
    ServiceStatus.ACTIVE.value: "فعال",
    ServiceStatus.DISABLED.value: "غیرفعال",
    ServiceStatus.EXPIRED.value: "منقضی‌شده",
    ServiceStatus.TRAFFIC_EXCEEDED.value: "اتمام حجم",
    ServiceStatus.DELETED.value: "حذف‌شده",
}

STATUS_TONES: dict[str, str] = {
    ServiceStatus.ACTIVE.value: "success",
    ServiceStatus.DISABLED.value: "",
    ServiceStatus.EXPIRED.value: "warning",
    ServiceStatus.TRAFFIC_EXCEEDED.value: "danger",
    ServiceStatus.DELETED.value: "",
}


# ---------------------------------------------------------------------------
# کمک‌کننده‌ها
# ---------------------------------------------------------------------------
def _status_value(raw: str) -> ServiceStatus | None:
    """رشتهٔ فیلتر وضعیت را به enum تبدیل می‌کند؛ مقدار نامعتبر یعنی «همه»."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return ServiceStatus(text)
    except ValueError:
        return None


def _optional_id(raw: str) -> int | None:
    """فیلتر خالی یعنی «همهٔ پنل‌ها»."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return value or None


def _filter_query(**values: Any) -> str:
    """رشتهٔ کوئری فیلترهای فعال، برای نگه داشتن آن‌ها در صفحه‌بندی."""
    parts = [f"{key}={value}" for key, value in values.items() if value not in (None, "")]
    return ("&" + "&".join(parts)) if parts else ""


def _label(status: ServiceStatus) -> str:
    return STATUS_LABELS.get(status.value, status.value)


async def _stats(session: AsyncSession) -> dict[str, int]:
    """شمارش سرویس‌ها به تفکیک وضعیت، با یک کوئری گروهی."""
    rows = await session.execute(select(Service.status, func.count(Service.id)).group_by(Service.status))
    counts = {status.value: 0 for status in ServiceStatus}
    for status, count in rows.all():
        counts[status.value] = int(count)
    counts["test"] = int(
        await session.scalar(
            select(func.count(Service.id)).where(Service.is_test.is_(True), Service.status != ServiceStatus.DELETED)
        )
        or 0
    )
    return counts


async def _service_for(session: AsyncSession, service_id: int) -> tuple[Service, Panel, WGGuardClient]:
    """سرویس، پنل و کلاینتش؛ نبود سرویس یا پنل به خطای فارسی ترجمه می‌شود."""
    return await panel_manager.find_service(session, service_id)


# ---------------------------------------------------------------------------
# فهرست
# ---------------------------------------------------------------------------
@router.get("/services")
async def list_services(
    request: Request,
    q: str = Query("", description="جست‌وجو در نام کاربری یا شناسهٔ کاربر پنل"),
    status: str = Query("", description="فیلتر وضعیت سرویس"),
    panel_id: str = Query("", description="فیلتر پنل"),
    page: Page = Depends(pagination),
    staff: Staff = Depends(require_any()),
    session: AsyncSession = Depends(get_db_session),
):
    selected_status = _status_value(status)
    selected_panel = _optional_id(panel_id)
    # ``q`` هم این‌جا و هم در ``pagination`` خوانده می‌شود و هر دو یک مقدار دارند.
    term = (q or page.search or "").strip()

    # کاربر برای نمایش نامش join می‌شود؛ خودِ سرویس پنل/پلن/دستگاه‌ها را
    # به‌صورت eager همراه می‌آورد، پس selectinload اضافه‌ای لازم نیست.
    stmt = select(Service).join(User, Service.user_id == User.id)
    if selected_status is not None:
        stmt = stmt.where(Service.status == selected_status)
    if selected_panel is not None:
        stmt = stmt.where(Service.panel_id == selected_panel)
    if term:
        needle = f"%{term}%"
        stmt = stmt.where(or_(Service.wg_username.ilike(needle), Service.wg_user_id.ilike(needle)))

    total = int(await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0)
    rows = list(
        (await session.execute(stmt.order_by(Service.created_at.desc()).limit(page.size).offset(page.offset))).scalars()
    )
    page.total = total

    return render(
        request,
        "services.html",
        {
            "page_title": "سرویس‌ها",
            "page_subtitle": f"{fa_digits(total)} سرویس با فیلترهای فعلی",
            "rows": rows,
            "page": page,
            "stats": await _stats(session),
            "panels": await catalog.panels_for_select(session),
            "status_labels": STATUS_LABELS,
            "tones": STATUS_TONES,
            "status_options": [(status.value, STATUS_LABELS[status.value]) for status in ServiceStatus],
            "selected_status": selected_status.value if selected_status else "",
            "selected_panel": selected_panel,
            "default_extend_days": DEFAULT_EXTEND_DAYS,
            "max_extend_days": MAX_EXTEND_DAYS,
            "base_url": BASE,
            "extra": _filter_query(
                status=selected_status.value if selected_status else "",
                panel_id=selected_panel,
            ),
        },
    )


# ---------------------------------------------------------------------------
# همگام‌سازی و ارسال دوباره (پشتیبان هم اجازه دارد)
# ---------------------------------------------------------------------------
@router.post("/services/{service_id}/sync")
async def sync_service(
    service_id: int,
    request: Request,
    staff: Staff = Depends(require_any()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        service, _panel, _client = await _service_for(session, service_id)
        username = service.wg_username
        before = service.last_synced_at
        # قاعدهٔ اصلی: پیش از هر تماس با نود، تراکنش بسته می‌شود.
        await session.commit()
        await provisioning.sync_service(session, service)
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="error")

    if service.last_synced_at == before:
        # ``sync_service`` خطای نود را می‌بلعد؛ فقط نودِ بی‌جواب این‌جا می‌ماند.
        return redirect(
            BASE,
            message=f"ارتباط با نود برقرار نشد؛ وضعیت سرویس {username} به‌روز نشد.",
            level="error",
        )

    await session.commit()
    return redirect(BASE, message=f"سرویس {username} همگام شد؛ وضعیت تازه: {_label(service.status)}.")


@router.post("/services/{service_id}/resend")
async def resend_service(
    service_id: int,
    request: Request,
    staff: Staff = Depends(require_any()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        service, _panel, _client = await _service_for(session, service_id)
        username = service.wg_username
        await session.commit()
        delivered = await delivery.deliver_service(session, service)
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="error")

    if not delivered:
        return redirect(
            BASE,
            message=(f"ارسال دوبارهٔ سرویس {username} ناموفق بود؛ شاید کاربر ربات را بلاک کرده است."),
            level="error",
        )
    return redirect(BASE, message=f"کانفیگ، کیو‌آر و لینک اشتراک سرویس {username} دوباره ارسال شد.")


# ---------------------------------------------------------------------------
# تغییر وضعیت، مصرف و تمدید
# ---------------------------------------------------------------------------
@router.post("/services/{service_id}/toggle")
async def toggle_service(
    service_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        service, _panel, client = await _service_for(session, service_id)
        if service.status == ServiceStatus.DELETED:
            return redirect(
                BASE,
                message=f"سرویس {service.wg_username} حذف شده است و وضعیتش تغییر نمی‌کند.",
                level="error",
            )

        username = service.wg_username
        wg_user_id = service.wg_user_id
        # سرویسِ خاموش یا منقضی «فعال» می‌شود، بقیه «غیرفعال».
        enabling = service.status in (
            ServiceStatus.DISABLED,
            ServiceStatus.EXPIRED,
            ServiceStatus.TRAFFIC_EXCEEDED,
        )
        await session.commit()

        if enabling:
            remote = await client.enable_user(wg_user_id)
        else:
            remote = await client.disable_user(wg_user_id)
        provisioning.apply_remote_state(service, remote)
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="error")

    if enabling and service.status != ServiceStatus.ACTIVE:
        return redirect(
            BASE,
            message=(
                f"نود سرویس {username} را فعال نکرد؛ وضعیت کنونی: {_label(service.status)}. "
                "برای بررسی، «همگام‌سازی» را بزنید."
            ),
            level="error",
        )

    state = "فعال" if enabling else "غیرفعال"
    return redirect(BASE, message=f"سرویس {username} {state} شد (وضعیت نود: {_label(service.status)}).")


@router.post("/services/{service_id}/reset-traffic")
async def reset_traffic(
    service_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        service, _panel, client = await _service_for(session, service_id)
        username = service.wg_username
        wg_user_id = service.wg_user_id
        await session.commit()

        remote = await client.reset_traffic(wg_user_id)
        provisioning.apply_remote_state(service, remote)
        # هشدارهای حجم پاک می‌شوند تا در دور تازه دوباره به کاربر اطلاع داده شود.
        service.notified_80pct = False
        service.notified_100pct = False
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="error")

    return redirect(BASE, message=f"مصرف سرویس {username} صفر شد؛ وضعیت تازه: {_label(service.status)}.")


@router.post("/services/{service_id}/extend")
async def extend_service(
    service_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    days = form_int(form, "days", DEFAULT_EXTEND_DAYS)
    if days <= 0 or days > MAX_EXTEND_DAYS:
        return redirect(
            BASE,
            message=f"مدت تمدید باید بین ۱ و {fa_digits(MAX_EXTEND_DAYS)} روز باشد.",
            level="error",
        )

    try:
        service, _panel, provider = await _service_for(session, service_id)
        username = service.wg_username
        wg_user_id = service.wg_user_id
        await session.commit()

        remote = await provider.renew_user(wg_user_id, duration_seconds=days * 86400)
        provisioning.apply_remote_state(service, remote)
        # یادآورهای دورهٔ قبل پاک می‌شوند تا برای انقضای تازه هم هشدار برسد.
        provisioning.reset_notifications(service)
        await provisioning.sync_service(session, service)
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="error")

    expires = jalali_date(service.expires_at) if service.expires_at else "—"
    return redirect(
        BASE,
        message=f"سرویس {username} به مدت {fa_digits(days)} روز تمدید شد؛ انقضای تازه: {expires}.",
    )


# ---------------------------------------------------------------------------
# بازنشانی کلیدها و حذف (حذف = غیرفعال‌سازی، نه پاک‌کردن ردیف)
# ---------------------------------------------------------------------------
@router.post("/services/{service_id}/rotate")
async def rotate_service(
    service_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        service, _panel, _client = await _service_for(session, service_id)
        username = service.wg_username
        await session.commit()

        _config, link = await provisioning.rotate_access(session, service)
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="error")

    if not link:
        return redirect(
            BASE,
            message=(f"کلیدهای سرویس {username} بازنشانی شد ولی لینک اشتراک تازه‌ای برنگشت؛ «همگام‌سازی» را بزنید."),
            level="error",
        )
    return redirect(
        BASE,
        message=f"کلیدها و لینک اشتراک سرویس {username} بازنشانی شد؛ کانفیگ تازه را برای کاربر بفرستید.",
    )


@router.post("/services/{service_id}/delete")
async def delete_service(
    service_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    service = await session.get(Service, service_id)
    if service is None:
        return redirect(BASE, message="سرویس مورد نظر پیدا نشد.", level="error")
    if service.status == ServiceStatus.DELETED:
        return redirect(BASE, message=f"سرویس {service.wg_username} از قبل حذف شده است.", level="error")

    username = service.wg_username
    # ردیف پاک نمی‌شود: همین سطر تاریخ فروش و مبنای گزارش‌ها است.
    service.status = ServiceStatus.DELETED
    await session.commit()
    return redirect(
        BASE,
        message=f"سرویس {username} حذف شد؛ ردیف آن به‌عنوان تاریخ فروش در گزارش‌ها می‌ماند.",
    )


__all__ = ["router"]
