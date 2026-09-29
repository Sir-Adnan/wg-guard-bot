"""Dashboard: the first thing the owner sees."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.jalali import jalali_date, jalali_long, now_utc
from app.core.logging import get_logger
from app.db.models import Order, OrderStatus, Receipt, ReceiptStatus, Service, ServiceStatus, Ticket, TicketStatus
from app.panels.manager import panel_manager
from app.services.receipts import receipt_service
from app.services.reports import reports
from app.services.settings_store import app_settings
from app.web.security import get_db_session, require_any
from app.web.templating import render

log = get_logger(__name__)
router = APIRouter(tags=["dashboard"])

DASHBOARD_DAYS = 30


@router.get("/")
async def dashboard(
    request: Request,
    staff=Depends(require_any()),
    session: AsyncSession = Depends(get_db_session),
):
    data = await reports.dashboard(session, days=DASHBOARD_DAYS)
    summary = data.summary

    pending_receipts = list(
        (
            await session.execute(
                select(Receipt)
                .where(Receipt.status == ReceiptStatus.PENDING)
                .order_by(Receipt.created_at.desc())
                .limit(6)
            )
        ).scalars()
    )
    panels = await panel_manager.list_snapshots(session)

    expiring = list(
        (
            await session.execute(
                select(Service)
                .where(
                    Service.status == ServiceStatus.ACTIVE,
                    Service.expires_at.is_not(None),
                    Service.expires_at <= now_utc() + timedelta(days=3),
                )
                .order_by(Service.expires_at.asc())
                .limit(6)
            )
        ).scalars()
    )

    needs_attention = list(
        (
            await session.execute(
                select(Order)
                .where(Order.status.in_((OrderStatus.FAILED, OrderStatus.PAID, OrderStatus.PROVISIONING)))
                .order_by(Order.created_at.desc())
                .limit(5)
            )
        ).scalars()
    )

    # -- chart geometry (server-side so the panel needs no chart library) --
    peak = max((point.revenue_rial for point in data.series), default=0) or 1
    chart = [
        {
            "label": point.label,
            "orders": point.orders,
            "value": point.revenue_rial,
            "height": max(round(point.revenue_rial / peak * 150), 2),
        }
        for point in data.series
    ]

    open_tickets = int(
        await session.scalar(select(func.count(Ticket.id)).where(Ticket.status != TicketStatus.CLOSED)) or 0
    )

    warnings = list(getattr(getattr(request.app.state, "runtime", None), "warnings", []) or [])
    if not settings.bot_token:
        warnings.append("BOT_TOKEN تنظیم نشده است؛ ربات تلگرام غیرفعال است.")
    if app_settings.get_bool("shop.maintenance", False):
        warnings.append("حالت تعمیر فعال است و کاربران عادی به ربات دسترسی ندارند.")

    return render(
        request,
        "dashboard.html",
        {
            "page_title": "داشبورد",
            "page_subtitle": jalali_long(now_utc()),
            "summary": summary,
            "series": data.series,
            "chart": chart,
            "peak_revenue": peak,
            "by_plan": data.by_plan[:6],
            "by_method": data.by_method,
            "recent_orders": data.recent_orders,
            "pending_receipts": pending_receipts,
            "panels": panels,
            "expiring_services": expiring,
            "needs_attention": needs_attention,
            "open_tickets": open_tickets,
            "today_label": jalali_date(now_utc()),
            "warnings": warnings,
            "days": DASHBOARD_DAYS,
        },
    )


@router.get("/api/counters")
async def counters(
    staff=Depends(require_any()),
    session: AsyncSession = Depends(get_db_session),
):
    """Tiny JSON endpoint for the header badges."""
    return {
        "receipts": await receipt_service.pending_count(session),
        "tickets": int(
            await session.scalar(select(func.count(Ticket.id)).where(Ticket.status != TicketStatus.CLOSED)) or 0
        ),
        "awaiting_review": int(
            await session.scalar(select(func.count(Order.id)).where(Order.status == OrderStatus.AWAITING_REVIEW)) or 0
        ),
    }


__all__ = ["router"]
