"""پلن‌های فروشگاه — مدیریت کاتالوگ (Plan).

خواندن فهرست از :mod:`app.services.catalog` انجام می‌شود؛ نوشتن روی خودِ مدل
است چون لایه سرویس عمداً فقط خواندنی نگه داشته شده است (تاریخ سفارش‌ها هرگز
با ویرایش پلن بازنویسی نمی‌شود).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError, ValidationError
from app.core.money import fa_digits
from app.db.models import Order, Plan, Staff
from app.services.catalog import catalog
from app.services.categories import categories
from app.services.ordering import next_sort_order
from app.web.deps import (
    Page,
    form_bool,
    form_dict,
    form_int,
    form_money,
    form_str,
    pagination,
)
from app.web.security import get_db_session, require_manager, verify_csrf
from app.web.templating import redirect, render

router = APIRouter(tags=["plans"])

BASE = f"{settings.panel_prefix}/plans"

#: سیاست شروع مدت‌زمان سرویس.
START_POLICIES: tuple[tuple[str, str], ...] = (
    ("first_connection", "از اولین اتصال"),
    ("immediate", "از لحظه خرید"),
)

DEFAULT_USERNAME_TEMPLATE = "wg{tg}"

#: Value of ``?category_id=`` that means "plans without a category at all".
UNCATEGORISED = "none"


# ---------------------------------------------------------------------------
# کمک‌کننده‌ها
# ---------------------------------------------------------------------------
def _optional_int(form: dict[str, Any], key: str) -> int | None:
    """فیلد خالی یعنی «نامحدود»."""
    return None if form_str(form, key) == "" else form_int(form, key, 0)


def _optional_money(form: dict[str, Any], key: str) -> int | None:
    """مبلغ خالی یعنی «تعیین نشده» (ورودی به تومان است)."""
    return None if form_str(form, key) == "" else form_money(form, key, 0)


def _feature_lines(raw: str) -> list[str]:
    return [line.strip() for line in (raw or "").replace("\r", "").split("\n") if line.strip()]


def _submitted_sort_order(form: dict[str, Any]) -> int | None:
    """``sort_order`` only when the form actually carried a number.

    The create/edit modal no longer has a «ترتیب نمایش» field — order comes
    from dragging on the list — so an absent field must mean "not my business":
    ``next_sort_order`` appends on create, and an edit leaves the row where the
    operator dragged it instead of resetting it behind their back.
    """
    raw = form.get("sort_order")
    if raw is None or form_str(form, "sort_order") == "":
        return None
    return form_int(form, "sort_order", 0)


async def _payload(session: AsyncSession, form: dict[str, Any], *, creating: bool) -> dict[str, Any]:
    """فیلدهای فرم را به ستون‌های ``Plan`` تبدیل می‌کند."""
    name = form_str(form, "name")
    if not name:
        raise ValidationError("نام پلن نمی‌تواند خالی باشد.")

    start_policy = form_str(form, "start_policy", "first_connection")
    if start_policy not in {key for key, _label in START_POLICIES}:
        start_policy = START_POLICIES[0][0]

    data: dict[str, Any] = {
        "name": name[:128],
        "description": form_str(form, "description") or None,
        "category_id": _optional_int(form, "category_id"),
        "panel_id": _optional_int(form, "panel_id"),
        "traffic_gb": _optional_int(form, "traffic_gb"),
        "duration_days": _optional_int(form, "duration_days"),
        "device_limit": max(form_int(form, "device_limit", 1), 1),
        "speed_limit_down_kbps": _optional_int(form, "speed_limit_down_kbps"),
        "speed_limit_up_kbps": _optional_int(form, "speed_limit_up_kbps"),
        "price_rial": form_money(form, "price", 0),
        "old_price_rial": _optional_money(form, "old_price"),
        "cost_rial": _optional_money(form, "cost"),
        "badge": form_str(form, "badge") or None,
        "features": _feature_lines(form_str(form, "features")),
        "start_policy": start_policy,
        "username_template": form_str(form, "username_template") or DEFAULT_USERNAME_TEMPLATE,
        "is_active": form_bool(form, "is_active"),
        "is_test": form_bool(form, "is_test"),
        "is_featured": form_bool(form, "is_featured"),
        "is_unlimited_stock": form_bool(form, "is_unlimited_stock"),
        "stock": _optional_int(form, "stock"),
    }

    submitted = _submitted_sort_order(form)
    if submitted is not None:
        data["sort_order"] = submitted
    elif creating:
        data["sort_order"] = await next_sort_order(session, "plans")
    return data


async def _form_context(session: AsyncSession) -> dict[str, Any]:
    """گزینه‌های مشترکِ هر دو مودال ساخت/ویرایش."""
    return {
        "category_options": await categories.flatten(session, active_only=False),
        "panels": await catalog.panels_for_select(session),
        "start_policies": START_POLICIES,
    }


def _category_filter(raw: str) -> tuple[int | None, bool]:
    """``(category_id, uncategorised)`` read from the ``category_id`` select.

    ``""`` is «همه دسته‌ها», ``none`` is «بدون دسته», and anything else is a
    category id.  A value that is neither (a hand-edited URL, a deleted
    category) falls back to "no filter" instead of failing the page.
    """
    text = (raw or "").strip()
    if text == UNCATEGORISED:
        return None, True
    if not text.isdigit():
        return None, False
    value = int(text)
    return (value, False) if value > 0 else (None, False)


async def _catalogue_page(
    session: AsyncSession, page: Page, *, category_id: int | None, uncategorised: bool
) -> tuple[list[Plan], int]:
    """One page of the catalogue, honouring the category filter.

    The filter is served by :meth:`CatalogService.list_plans`, because only that
    query knows the two rules the page promises: a parent category includes its
    descendants (``include_descendants``), and «بدون دسته» is a filter of its
    own — ``catalog.search`` supports neither, so a filtered view reads the
    matching plans whole and is sliced here.  The unfiltered view keeps its
    SQL-level paging, which is the common case.
    """
    if category_id is None and not uncategorised:
        return await catalog.search(session, page.search, limit=page.size, offset=page.offset)

    rows = await catalog.list_plans(
        session,
        category_id=category_id,
        include_descendants=category_id is not None,
        include_inactive=True,
        include_test=True,
    )
    if uncategorised:
        rows = [plan for plan in rows if plan.category_id is None]
    # Same order as ``catalog.search``, so filtering never reshuffles the list.
    rows.sort(key=lambda plan: (plan.sort_order, plan.id))
    return rows[page.offset : page.offset + page.size], len(rows)


# ---------------------------------------------------------------------------
# فهرست
# ---------------------------------------------------------------------------
@router.get("/plans")
async def list_plans(
    request: Request,
    category_id: str = Query("", description="فیلتر دسته‌بندی؛ خالی یعنی همه و none یعنی بدون دسته"),
    page: Page = Depends(pagination),
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    selected, uncategorised = _category_filter(category_id)
    rows, total = await _catalogue_page(session, page, category_id=selected, uncategorised=uncategorised)
    page.total = total
    selected_value = UNCATEGORISED if uncategorised else str(selected or "")
    context: dict[str, Any] = {
        "page_title": "پلن‌ها",
        "page_subtitle": (
            f"{fa_digits(total)} پلن با فیلترهای فعلی" if selected_value else f"{fa_digits(total)} پلن در کاتالوگ"
        ),
        "rows": rows,
        "page": page,
        "selected_category": selected_value,
        "extra": f"&category_id={selected_value}" if selected_value else "",
        "base_url": BASE,
    }
    context.update(await _form_context(session))
    return render(request, "plans.html", context)


# ---------------------------------------------------------------------------
# ساخت / ویرایش
# ---------------------------------------------------------------------------
@router.post("/plans")
async def create_plan(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        data = await _payload(session, form, creating=True)
        plan = Plan(**data)
        session.add(plan)
        await session.flush()
        name = plan.name
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    return redirect(BASE, message=f"پلن «{name}» ساخته شد.")


@router.post("/plans/{plan_id}")
async def update_plan(
    plan_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        plan = await catalog.get(session, plan_id)
        for key, value in (await _payload(session, form, creating=False)).items():
            setattr(plan, key, value)
        await session.flush()
        name = plan.name
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    return redirect(BASE, message=f"پلن «{name}» ذخیره شد.")


@router.post("/plans/{plan_id}/toggle")
async def toggle_plan(
    plan_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        plan = await catalog.get(session, plan_id)
        plan.is_active = not plan.is_active
        active, name = plan.is_active, plan.name
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    state = "فعال" if active else "غیرفعال"
    return redirect(BASE, message=f"پلن «{name}» {state} شد.")


@router.post("/plans/{plan_id}/duplicate")
async def duplicate_plan(
    plan_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        plan = await catalog.get(session, plan_id)
        copy = Plan(
            panel_id=plan.panel_id,
            name=f"{plan.name} (کپی)"[:128],
            description=plan.description,
            category=plan.category,
            category_id=plan.category_id,
            features=list(plan.features or []),
            is_featured=plan.is_featured,
            traffic_gb=plan.traffic_gb,
            duration_days=plan.duration_days,
            device_limit=plan.device_limit,
            speed_limit_down_kbps=plan.speed_limit_down_kbps,
            speed_limit_up_kbps=plan.speed_limit_up_kbps,
            start_policy=plan.start_policy,
            price_rial=plan.price_rial,
            old_price_rial=plan.old_price_rial,
            cost_rial=plan.cost_rial,
            # کپی عمداً غیرفعال است تا اشتباهی به فروش نرود.
            is_active=False,
            is_test=plan.is_test,
            is_unlimited_stock=plan.is_unlimited_stock,
            stock=plan.stock,
            # کپی به انتهای فهرست می‌رود تا ترتیبِ دستیِ اپراتور به‌هم نریزد.
            sort_order=await next_sort_order(session, "plans"),
            badge=plan.badge,
            interface_id=plan.interface_id,
            username_template=plan.username_template,
            note=plan.note,
            # ``wg_plan_id`` کپی نمی‌شود: اتصال هر پلن به نود خودکار ساخته می‌شود.
        )
        session.add(copy)
        await session.flush()
        name = copy.name
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    return redirect(BASE, message=f"پلن «{name}» ساخته شد؛ برای نمایش در فروشگاه آن را فعال کنید.")


@router.post("/plans/{plan_id}/delete")
async def delete_plan(
    plan_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        plan = await catalog.get(session, plan_id)
        name = plan.name
        orders = int(await session.scalar(select(func.count(Order.id)).where(Order.plan_id == plan_id)) or 0)
        if orders:
            return redirect(
                BASE,
                message=(
                    f"پلن «{name}» {fa_digits(orders)} سفارش ثبت‌شده دارد و حذف نمی‌شود؛ "
                    "برای کنار گذاشتن آن، پلن را غیرفعال کنید."
                ),
                level="danger",
            )
        await session.delete(plan)
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    return redirect(BASE, message=f"پلن «{name}» حذف شد.")


__all__ = ["router"]
