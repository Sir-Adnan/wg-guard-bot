"""رسیدهای پرداخت — بررسی کارت‌به‌کارت با گردش کار چند‌مدیره.

هر رسید به همهٔ بررسی‌کننده‌ها فرستاده می‌شود و هر کس اول تصمیم بگیرد،
نسخهٔ بقیه هم داخل ربات ویرایش می‌شود.  پنل دقیقاً همان قاعده را دنبال
می‌کند:

1. تصمیم روی خودِ رسید ثبت می‌شود (:mod:`app.services.receipts`)،
2. کپی همهٔ بررسی‌کننده‌ها ویرایش می‌شود تا کسی دوباره تصمیم نگیرد،
3. به کاربر اطلاع داده می‌شود،
4. اگر رسید مربوط به سفارش پرداخت‌شده باشد، سرویس ساخته و تحویل می‌شود.

تماس با نود WG-Guard فقط در گام ۴ رخ می‌دهد و پیش از آن تراکنش بسته
می‌شود؛ پس یک نود کند فقط یک پیام خطای فارسی می‌سازد، نه تراکنش باز.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.core.money import fa_digits
from app.db.models import OrderStatus, Receipt, ReceiptStatus, Service, Staff
from app.services.delivery import delivery
from app.services.provisioning import provisioning
from app.services.receipts import PURPOSE_LABELS, receipt_service
from app.web.deps import Page, form_dict, form_str, pagination
from app.web.security import get_db_session, require_any, require_manager, verify_csrf
from app.web.templating import redirect, render

router = APIRouter(tags=["receipts"])

BASE = f"{settings.panel_prefix}/receipts"

#: زبانه‌های صفحه، به همان ترتیبی که نمایش داده می‌شوند.
STATUS_TABS: tuple[tuple[str, str], ...] = (
    ("pending", "در انتظار بررسی"),
    ("approved", "تأییدشده"),
    ("rejected", "ردشده"),
    ("expired", "منقضی"),
    ("all", "همه"),
)

#: رنگ نشان هر وضعیت (خالی یعنی نشانِ بی‌رنگ).
STATUS_TONES: dict[str, str] = {
    ReceiptStatus.PENDING.value: "warning",
    ReceiptStatus.APPROVED.value: "success",
    ReceiptStatus.REJECTED.value: "danger",
    ReceiptStatus.EXPIRED.value: "",
}


# ---------------------------------------------------------------------------
# کمک‌کننده‌ها
# ---------------------------------------------------------------------------
def _selected_status(raw: str) -> str:
    """زبانهٔ خواسته‌شده؛ مقدار نامعتبر یعنی «در انتظار بررسی»."""
    key = (raw or "").strip()
    return key if key in {value for value, _label in STATUS_TABS} else "pending"


def _filter_query(**values: Any) -> str:
    """رشتهٔ کوئری فیلترهای فعال، برای نگه داشتن آن‌ها در صفحه‌بندی."""
    parts = [f"{key}={value}" for key, value in values.items() if value not in (None, "")]
    return ("&" + "&".join(parts)) if parts else ""


async def _status_counts(session: AsyncSession) -> dict[str, int]:
    """شمارش رسیدها به تفکیک وضعیت، با یک کوئری و بدون N+1."""
    rows = await session.execute(select(Receipt.status, func.count(Receipt.id)).group_by(Receipt.status))
    counts = {status.value: 0 for status in ReceiptStatus}
    total = 0
    for status, count in rows.all():
        counts[status.value] = int(count)
        total += int(count)
    counts["all"] = total
    return counts


async def _reviewer_names(session: AsyncSession) -> dict[int, str]:
    """نام نمایشی کارکنان، برای ستون «بررسی‌کننده» و فهرست نسخه‌ها."""
    rows = (await session.execute(select(Staff).order_by(Staff.id.asc()))).scalars()
    return {
        int(member.id): (member.name or (f"@{member.login}" if member.login else f"#{member.id}")) for member in rows
    }


# ---------------------------------------------------------------------------
# فهرست رسیدها
# ---------------------------------------------------------------------------
@router.get("/receipts")
async def list_receipts(
    request: Request,
    status: str = Query("pending", description="زبانهٔ وضعیت رسید"),
    page: Page = Depends(pagination),
    staff: Staff = Depends(require_any()),
    session: AsyncSession = Depends(get_db_session),
):
    selected = _selected_status(status)
    wanted = None if selected == "all" else ReceiptStatus(selected)

    rows, total = await receipt_service.search(
        session,
        status=wanted,
        query=page.search,
        limit=page.size,
        offset=page.offset,
    )
    page.total = total
    counts = await _status_counts(session)

    tabs = [
        {"key": key, "label": label, "count": counts.get(key, 0), "active": key == selected}
        for key, label in STATUS_TABS
    ]

    return render(
        request,
        "receipts.html",
        {
            "page_title": "رسیدهای پرداخت",
            "page_subtitle": f"{fa_digits(counts.get('pending', 0))} رسید در انتظار بررسی",
            "rows": rows,
            "page": page,
            "tabs": tabs,
            "counts": counts,
            "selected_status": selected,
            "reviewers": await _reviewer_names(session),
            "purpose_labels": PURPOSE_LABELS,
            "status_label": receipt_service.status_label,
            "tones": STATUS_TONES,
            "base_url": BASE,
            "extra": _filter_query(status=selected),
        },
    )


# ---------------------------------------------------------------------------
# تأیید
# ---------------------------------------------------------------------------
@router.post("/receipts/{receipt_id}/approve")
async def approve_receipt(
    receipt_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        receipt = await receipt_service.get(session, receipt_id)
        outcome = await receipt_service.approve(session, receipt, staff)
        if not outcome.accepted:
            reviewer = outcome.already_reviewed_by
            message = outcome.message if not reviewer else f"{outcome.message} (تصمیم‌گیرنده: {reviewer})"
            return redirect(BASE, message=message, level="error")

        # کپی همهٔ بررسی‌کننده‌ها ویرایش می‌شود تا کسی دوباره تصمیم نگیرد.
        await receipt_service.refresh_copies(session, receipt, reviewer=staff, decision="approved")
        await receipt_service.notify_customer(session, receipt, approved=True)

        code = receipt.code
        order = receipt.order
        order_id = int(order.id) if order is not None else None
        order_code = order.order_code if order is not None else ""
        # پرداخت تأیید شده اما سرویس هنوز ساخته نشده است.
        needs_provisioning = order is not None and order.status == OrderStatus.PAID
        # تا اینجا هیچ تماسی با نود WG-Guard نداشته‌ایم؛ قبل از حرف زدن با
        # نود، تأیید رسید و جابه‌جایی پول را قطعی می‌کنیم.
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="error")

    if not needs_provisioning or order_id is None:
        return redirect(BASE, message=f"رسید {code} تأیید شد.")

    try:
        result = await provisioning.provision_order(order_id)
    except AppError as exc:
        return redirect(
            BASE,
            message=(
                f"رسید {code} تأیید شد ولی ساخت سرویس ناموفق بود: {exc.message} — از صفحهٔ سفارش‌ها دوباره تلاش کنید."
            ),
            level="error",
        )

    if result.failed or result.service_id is None:
        return redirect(
            BASE,
            message=(
                f"رسید {code} تأیید شد ولی ساخت سرویس ناموفق بود: {result.error or 'دلیل نامشخص'} — "
                "از صفحهٔ سفارش‌ها دوباره تلاش کنید."
            ),
            level="error",
        )

    service = await session.get(Service, result.service_id)
    if service is None:
        return redirect(
            BASE,
            message=f"رسید {code} تأیید شد و سفارش {order_code} ساخته شد، اما سرویس آن پیدا نشد.",
            level="error",
        )

    username = service.wg_username
    try:
        intro = await delivery.purchase_success_text(session, service, order_code)
        # ارسال کانفیگ ممکن است در صورت نبود کش، از نود بخواند؛ پس تراکنش
        # را همین‌جا می‌بندیم.
        await session.commit()
        delivered = await delivery.deliver_service(session, service, intro=intro)
    except AppError as exc:
        return redirect(
            BASE,
            message=f"سرویس {username} ساخته شد ولی ارسال آن ناموفق بود: {exc.message}",
            level="error",
        )

    if not delivered:
        return redirect(
            BASE,
            message=(
                f"سرویس {username} ساخته شد ولی ارسال به کاربر ناموفق بود؛ از صفحهٔ سرویس‌ها «ارسال دوباره» را بزنید."
            ),
            level="error",
        )

    await session.commit()
    return redirect(BASE, message=f"رسید {code} تأیید شد و سرویس {username} برای کاربر ساخته و ارسال شد.")


# ---------------------------------------------------------------------------
# رد
# ---------------------------------------------------------------------------
@router.post("/receipts/{receipt_id}/reject")
async def reject_receipt(
    receipt_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    # این متن عیناً برای کاربر ارسال می‌شود، پس خالی‌بودنش پذیرفتنی نیست.
    reason = form_str(form, "reason")
    if not reason:
        return redirect(
            BASE,
            message="دلیل رد رسید را بنویسید؛ همین متن برای کاربر ارسال می‌شود.",
            level="error",
        )

    try:
        receipt = await receipt_service.get(session, receipt_id)
        outcome = await receipt_service.reject(session, receipt, staff, reason=reason)
        if not outcome.accepted:
            reviewer = outcome.already_reviewed_by
            message = outcome.message if not reviewer else f"{outcome.message} (تصمیم‌گیرنده: {reviewer})"
            return redirect(BASE, message=message, level="error")

        await receipt_service.refresh_copies(session, receipt, reviewer=staff, decision="rejected")
        await receipt_service.notify_customer(session, receipt, approved=False)
        code = receipt.code
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="error")

    return redirect(BASE, message=f"رسید {code} رد شد و دلیلش برای کاربر ارسال شد.")


__all__ = ["router"]
