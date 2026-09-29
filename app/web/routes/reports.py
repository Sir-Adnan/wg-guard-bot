"""گزارش فروش — درآمد، تفکیک‌ها، دفتر پرداخت‌ها و خروجی CSV.

همه‌ی محاسبات در :mod:`app.services.reports` انجام می‌شود؛ این ماژول فقط آن
اعداد را برای قالب آماده می‌کند: هندسه‌ی نمودار، ردیف‌های آماده‌ی ``bar_list`` و
دفتر پرداخت‌ها (که برای نمایش نام کاربر یک کوئری دسته‌ای جدا می‌خواهد، چون
:class:`app.db.models.Payment` رابطه‌ی ``user`` ندارد).

هرچه به پول مربوط است در دیتابیس **ریال** است و فقط هنگام نمایش با فیلتر
``|money`` (یا :func:`app.core.money.format_amount`) به واحد فروشگاه تبدیل می‌شود.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.core.money import fa_digits, format_amount
from app.db.models import Payment, PaymentKind, Staff, User
from app.services.reports import reports
from app.web.security import get_db_session, require_any, require_manager
from app.web.templating import render

log = get_logger(__name__)
router = APIRouter(tags=["reports"])

BASE = f"{settings.panel_prefix}/reports"

#: بازه‌های مجاز گزارش؛ هر چیز دیگری به بازه‌ی پیش‌فرض برمی‌گردد.
ALLOWED_DAYS: tuple[int, ...] = (7, 30, 90, 365)
DEFAULT_DAYS = 30
CSV_DAYS = 90
LEDGER_LIMIT = 200

#: برچسب فارسی انواع تراکنش دفتر کل (:class:`PaymentKind`).
KIND_TITLES: dict[str, str] = {
    PaymentKind.DEPOSIT.value: "شارژ کیف پول",
    PaymentKind.PURCHASE.value: "خرید سرویس",
    PaymentKind.REFUND.value: "بازگشت وجه",
    PaymentKind.ADJUSTMENT.value: "اصلاح توسط مدیر",
    PaymentKind.REFERRAL.value: "پاداش معرفی",
    PaymentKind.GIFT.value: "هدیه",
}

CSV_MEDIA_TYPE = "text/csv; charset=utf-8"


# ---------------------------------------------------------------------------
# کمک‌کننده‌ها
# ---------------------------------------------------------------------------
def _window(days: int) -> int:
    """بازه‌ی معتبر گزارش؛ مقدار ناشناخته به پیش‌فرض برمی‌گردد."""
    return days if days in ALLOWED_DAYS else DEFAULT_DAYS


def _bar_rows(rows: list[Any]) -> list[dict[str, Any]]:
    """ردیف‌های آماده‌ی ماکروی ``bar_list`` (درصد نسبت به بزرگ‌ترین درآمد)."""
    top = max((row.revenue_rial for row in rows), default=0) or 1
    return [
        {
            "label": row.label,
            "value": f"{format_amount(row.revenue_rial)} — {fa_digits(row.orders)} سفارش",
            "percent": round(row.revenue_rial / top * 100, 1),
        }
        for row in rows
    ]


async def _ledger_rows(session: AsyncSession, payments: list[Payment]) -> list[dict[str, Any]]:
    """دفتر پرداخت‌ها به‌همراه نام کاربر (یک کوئری برای همه‌ی ردیف‌ها)."""
    user_ids = {payment.user_id for payment in payments}
    users: dict[int, User] = {}
    if user_ids:
        users = {user.id: user for user in (await session.execute(select(User).where(User.id.in_(user_ids)))).scalars()}

    rows: list[dict[str, Any]] = []
    for payment in payments:
        user = users.get(payment.user_id)
        rows.append(
            {
                "id": payment.id,
                "created_at": payment.created_at,
                "user_name": user.display_name if user else "—",
                "user_telegram_id": user.telegram_id if user else None,
                "kind": payment.kind.value,
                "kind_label": KIND_TITLES.get(payment.kind.value, payment.kind.value),
                "method_label": reports.method_label(payment.method),
                "amount_rial": int(payment.amount_rial),
                "balance_after_rial": int(payment.balance_after_rial),
                "description": payment.description or "",
            }
        )
    return rows


def _csv(content: str, filename: str) -> Response:
    return Response(
        content=content.encode("utf-8"),
        media_type=CSV_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# صفحه‌ی گزارش
# ---------------------------------------------------------------------------
@router.get("/reports")
async def sales_report(
    request: Request,
    days: int = Query(DEFAULT_DAYS, description="بازه‌ی گزارش به روز (۷، ۳۰، ۹۰ یا ۳۶۵)"),
    staff: Staff = Depends(require_any()),
    session: AsyncSession = Depends(get_db_session),
):
    window = _window(days)

    summary = await reports.summary(session, days=window)
    series = await reports.daily_series(session, days=window)
    by_plan = await reports.by_plan(session, days=window)
    by_method = await reports.by_method(session, days=window)
    by_panel = await reports.by_panel(session, days=window)
    ledger = await _ledger_rows(session, await reports.ledger(session, days=window, limit=LEDGER_LIMIT))
    totals = await reports.totals_by_payment_kind(session, days=window)

    kind_totals = [
        {"key": key, "label": KIND_TITLES.get(key, key), "total_rial": total}
        for key, total in sorted(totals.items(), key=lambda item: -abs(item[1]))
    ]

    return render(
        request,
        "reports.html",
        {
            "page_title": "گزارش فروش",
            "page_subtitle": (f"{fa_digits(window)} روز گذشته · درآمد {format_amount(summary.revenue_rial)}"),
            "days": window,
            "day_options": ALLOWED_DAYS,
            "summary": summary,
            "series": series,
            "peak_revenue": max((point.revenue_rial for point in series), default=0),
            "by_plan": _bar_rows(by_plan),
            "by_method": _bar_rows(by_method),
            "by_panel": _bar_rows(by_panel),
            "ledger": ledger,
            "kind_totals": kind_totals,
            "ledger_limit": LEDGER_LIMIT,
            "base_url": BASE,
        },
    )


# ---------------------------------------------------------------------------
# خروجی CSV
# ---------------------------------------------------------------------------
@router.get("/reports/orders.csv")
async def export_orders(
    days: int = Query(CSV_DAYS, description="بازه‌ی خروجی به روز"),
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    content = await reports.orders_csv(session, days=_window(days))
    log.info("%s خروجی CSV سفارش‌ها را گرفت", staff.login or staff.name)
    return _csv(content, "orders.csv")


@router.get("/reports/services.csv")
async def export_services(
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    content = await reports.services_csv(session)
    log.info("%s خروجی CSV سرویس‌ها را گرفت", staff.login or staff.name)
    return _csv(content, "services.csv")


__all__ = ["router"]
