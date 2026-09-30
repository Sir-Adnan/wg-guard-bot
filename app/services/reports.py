"""Sales reporting.

Aggregation happens in Python over a bounded row set rather than in SQL.  That
is a deliberate trade: it keeps the queries portable (PostgreSQL in production,
anything in tests), and it lets every figure carry a **Jalali** label without a
second translation layer.  The window is always bounded, so the memory cost is
predictable.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.jalali import jalali_date, jalali_short_day, now_utc, to_local
from app.core.logging import get_logger
from app.core.money import bytes_to_gb, format_amount, to_toman
from app.db.models import (
    Order,
    OrderKind,
    OrderStatus,
    Panel,
    Payment,
    PaymentKind,
    PaymentMethod,
    Receipt,
    ReceiptStatus,
    Service,
    ServiceStatus,
    Ticket,
    TicketStatus,
    User,
)

log = get_logger(__name__)

REVENUE_STATUSES = (OrderStatus.COMPLETED,)

METHOD_LABELS: dict[str, str] = {
    PaymentMethod.CARD.value: "کارت به کارت",
    PaymentMethod.WALLET.value: "کیف پول",
    PaymentMethod.ADMIN.value: "دستی (مدیر)",
    PaymentMethod.GIFT.value: "هدیه",
    PaymentMethod.REFERRAL.value: "معرفی دوستان",
}

KIND_LABELS: dict[str, str] = {
    OrderKind.NEW.value: "خرید جدید",
    OrderKind.RENEW.value: "تمدید",
    OrderKind.EXTRA_TRAFFIC.value: "حجم اضافه",
    OrderKind.EXTRA_DEVICE.value: "دستگاه اضافه",
    OrderKind.TEST.value: "سرویس تست",
}

STATUS_LABELS: dict[str, str] = {
    OrderStatus.DRAFT.value: "پیشنویس",
    OrderStatus.PENDING_PAYMENT.value: "در انتظار پرداخت",
    OrderStatus.AWAITING_REVIEW.value: "در انتظار بررسی رسید",
    OrderStatus.PAID.value: "پرداخت‌شده",
    OrderStatus.PROVISIONING.value: "در حال ساخت",
    OrderStatus.COMPLETED.value: "تکمیل‌شده",
    OrderStatus.FAILED.value: "خطا در ساخت",
    OrderStatus.CANCELED.value: "لغو‌شده",
    OrderStatus.REFUNDED.value: "بازگشت وجه",
    OrderStatus.EXPIRED.value: "منقضی",
}


@dataclass(slots=True)
class DayPoint:
    day: date
    label: str
    orders: int = 0
    revenue_rial: int = 0

    @property
    def revenue_toman(self) -> int:
        return to_toman(self.revenue_rial)


@dataclass(slots=True)
class Breakdown:
    key: str
    label: str
    orders: int = 0
    revenue_rial: int = 0

    @property
    def revenue_toman(self) -> int:
        return to_toman(self.revenue_rial)


@dataclass(slots=True)
class SalesSummary:
    window_days: int = 30
    revenue_rial: int = 0
    revenue_today_rial: int = 0
    orders_total: int = 0
    orders_completed: int = 0
    orders_pending: int = 0
    orders_failed: int = 0
    average_order_rial: int = 0
    new_users: int = 0
    active_services: int = 0
    expiring_soon: int = 0
    wallet_liability_rial: int = 0
    pending_receipts: int = 0
    open_tickets: int = 0

    @property
    def average_order_toman(self) -> int:
        return to_toman(self.average_order_rial)


@dataclass(slots=True)
class Dashboard:
    summary: SalesSummary = field(default_factory=SalesSummary)
    series: list[DayPoint] = field(default_factory=list)
    by_plan: list[Breakdown] = field(default_factory=list)
    by_method: list[Breakdown] = field(default_factory=list)
    by_panel: list[Breakdown] = field(default_factory=list)
    recent_orders: list[Order] = field(default_factory=list)

    @property
    def max_revenue(self) -> int:
        return max((point.revenue_rial for point in self.series), default=0)


class ReportService:
    """Builds the numbers shown on the dashboard and the reports page."""

    # -- window ------------------------------------------------------------
    @staticmethod
    def _since(days: int) -> datetime:
        return now_utc() - timedelta(days=max(days, 1))

    async def _orders_between(
        self, session: AsyncSession, since: datetime, *, statuses: tuple[OrderStatus, ...] | None = None
    ) -> list[Order]:
        stmt = select(Order).where(Order.created_at >= since)
        if statuses:
            stmt = stmt.where(Order.status.in_(statuses))
        return list((await session.execute(stmt.order_by(Order.created_at.asc()))).scalars())

    # -- summary -----------------------------------------------------------
    async def summary(self, session: AsyncSession, *, days: int = 30) -> SalesSummary:
        since = self._since(days)
        summary = SalesSummary(window_days=days)

        orders = await self._orders_between(session, since)
        today_start = to_local(now_utc()).replace(hour=0, minute=0, second=0, microsecond=0)

        for order in orders:
            summary.orders_total += 1
            if order.status == OrderStatus.COMPLETED:
                summary.orders_completed += 1
                summary.revenue_rial += int(order.payable_rial)
                if order.completed_at and to_local(order.completed_at) >= today_start:
                    summary.revenue_today_rial += int(order.payable_rial)
            elif order.status in (OrderStatus.PENDING_PAYMENT, OrderStatus.AWAITING_REVIEW, OrderStatus.PAID):
                summary.orders_pending += 1
            elif order.status == OrderStatus.FAILED:
                summary.orders_failed += 1

        if summary.orders_completed:
            summary.average_order_rial = summary.revenue_rial // summary.orders_completed

        summary.new_users = int(await session.scalar(select(func.count(User.id)).where(User.created_at >= since)) or 0)
        summary.active_services = int(
            await session.scalar(select(func.count(Service.id)).where(Service.status == ServiceStatus.ACTIVE)) or 0
        )
        soon = now_utc() + timedelta(days=3)
        summary.expiring_soon = int(
            await session.scalar(
                select(func.count(Service.id)).where(
                    Service.status == ServiceStatus.ACTIVE,
                    Service.expires_at.is_not(None),
                    Service.expires_at <= soon,
                )
            )
            or 0
        )
        summary.wallet_liability_rial = int(
            await session.scalar(select(func.coalesce(func.sum(User.balance_rial), 0))) or 0
        )
        summary.pending_receipts = int(
            await session.scalar(select(func.count(Receipt.id)).where(Receipt.status == ReceiptStatus.PENDING)) or 0
        )
        summary.open_tickets = int(
            await session.scalar(select(func.count(Ticket.id)).where(Ticket.status != TicketStatus.CLOSED)) or 0
        )
        return summary

    # -- series ------------------------------------------------------------
    async def daily_series(self, session: AsyncSession, *, days: int = 30) -> list[DayPoint]:
        since = self._since(days)
        orders = await self._orders_between(session, since)

        buckets: dict[date, DayPoint] = {}
        for offset in range(days):
            day = (to_local(now_utc()) - timedelta(days=days - 1 - offset)).date()
            buckets[day] = DayPoint(day=day, label=jalali_short_day(day))

        completed = [o for o in orders if o.status == OrderStatus.COMPLETED]
        for order in completed:
            stamp = to_local(order.completed_at or order.created_at)
            if stamp is None:
                continue
            point = buckets.get(stamp.date())
            if point is None:
                continue
            point.orders += 1
            point.revenue_rial += int(order.payable_rial)
        return list(buckets.values())

    # -- breakdowns --------------------------------------------------------
    async def by_plan(self, session: AsyncSession, *, days: int = 30, limit: int = 10) -> list[Breakdown]:
        orders = await self._orders_between(session, self._since(days), statuses=REVENUE_STATUSES)
        grouped: dict[str, Breakdown] = {}
        for order in orders:
            key = str(order.plan_id or 0)
            row = grouped.setdefault(key, Breakdown(key=key, label=order.plan_name or "—"))
            row.orders += 1
            row.revenue_rial += int(order.payable_rial)
        return sorted(grouped.values(), key=lambda r: r.revenue_rial, reverse=True)[:limit]

    async def by_method(self, session: AsyncSession, *, days: int = 30) -> list[Breakdown]:
        orders = await self._orders_between(session, self._since(days), statuses=REVENUE_STATUSES)
        grouped: dict[str, Breakdown] = {}
        for order in orders:
            key = order.payment_method.value if order.payment_method else "unknown"
            row = grouped.setdefault(key, Breakdown(key=key, label=METHOD_LABELS.get(key, "نامشخص")))
            row.orders += 1
            row.revenue_rial += int(order.payable_rial)
        return sorted(grouped.values(), key=lambda r: r.revenue_rial, reverse=True)

    async def by_panel(self, session: AsyncSession, *, days: int = 30) -> list[Breakdown]:
        orders = await self._orders_between(session, self._since(days), statuses=REVENUE_STATUSES)
        panels = {p.id: p.name for p in (await session.execute(select(Panel))).scalars()}
        grouped: dict[str, Breakdown] = {}
        for order in orders:
            key = str(order.panel_id or 0)
            row = grouped.setdefault(key, Breakdown(key=key, label=panels.get(order.panel_id or 0, "نامشخص")))
            row.orders += 1
            row.revenue_rial += int(order.payable_rial)
        return sorted(grouped.values(), key=lambda r: r.revenue_rial, reverse=True)

    async def by_status(self, session: AsyncSession, *, days: int = 30) -> list[Breakdown]:
        orders = await self._orders_between(session, self._since(days))
        grouped: dict[str, Breakdown] = {}
        for order in orders:
            key = order.status.value
            row = grouped.setdefault(key, Breakdown(key=key, label=STATUS_LABELS.get(key, key)))
            row.orders += 1
            row.revenue_rial += int(order.payable_rial)
        return sorted(grouped.values(), key=lambda r: r.orders, reverse=True)

    # -- dashboard ---------------------------------------------------------
    async def dashboard(self, session: AsyncSession, *, days: int = 30) -> Dashboard:
        recent = list((await session.execute(select(Order).order_by(Order.created_at.desc()).limit(8))).scalars())
        return Dashboard(
            summary=await self.summary(session, days=days),
            series=await self.daily_series(session, days=days),
            by_plan=await self.by_plan(session, days=days),
            by_method=await self.by_method(session, days=days),
            by_panel=await self.by_panel(session, days=days),
            recent_orders=recent,
        )

    # -- ledger ------------------------------------------------------------
    async def ledger(
        self,
        session: AsyncSession,
        *,
        days: int = 30,
        kinds: tuple[PaymentKind, ...] | None = None,
        limit: int = 200,
    ) -> list[Payment]:
        stmt = select(Payment).where(Payment.created_at >= self._since(days))
        if kinds:
            stmt = stmt.where(Payment.kind.in_(kinds))
        return list((await session.execute(stmt.order_by(Payment.created_at.desc()).limit(limit))).scalars())

    async def totals_by_payment_kind(self, session: AsyncSession, *, days: int = 30) -> dict[str, int]:
        rows = await session.execute(
            select(Payment.kind, func.coalesce(func.sum(Payment.amount_rial), 0))
            .where(Payment.created_at >= self._since(days))
            .group_by(Payment.kind)
        )
        return {kind.value: int(total) for kind, total in rows.all()}

    # -- CSV export --------------------------------------------------------
    async def orders_csv(self, session: AsyncSession, *, days: int = 90, limit: int = 5000) -> str:
        """UTF-8-with-BOM CSV that opens correctly in Excel (Persian)."""
        orders = list(
            (
                await session.execute(
                    select(Order)
                    .where(Order.created_at >= self._since(days))
                    .order_by(Order.created_at.desc())
                    .limit(limit)
                )
            ).scalars()
        )
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "کد سفارش",
                "کاربر",
                "شناسه تلگرام",
                "پلن",
                "نوع",
                "وضعیت",
                "روش پرداخت",
                "مبلغ (تومان)",
                "تخفیف (تومان)",
                "پرداختی (تومان)",
                "پنل",
                "سرویس",
                "تاریخ ایجاد",
                "تاریخ تکمیل",
            ]
        )
        for order in orders:
            user = order.user
            writer.writerow(
                [
                    order.order_code,
                    user.display_name if user else "—",
                    user.telegram_id if user else "",
                    order.plan_name,
                    KIND_LABELS.get(order.kind.value, order.kind.value),
                    STATUS_LABELS.get(order.status.value, order.status.value),
                    METHOD_LABELS.get(order.payment_method.value, "—") if order.payment_method else "—",
                    to_toman(order.amount_rial),
                    to_toman(order.discount_rial),
                    to_toman(order.payable_rial),
                    order.panel.name if order.panel else "—",
                    order.wg_user_id or "—",
                    jalali_date(order.created_at),
                    jalali_date(order.completed_at) if order.completed_at else "—",
                ]
            )
        return "\ufeff" + buffer.getvalue()

    async def services_csv(self, session: AsyncSession, *, limit: int = 5000) -> str:
        services = list(
            (await session.execute(select(Service).order_by(Service.created_at.desc()).limit(limit))).scalars()
        )
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "شناسه سرویس",
                "نام کاربری",
                "کاربر",
                "شناسه تلگرام",
                "پنل",
                "وضعیت",
                "حجم کل (گیگ)",
                "مصرف (گیگ)",
                "دستگاه",
                "شروع",
                "انقضا",
                "سرویس تست",
            ]
        )
        for service in services:
            user = service.user
            writer.writerow(
                [
                    service.id,
                    service.wg_username,
                    user.display_name if user else "—",
                    user.telegram_id if user else "",
                    service.panel.name if service.panel else "—",
                    service.status.value,
                    round(bytes_to_gb(service.traffic_limit_bytes), 2)
                    if service.traffic_limit_bytes is not None
                    else "نامحدود",
                    round(bytes_to_gb(service.traffic_used_bytes), 2),
                    service.device_limit,
                    jalali_date(service.started_at) if service.started_at else "—",
                    jalali_date(service.expires_at) if service.expires_at else "—",
                    "بله" if service.is_test else "خیر",
                ]
            )
        return "\ufeff" + buffer.getvalue()

    # -- misc --------------------------------------------------------------
    @staticmethod
    def status_label(status: OrderStatus) -> str:
        return STATUS_LABELS.get(status.value, status.value)

    @staticmethod
    def kind_label(kind: OrderKind) -> str:
        return KIND_LABELS.get(kind.value, kind.value)

    @staticmethod
    def method_label(method: PaymentMethod | None) -> str:
        if method is None:
            return "—"
        return METHOD_LABELS.get(method.value, method.value)

    @staticmethod
    def money(rial: int) -> str:
        return format_amount(rial)


reports = ReportService()


def sparkline(values: list[int], width: int = 220, height: int = 46) -> str:
    """Inline SVG sparkline (no chart library, no external requests)."""
    if not values:
        values = [0]
    peak = max(max(values), 1)
    step = width / max(len(values) - 1, 1)
    points = " ".join(
        f"{index * step:.1f},{height - (value / peak) * (height - 6) - 3:.1f}" for index, value in enumerate(values)
    )
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
        f'role="img" aria-hidden="true"><polyline points="{points}" /></svg>'
    )


__all__ = [
    "KIND_LABELS",
    "METHOD_LABELS",
    "STATUS_LABELS",
    "Breakdown",
    "Dashboard",
    "DayPoint",
    "ReportService",
    "SalesSummary",
    "reports",
    "sparkline",
]
