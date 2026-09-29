"""سفارش‌ها — پیگیری چرخه‌ی خرید، تأیید پرداخت، ساخت و بازگشت وجه (Order).

مسیرهای این صفحه فقط لایه‌ی نازکی روی :mod:`app.services.orders` هستند؛ هر
گذار وضعیت در سرویس انجام می‌شود و بعد از ``commit`` نوبت به
:mod:`app.services.provisioning` می‌رسد که در نشستِ جداگانه‌ی خودش با نود
WG-Guard حرف می‌زند.  به همین دلیل خطای شبکه هرگز تراکنش پنل را باز نگه
نمی‌دارد و هر شکست فقط یک ``failure_reason`` روی همان سفارش می‌گذارد.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.core.jalali import jalali_date
from app.core.money import fa_digits, to_toman
from app.db.models import Order, OrderStatus, Panel, PaymentMethod, Plan, Staff, User
from app.services.orders import order_service
from app.services.provisioning import provisioning
from app.services.reports import KIND_LABELS, METHOD_LABELS, STATUS_LABELS, reports
from app.web.deps import Page, form_dict, form_str, pagination
from app.web.security import get_db_session, require_any, require_manager, verify_csrf
from app.web.templating import redirect, render

router = APIRouter(tags=["orders"])

BASE = f"{settings.panel_prefix}/orders"

#: حداکثر تعداد ردیف در خروجی CSV تا حافظه‌ی سرور بی‌دلیل پر نشود.
EXPORT_LIMIT = 5000

#: فیلترهای پولی/نوعی که به ``cancel`` و ``refund`` پاس داده می‌شوند.
REFUNDABLE_STATUSES: tuple[OrderStatus, ...] = (
    OrderStatus.PAID,
    OrderStatus.PROVISIONING,
    OrderStatus.COMPLETED,
)


# ---------------------------------------------------------------------------
# کمک‌کننده‌ها
# ---------------------------------------------------------------------------
def _status_value(raw: str) -> OrderStatus | None:
    """رشته‌ی فیلتر وضعیت را به enum تبدیل می‌کند؛ مقدار نامعتبر یعنی «همه»."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return OrderStatus(text)
    except ValueError:
        return None


def _optional_id(raw: str) -> int | None:
    """فیلتر خالی یعنی «همه‌ی پنل‌ها/پلن‌ها»."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return value or None


def _filter_query(**values: Any) -> str:
    """رشته‌ی کوئری فیلترهای فعال، برای نگه داشتن آن‌ها در صفحه‌بندی."""
    parts = [f"{key}={value}" for key, value in values.items() if value not in (None, "")]
    return ("&" + "&".join(parts)) if parts else ""


def _csv_response(content: str, filename: str):
    """CSV با BOM تا اکسل فارسی آن را درست باز کند."""
    from fastapi.responses import Response

    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def users_csv(rows: list[User], counts: dict[int, int], *, filename: str = "users.csv"):
    """خروجی اکسل از فهرست کاربران (مشترک بین صفحه‌ی کاربران و گزارش‌ها)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["نام", "نام کاربری", "شناسه", "موجودی (تومان)", "تعداد سفارش", "تاریخ عضویت"])
    for user in rows:
        writer.writerow(
            [
                user.display_name,
                f"@{user.username}" if user.username else "—",
                user.telegram_id,
                to_toman(int(user.balance_rial or 0)),
                counts.get(int(user.id), 0),
                jalali_date(user.created_at) if user.created_at else "—",
            ]
        )
    return _csv_response("\ufeff" + buffer.getvalue(), filename)


async def _order_counts(session: AsyncSession, user_ids: list[int]) -> dict[int, int]:
    """تعداد سفارش هر کاربر با یک کوئری گروهی (بدون N+1)."""
    if not user_ids:
        return {}
    rows = await session.execute(
        select(Order.user_id, func.count(Order.id)).where(Order.user_id.in_(user_ids)).group_by(Order.user_id)
    )
    return {int(user_id): int(count) for user_id, count in rows.all()}


