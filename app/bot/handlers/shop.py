"""Shop: the category tree and the plan catalog.

Navigation model
----------------
``home`` → root categories (or a flat list when the owner has not created any)
``open`` → a category's sub-categories **and** its own plans, with a breadcrumb
``all``  → every plan in one list
Plans are paginated inside whichever view opened them, so the ``page`` action
always carries the category it belongs to.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import CatCB, MenuCB, PlanCB
from app.bot.keyboards import KB
from app.bot.menus import category_list, order_actions, payment_methods, plan_actions, plan_list
from app.bot.utils import answer_callback, paginate, show
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.money import format_amount, format_gb
from app.db.models import Plan, PlanCategory, User
from app.services.catalog import catalog
from app.services.categories import categories
from app.services.orders import order_service
from app.services.settings_store import card_payments_enabled, wallet_enabled
from app.services.texts import html_escape, texts

log = get_logger(__name__)
router = Router(name="shop")

PLANS_PER_PAGE = 6


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
@router.callback_query(MenuCB.filter(F.action == "buy"))
async def open_shop(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    await answer_callback(callback)
    await render_catalog(callback, session, action="home")


@router.callback_query(CatCB.filter(F.action == "home"))
async def catalog_home(callback: CallbackQuery, session: AsyncSession) -> None:
    await answer_callback(callback)
    await render_catalog(callback, session, action="home")


@router.callback_query(CatCB.filter(F.action == "open"))
async def catalog_open(callback: CallbackQuery, callback_data: CatCB, session: AsyncSession) -> None:
    await answer_callback(callback)
    await render_catalog(callback, session, action="open", category_id=callback_data.category_id)


@router.callback_query(CatCB.filter(F.action == "all"))
async def catalog_all(callback: CallbackQuery, callback_data: CatCB, session: AsyncSession) -> None:
    await answer_callback(callback)
    await render_catalog(callback, session, action="all", page=callback_data.page or 1)


@router.callback_query(CatCB.filter(F.action == "featured"))
async def catalog_featured(callback: CallbackQuery, callback_data: CatCB, session: AsyncSession) -> None:
    await answer_callback(callback)
    await render_catalog(callback, session, action="featured", page=callback_data.page or 1)


@router.callback_query(CatCB.filter(F.action == "page"))
async def catalog_page(callback: CallbackQuery, callback_data: CatCB, session: AsyncSession) -> None:
    await answer_callback(callback)
    await render_catalog(
        callback,
        session,
        action="all" if callback_data.category_id == 0 else "open",
        category_id=callback_data.category_id,
        page=callback_data.page or 1,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
async def render_catalog(
    event,
    session: AsyncSession,
    *,
    action: str,
    category_id: int = 0,
    page: int = 1,
) -> None:
    roots = await categories.roots(session)
    featured_count = len(await catalog.list_plans(session, featured_only=True))

    # -- owner has not built a tree yet: keep the classic flat catalog -----
    if not roots and action in ("home", "open"):
        plans = await catalog.list_plans(session)
        if not plans:
            await show(event, await texts.get("shop.empty", session))
            return
        slice_, page, total_pages = paginate(plans, page, PLANS_PER_PAGE)
        body = await texts.get("shop.title", session)
        if total_pages > 1:
            body += f"\n\n<i>صفحه {fa(page)} از {fa(total_pages)}</i>"
        await show(event, body, keyboard=await plan_list(session, slice_, page, total_pages))
        return

    if action == "featured":
        plans = await catalog.list_plans(session, featured_only=True)
        if not plans:
            await show(event, await texts.get("shop.empty", session))
            return
        slice_, page, total_pages = paginate(plans, page, PLANS_PER_PAGE)
        body = await texts.get("shop.featured_title", session) + "\n\n" + await texts.get("shop.choose_plan", session)
        await show(
            event,
            body,
            keyboard=await plan_list(session, slice_, page, total_pages, page_action="featured"),
        )
        return

    if action == "all":
        plans = await catalog.list_plans(session)
        if not plans:
            await show(event, await texts.get("shop.empty", session))
            return
        slice_, page, total_pages = paginate(plans, page, PLANS_PER_PAGE)
        body = await texts.get("shop.all_title", session) + "\n\n" + await texts.get("shop.choose_plan", session)
        if total_pages > 1:
            body += f"\n\n<i>صفحه {fa(page)} از {fa(total_pages)}</i>"
        await show(
            event,
            body,
            keyboard=await plan_list(session, slice_, page, total_pages, page_action="all"),
        )
        return

    # -- tree navigation ---------------------------------------------------
    node: PlanCategory | None = None
    if action == "open" and category_id:
        try:
            node = await categories.get(session, category_id)
        except AppError:
            node = None

    children = await categories.children(session, node.id if node else None)
    plans: list[Plan] = []
    if node is not None:
        plans = await catalog.list_plans(session, category_id=node.id, include_inactive=False)

    if not children and not plans:
        await show(event, await texts.get("shop.empty", session))
        return

    header = await _header(session, node)

    if plans:
        slice_, page, total_pages = paginate(plans, page, PLANS_PER_PAGE)
        body = header + "\n\n" + await texts.get("shop.choose_plan", session)
        if total_pages > 1:
            body += f"\n\n<i>صفحه {fa(page)} از {fa(total_pages)}</i>"
        back_to = node.parent_id if node is not None else None
        await show(
            event,
            body,
            keyboard=await plan_list(
                session,
                slice_,
                page,
                total_pages,
                page_action="page",
                category_id=node.id if node else 0,
                back_category_id=back_to,
            ),
        )
        return

    kb = await category_list(
        session,
        list(children),
        show_all=True,
        show_featured=featured_count > 0,
        page=page,
        total_pages=1,
        back_action="open" if (node is not None and node.parent_id) else "home",
    )
    await show(event, header, keyboard=kb)


async def _header(session: AsyncSession, node: PlanCategory | None) -> str:
    if node is None:
        return await texts.get("shop.title", session)

    trail = await categories.breadcrumb(session, node.id)
    path = " › ".join(category.name for category in trail)
    body = f"🗂 <b>{html_escape(path)}</b>"
    if node.description:
        body += f"\n\n{html_escape(node.description)}"
    return body


def fa(value: object) -> str:
    from app.core.money import fa_digits

    return fa_digits(str(value))


# ---------------------------------------------------------------------------
# One plan
# ---------------------------------------------------------------------------
async def render_plan(session: AsyncSession, plan: Plan) -> tuple[str, KB]:
    """Build the plan card text + keyboard (also used by deep links)."""
    unlimited = await texts.get("common.unlimited", session)

    volume = format_gb(plan.traffic_gb) if plan.traffic_gb else unlimited
    days_text = _duration_text(plan.duration_days, unlimited)
    speed = fa(f"{plan.speed_limit_down_kbps // 1024} مگابیت بر ثانیه") if plan.speed_limit_down_kbps else unlimited

    price = format_amount(plan.price_rial) if plan.price_rial else "رایگان"
    badge = ""
    if plan.badge:
        badge = f"<b>{html_escape(plan.badge)}</b>"
    elif plan.discount_percent:
        badge = f"<b>{fa(plan.discount_percent)}٪ تخفیف</b>"

    body = await texts.get(
        "shop.plan_card",
        session,
        name=html_escape(plan.name),
        badge=badge,
        volume=volume,
        days=days_text,
        devices=fa(plan.device_limit or 1),
        speed=speed,
        price=price,
        description=html_escape(plan.description or ""),
    )

    if plan.features:
        bullets = "\n".join(f"• {html_escape(str(item))}" for item in plan.features if str(item).strip())
        if bullets:
            body += f"\n\n{bullets}"

    available = catalog.is_available(plan)
    if not available:
        body += "\n\n" + await texts.get("shop.sold_out", session)

    can_buy = available and (wallet_enabled() or card_payments_enabled())
    keyboard = await plan_actions(session, plan, can_buy=can_buy)
    return body, keyboard


def _duration_text(days: int | None, unlimited: str) -> str:
    if not days:
        return unlimited
    if days % 30 == 0:
        months = days // 30
        return fa(f"{months} ماه")
    return fa(f"{days} روز")


@router.callback_query(PlanCB.filter(F.action == "view"))
async def view_plan(callback: CallbackQuery, callback_data: PlanCB, session: AsyncSession) -> None:
    await answer_callback(callback)
    try:
        plan = await catalog.get(session, callback_data.plan_id)
    except AppError:
        await show(callback, await texts.get("error.not_found", session))
        return
    text, keyboard = await render_plan(session, plan)
    await show(callback, text, keyboard=keyboard)


@router.callback_query(PlanCB.filter(F.action == "buy"))
async def buy_plan(callback: CallbackQuery, callback_data: PlanCB, session: AsyncSession, user: User) -> None:
    """Create the order and let the customer pick a payment method."""
    try:
        plan = await catalog.get(session, callback_data.plan_id, require_active=True)
    except AppError as exc:
        await callback.answer(exc.message, show_alert=True)
        return

    if not catalog.is_available(plan):
        await callback.answer(await texts.get("shop.sold_out", session), show_alert=True)
        return

    existing = await order_service.open_orders(session, user.id)
    same = next((o for o in existing if o.plan_id == plan.id and o.is_open), None)
    if same is not None:
        await answer_callback(callback)
        await show(
            callback,
            await texts.get("buy.duplicate", session, order=same.order_code),
            keyboard=await order_actions(session, same),
        )
        return

    try:
        order = await order_service.create(session, user, plan)
    except AppError as exc:
        await callback.answer(exc.message[:190], show_alert=True)
        return

    await answer_callback(callback)

    if order.payable_rial <= 0:
        from app.bot.handlers.purchase import finalize_order

        await finalize_order(callback, session, order)
        return

    body = await texts.get(
        "buy.choose_method", session, plan=html_escape(plan.name), price=format_amount(order.payable_rial)
    )
    await show(
        callback,
        body,
        keyboard=await payment_methods(session, order, card=card_payments_enabled(), wallet=wallet_enabled()),
    )


__all__ = ["PLANS_PER_PAGE", "render_catalog", "render_plan", "router"]