def orders_csv(rows: list[Order], panels: dict[int, str], *, filename: str = "orders.csv"):
    """خروجی اکسل از سفارش‌های فهرست‌شده."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "کد سفارش",
            "کاربر",
            "پلن",
            "نوع",
            "وضعیت",
            "روش پرداخت",
            "مبلغ (تومان)",
            "تخفیف (تومان)",
            "پرداختی (تومان)",
            "پنل",
            "شناسه کاربر پنل",
            "تاریخ ثبت",
            "تاریخ تکمیل",
        ]
    )
    for order in rows:
        writer.writerow(
            [
                order.order_code,
                order.user.display_name if order.user else "—",
                order.plan_name,
                reports.kind_label(order.kind),
                reports.status_label(order.status),
                reports.method_label(order.payment_method),
                to_toman(int(order.amount_rial or 0)),
                to_toman(int(order.discount_rial or 0)),
                to_toman(int(order.payable_rial or 0)),
                panels.get(int(order.panel_id or 0), "—"),
                order.wg_user_id or "—",
                jalali_date(order.created_at) if order.created_at else "—",
                jalali_date(order.completed_at) if order.completed_at else "—",
            ]
        )
    return _csv_response("\ufeff" + buffer.getvalue(), filename)


async def _filters(session: AsyncSession) -> tuple[list[Plan], list[Panel]]:
    """گزینه‌های کشویی پنل و پلن برای نوار فیلتر."""
    plans = list(
        (
            await session.execute(select(Plan).order_by(Plan.is_active.desc(), Plan.sort_order.asc(), Plan.id.asc()))
        ).scalars()
    )
    panels = list((await session.execute(select(Panel).order_by(Panel.sort_order.asc(), Panel.id.asc()))).scalars())
    return plans, panels


async def _order_for(session: AsyncSession, order_id: int) -> Order:
    return await order_service.get(session, order_id)


# ---------------------------------------------------------------------------
# فهرست
# ---------------------------------------------------------------------------
@router.get("/orders")
async def list_orders(
    request: Request,
    q: str = Query("", description="جست‌وجو در کد سفارش، نام پلن یا شناسه‌ی کاربر پنل"),
    status: str = Query("", description="فیلتر وضعیت سفارش"),
    panel_id: str = Query("", description="فیلتر پنل"),
    plan_id: str = Query("", description="فیلتر پلن"),
    page: Page = Depends(pagination),
    staff: Staff = Depends(require_any()),
    session: AsyncSession = Depends(get_db_session),
):
    selected_status = _status_value(status)
    selected_panel = _optional_id(panel_id)
    selected_plan = _optional_id(plan_id)
    # ``pagination`` فقط ``q`` را می‌خواند؛ باقی فیلترها جدا می‌آیند.
    page.search = (q or "").strip()

    try:
        plans, panels = await _filters(session)
        rows, total = await order_service.search(
            session,
            status=selected_status,
            search=page.search,
            panel_id=selected_panel,
            plan_id=selected_plan,
            limit=page.size,
            offset=page.offset,
        )
        stats = {
            "completed": await order_service.count_by_status(session, OrderStatus.COMPLETED),
            "awaiting_review": await order_service.count_by_status(session, OrderStatus.AWAITING_REVIEW),
            "failed": await order_service.count_by_status(session, OrderStatus.FAILED),
        }
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="error")

    page.total = total
    plan_names = {plan.id: plan.name for plan in plans}
    panel_names = {panel.id: panel.name for panel in panels}
    extra = _filter_query(
        status=selected_status.value if selected_status else "",
        panel_id=selected_panel,
        plan_id=selected_plan,
    )

    return render(
        request,
        "orders.html",
        {
            "page_title": "سفارش‌ها",
            "page_subtitle": f"{fa_digits(total)} سفارش با فیلترهای فعلی",
            "rows": rows,
            "page": page,
            "stats": stats,
            "plans": plans,
            "panels": panels,
            "plan_names": plan_names,
            "panel_names": panel_names,
            "status_labels": STATUS_LABELS,
            "kind_labels": KIND_LABELS,
            "method_labels": METHOD_LABELS,
            "selected_status": selected_status.value if selected_status else "",
            "selected_panel": selected_panel,
            "selected_plan": selected_plan,
            "base_url": BASE,
            "extra": extra,
        },
    )


# ---------------------------------------------------------------------------
# خروجی CSV
# ---------------------------------------------------------------------------
@router.get("/orders/export.csv")
async def export_orders(
    request: Request,
    q: str = Query(""),
    status: str = Query(""),
    panel_id: str = Query(""),
    plan_id: str = Query(""),
    staff: Staff = Depends(require_any()),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        rows, _total = await order_service.search(
            session,
            status=_status_value(status),
            search=(q or "").strip(),
            panel_id=_optional_id(panel_id),
            plan_id=_optional_id(plan_id),
            limit=EXPORT_LIMIT,
            offset=0,
        )
        _plans, panels = await _filters(session)
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="error")

    return orders_csv(rows, {panel.id: panel.name for panel in panels})


# ---------------------------------------------------------------------------
# گذارهای وضعیت
# ---------------------------------------------------------------------------
async def _provision(session: AsyncSession, order: Order) -> str | None:
    """پس از ``commit`` نوبت ساخت است؛ پیام خطا برمی‌گرداند (یا ``None``)."""
    result = await provisioning.provision_order(order.id)
    if result.failed:
        return result.error or "ساخت سرویس ناموفق بود."
    return None


@router.post("/orders/{order_id}/confirm")
async def confirm_order(
    order_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        order = await _order_for(session, order_id)
        code = order.order_code
        await order_service.mark_paid(
            session,
            order,
            method=PaymentMethod.ADMIN,
            reference="manual",
            staff_id=staff.id,
        )
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="error")

    failure = await _provision(session, order)
    if failure:
        return redirect(
            BASE,
            message=f"پرداخت سفارش {code} تأیید شد ولی ساخت سرویس ناموفق بود: {failure}",
            level="error",
        )
    return redirect(BASE, message=f"پرداخت سفارش {code} تأیید شد و سرویس ساخته شد.")


@router.post("/orders/{order_id}/retry")
async def retry_order(
    order_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        order = await _order_for(session, order_id)
        code = order.order_code
        await order_service.retry_provisioning(session, order, staff.id)
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="error")

    failure = await _provision(session, order)
    if failure:
        return redirect(
            BASE,
            message=f"تلاش دوباره برای سفارش {code} ناموفق بود: {failure}",
            level="error",
        )
    return redirect(BASE, message=f"سرویس سفارش {code} با موفقیت ساخته شد.")


@router.post("/orders/{order_id}/cancel")
async def cancel_order(
    order_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    reason = form_str(form, "reason") or "لغو توسط مدیر"

    try:
        order = await _order_for(session, order_id)
        code = order.order_code
        await order_service.cancel(
            session,
            order,
            reason=reason,
            refund_wallet=True,
            staff_id=staff.id,
        )
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="error")

    return redirect(BASE, message=f"سفارش {code} لغو شد و وجه رزروشده به کیف پول کاربر برگشت.")


@router.post("/orders/{order_id}/refund")
async def refund_order(
    order_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    reason = form_str(form, "reason") or "بازگشت وجه توسط مدیر"

    try:
        order = await _order_for(session, order_id)
        code = order.order_code
        # سفارش پرداخت‌شده اول باید از مسیر انصراف بگذرد تا وضعیتش سازگار بماند.
        if order.status in REFUNDABLE_STATUSES:
            await order_service.cancel(
                session,
                order,
                reason=reason,
                refund_wallet=True,
                staff_id=staff.id,
            )
        await order_service.refund(
            session,
            order,
            reason=reason,
            to_wallet=True,
            staff_id=staff.id,
        )
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="error")

    return redirect(BASE, message=f"وجه سفارش {code} به کیف پول کاربر بازگشت داده شد.")


__all__ = ["orders_csv", "router", "users_csv"]
